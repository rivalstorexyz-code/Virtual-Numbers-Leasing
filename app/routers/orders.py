"""الطلبات: إنشاء، مزامنة، فحص، إنهاء، إلغاء."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, Request

from app.config import (
    SIM5_ENABLED,
    GUEST_MODE,
    GUEST_REQUIRES_VOUCHER,
    HIDE_PRICES_FROM_CUSTOMERS,
    HIDE_BALANCE_FROM_CUSTOMERS,
)
from app.core.storage import STORE, persist_now, refresh
from app.core.state import now_iso
from app.deps import get_current_customer
from app.models.schemas import OrderCreate
from app.services.sim5 import (
    sim5_get,
    Sim5Error,
    build_live_catalog,
    calculate_sale_price,
    is_country_blocked,
)
from app.services.vouchers import validate_voucher, voucher_allows


router = APIRouter()


# الحالات التي تُعتبر "مغلقة" — لا يجوز إلغاؤها أو إنهاؤها مرة أخرى
# 🔒 إصلاح: ثوابت مركزية لمنع الاسترداد المزدوج
CLOSED_STATUSES = frozenset({"CANCELED", "CANCELLED", "FINISHED", "REFUNDED"})


def _next_id(items: list[dict]) -> int:
    """
    🔒 إصلاح: توليد ID آمن بدل len()+1.
    len()+1 يعطي IDs مكررة بعد أي حذف أو عند التزامن.
    """
    return (max((i["id"] for i in items), default=0)) + 1


def _find_customer_order(customer_id: int, order_id: int) -> dict | None:
    """🔒 إصلاح: مساعد موحّد لتقليل التكرار في check/finish/cancel."""
    return next(
        (o for o in STORE["orders"]
         if o["id"] == order_id and o["customer_id"] == customer_id),
        None,
    )


def _ensure_order_open(order: dict) -> None:
    """
    🔒 إصلاح: يرفض العملية على طلب مغلق.
    يمنع الاسترداد المزدوج عند تكرار الإلغاء أو إلغاء طلب منتهٍ.
    """
    if order.get("status") in CLOSED_STATUSES:
        raise HTTPException(
            400,
            f"الطلب مغلق بالفعل (الحالة: {order['status']}) — لا يمكن تنفيذ العملية",
        )




def _record_voucher_status(voucher: dict, status: str) -> None:
    """Append an immutable audit entry for each new voucher state change."""
    timestamp = now_iso()
    voucher["status_updated_at"] = timestamp
    voucher.setdefault("status_events", []).append({
        "status": status, "at": timestamp, "attempts": int(voucher.get("attempts") or 0)
    })


def _voucher_for_order(order: dict) -> dict | None:
    code = order.get("paid_by_voucher")
    if not code:
        return None
    return next((v for v in STORE["vouchers"] if v.get("code") == code), None)


def _mark_voucher_used_on_sms(order: dict) -> bool:
    """Consume only after a real SMS arrives from the supplier."""
    v = _voucher_for_order(order)
    if not v or v.get("status") != "reserved":
        return False
    if v.get("reserved_order_id") != order["id"]:
        return False
    v["status"] = "used"
    _record_voucher_status(v, "used")
    v["used_at"] = now_iso()
    v["reserved_order_id"] = None
    v["used_on"] = {"country": order["country_slug"], "service": order["service_id"]}
    # Give change from the prepaid voucher only after successful SMS delivery.
    if not order.get("voucher_remainder_settled"):
        remaining = round(float(v["value"]) - float(order["price"]), 2)
        if remaining > 0:
            customer = next((c for c in STORE["customers"].values() if c.get("id") == order["customer_id"]), None) if isinstance(STORE["customers"], dict) else next((c for c in STORE["customers"] if c.get("id") == order["customer_id"]), None)
            if customer:
                customer["balance"] += remaining
                STORE["transactions"].append({
                    "id": _next_id(STORE["transactions"]),
                    "customer_id": order["customer_id"],
                    "type": "voucher_remainder",
                    "description": f"باقي كود {v['code']} بعد وصول الرسالة",
                    "amount": remaining,
                    "balance_after": customer["balance"],
                    "created_at": now_iso(),
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                })
        order["voucher_remainder_settled"] = True
    return True


def _release_voucher_after_supplier_cancel(order: dict) -> bool:
    """Never release a voucher on a local timeout alone."""
    v = _voucher_for_order(order)
    if not v or v.get("status") != "reserved" or v.get("reserved_order_id") != order["id"]:
        return False
    if any(s.get("order_id") == order["id"] for s in STORE["sms"]):
        return False
    v["status"] = "retry"
    _record_voucher_status(v, "retry")
    v["reserved_order_id"] = None
    v["used_by"] = None
    v["used_at"] = None
    v["used_on"] = None
    v["last_failure"] = "لم تصل رسالة SMS"
    v["last_failure_at"] = now_iso()
    if not order.get("voucher_release_settled"):
        STORE["owner_wallet"]["total_profit"] -= order.get("profit", 0.0)
        STORE["owner_wallet"]["reserved_for_5sim"] -= order.get("fivesim_cost", 0.0)
        order["voucher_release_settled"] = True
    return True


def _has_sms(order: dict, provider_data: dict | None = None) -> bool:
    return bool((provider_data or {}).get("sms") or any(s.get("order_id") == order["id"] for s in STORE["sms"]))


# ══════════════════════════════════════════════════════════════
# POST /api/v1/orders
# ══════════════════════════════════════════════════════════════
@router.post("/api/v1/orders")
async def create_order(payload: OrderCreate, request: Request):
    refresh()
    customer = await get_current_customer(request)
    cid = customer["id"]

    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل — لا يمكن الشراء")

    if GUEST_MODE and GUEST_REQUIRES_VOUCHER and not payload.voucher_code:
        raise HTTPException(400, "أدخل كود الشراء أولاً")

    # 🔒 إصلاح: حذف الفحص المكرر لـ is_country_blocked
    # كان يُستدعى قبل وبعد build_live_catalog — واحد يكفي
    if is_country_blocked(payload.country):
        raise HTTPException(400, "هذه الدولة غير متاحة")

    voucher = None
    if payload.voucher_code:
        voucher = validate_voucher(payload.voucher_code)
        if not voucher_allows(voucher, payload.country.lower(), payload.service):
            pairs_str = ", ".join(
                f"{p['country']}/{p['service']}"
                for p in voucher["allowed_pairs"]
            )
            raise HTTPException(
                403,
                f"هذا الكود لا يصلح لـ {payload.country}/{payload.service}. "
                f"يصلح لـ: {pairs_str}",
            )

    result = await build_live_catalog()
    if not result["ok"]:
        raise HTTPException(503, f"تعذّر الاتصال بـ 5SIM: {result['error']}")

    c_slug = payload.country.lower()

    country = next(
        (c for c in result["countries"]
         if c["slug"] == c_slug or c["code"].lower() == c_slug),
        None,
    )
    if not country:
        raise HTTPException(400, "دولة غير مدعومة في 5SIM")

    service = next(
        (s for s in country["services"]
         if s["id"] == payload.service and s["available"]),
        None,
    )
    if not service:
        raise HTTPException(400, "خدمة غير متوفرة في هذه الدولة")

    if not service.get("operator"):
        raise HTTPException(400, "لم يُحدد مشغل متاح لهذه الخدمة")

    # 🔒 إصلاح: حساب sale_price المتوقع قبل الشراء
    # الكود القديم كان يشتري ثم يكتشف أن sale_price > balance فيضطر للإلغاء.
    # الآن نرفض مبكراً إن كان المتاح لا يغطي السعر النهائي المتوقع.
    expected_cost = float(service.get("fivesim_cost") or service["cost"])
    expected_sale_price = calculate_sale_price(expected_cost)

    if voucher:
        available_balance = voucher["value"]
        source_name = "الكود"
    else:
        if HIDE_BALANCE_FROM_CUSTOMERS and GUEST_MODE:
            raise HTTPException(400, "أدخل كود الشراء أولاً")
        available_balance = customer["balance"]
        source_name = "رصيدك"

    if available_balance < expected_sale_price:
        raise HTTPException(
            400,
            f"{source_name} غير كافٍ. المطلوب: ${expected_sale_price:.2f}، "
            f"المتاح: ${available_balance:.2f}",
        )

    try:
        sim5_order = await sim5_get(
            f"/user/buy/activation/{country['slug']}/{service['operator']}/{service['id']}",
            auth=True,
            timeout=30.0,
        )
    except Sim5Error as e:
        raise HTTPException(
            status_code=400 if 400 <= e.status < 500 else 503,
            detail=f"فشل الشراء من 5SIM: {e.message}",
        )

    fivesim_cost = float(sim5_order.get("price", expected_cost))
    sale_price = calculate_sale_price(fivesim_cost)
    profit = round(sale_price - fivesim_cost, 4)

    # 🔒 إصلاح: فحص ما بعد الشراء يبقى كشبكة أمان
    # (السعر الفعلي من 5SIM قد يختلف عن المتوقع)
    if voucher:
        if voucher["value"] < sale_price:
            try:
                await sim5_get(f"/user/cancel/{sim5_order['id']}", auth=True)
            except Exception:
                pass
            raise HTTPException(
                400,
                f"قيمة الكود غير كافية. المطلوب: ${sale_price:.2f}، "
                f"قيمة الكود: ${voucher['value']:.2f}",
            )
    else:
        if customer["balance"] < sale_price:
            try:
                await sim5_get(f"/user/cancel/{sim5_order['id']}", auth=True)
            except Exception:
                pass
            raise HTTPException(
                400,
                f"رصيدك غير كافٍ. المطلوب: ${sale_price:.2f}، "
                f"المتاح: ${customer['balance']:.2f}",
            )

    now = datetime.now(timezone.utc)
    order_id = _next_id(STORE["orders"])

    order = {
        "id": order_id,
        "customer_id": cid,
        "sim5_id": sim5_order.get("id"),
        "phone": sim5_order.get("phone", ""),
        "country": country["name"],
        "country_slug": country["slug"],
        "country_code": country["code"],
        "country_prefix": country.get("prefix", ""),
        "flag": country["flag"],
        "service": service["name"],
        "service_id": service["id"],
        "operator": sim5_order.get("operator", service["operator"]),
        "price": sale_price,
        "fivesim_cost": fivesim_cost,
        "profit": profit,
        "status": sim5_order.get("status", "PENDING"),
        "expires": sim5_order.get("expires"),
        "created_at": sim5_order.get("created_at", now.isoformat()),
        "sms": sim5_order.get("sms") or [],
        "paid_by_voucher": voucher["code"] if voucher else None,
        "refunded": False,  # 🔒 إصلاح: علم صريح لمنع الاسترداد المزدوج
        "voucher_remainder_settled": False,
    }
    STORE["orders"].append(order)

    STORE["numbers"].append({
        "id": _next_id(STORE["numbers"]),
        "customer_id": cid,
        "sim5_id": sim5_order.get("id"),
        "phone": order["phone"],
        "country": country["name"],
        "country_code": country["code"],
        "country_prefix": country.get("prefix", ""),
        "flag": country["flag"],
        "service": service["name"],
        "service_id": service["id"],
        "status": "active" if order["status"] in ("PENDING", "RECEIVED") else order["status"],
        "price": sale_price,
        "sms_count": 0,
        "expires": order["expires"],
        "created_at": order["created_at"],
    })

    if voucher:
        voucher["status"] = "reserved"
        voucher["attempts"] = int(voucher.get("attempts") or 0) + 1
        _record_voucher_status(voucher, "reserved")
        voucher["reserved_order_id"] = order_id
        voucher["reserved_at"] = now.isoformat()
        voucher["used_by"] = cid
        voucher["used_on"] = {"country": country["slug"], "service": service["id"]}
        # Do not settle voucher or credit unused value until SMS is received.
    else:
        customer["balance"] -= sale_price

    ow = STORE["owner_wallet"]
    ow["total_profit"] += profit
    ow["reserved_for_5sim"] += fivesim_cost

    STORE["transactions"].append({
        "id": _next_id(STORE["transactions"]),
        "customer_id": cid,
        "type": "purchase",
        "description": f"شراء رقم {service['name']} — {country['name']}"
                       + (f" (كود {voucher['code']})" if voucher else ""),
        "amount": -sale_price,
        "balance_after": customer["balance"],
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })

    persist_now()

    response = {
        "id": order_id,
        "sim5_id": order["sim5_id"],
        "phone": order["phone"],
        "status": order["status"],
        "expires": order["expires"],
        "created_at": order["created_at"],
        "service": service["name"],
        "country": country["name"],
        "country_code": country["code"],
        "country_prefix": country.get("prefix", ""),
        "flag": country["flag"],
        "paid_by_voucher": voucher["code"] if voucher else None,
    }
    if not HIDE_PRICES_FROM_CUSTOMERS:
        response["price"] = sale_price
    if not HIDE_BALANCE_FROM_CUSTOMERS:
        response["new_balance"] = customer["balance"]

    return response


# ══════════════════════════════════════════════════════════════
# GET /api/v1/orders/sync
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/orders/sync")
async def sync_orders_from_sim5(request: Request, category: str = "activation"):
    refresh()
    customer = await get_current_customer(request)
    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل")
    try:
        data = await sim5_get(
            f"/user/orders?category={category}&limit=100&offset=0&order=id&reverse=true",
            auth=True,
        )
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    sim5_orders = data.get("Data") or []
    existing = {o.get("sim5_id") for o in STORE["orders"]}

    imported = 0
    for so in sim5_orders:
        if so["id"] in existing:
            continue
        STORE["orders"].append({
            "id": _next_id(STORE["orders"]),
            "customer_id": customer["id"],
            "sim5_id": so["id"],
            "phone": so.get("phone", ""),
            "country": so.get("country", ""),
            "country_slug": so.get("country", ""),
            "country_code": "",
            "country_prefix": "",
            "flag": "🏳️",
            "service": so.get("product", ""),
            "service_id": so.get("product", ""),
            "operator": so.get("operator"),
            "price": float(so.get("price", 0)),
            "fivesim_cost": float(so.get("price", 0)),
            "profit": 0.0,
            "status": so.get("status", ""),
            "expires": so.get("expires"),
            "created_at": so.get("created_at"),
            "sms": so.get("sms") or [],
            "refunded": False,  # 🔒 إصلاح: اتساق الحقول مع create_order
        })
        imported += 1

    if imported:
        persist_now()

    return {"ok": True, "imported": imported, "total_from_sim5": len(sim5_orders)}


# ══════════════════════════════════════════════════════════════
# GET /api/v1/orders/{order_id}/check
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/orders/{order_id}/check")
async def check_order(order_id: int, request: Request):
    refresh()
    customer = await get_current_customer(request)
    order = _find_customer_order(customer["id"], order_id)
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")

    try:
        data = await sim5_get(f"/user/check/{order['sim5_id']}", auth=True, timeout=15.0)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    order["status"] = data.get("status", order["status"])
    order["expires"] = data.get("expires", order.get("expires"))

    existing_keys = {(s.get("order_id"), s.get("created_at")) for s in STORE["sms"]}
    imported = 0
    for sms in (data.get("sms") or []):
        key = (order_id, sms.get("created_at"))
        if key in existing_keys:
            continue
        STORE["sms"].append({
            "id": _next_id(STORE["sms"]),
            "sim5_id": sms.get("id"),
            "customer_id": customer["id"],
            "order_id": order_id,
            "phone": order["phone"],
            "sender": sms.get("sender", ""),
            "text": sms.get("text", ""),
            "code": sms.get("code", ""),
            "created_at": sms.get("created_at") or now_iso(),
            "read": False,
        })
        imported += 1

    for n in STORE["numbers"]:
        if n.get("sim5_id") == order["sim5_id"]:
            n["sms_count"] = len([s for s in STORE["sms"] if s.get("order_id") == order_id])
            if order["status"] == "RECEIVED":
                n["status"] = "active"

    if _has_sms(order, data):
        _mark_voucher_used_on_sms(order)
    else:
        created = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = datetime.now(timezone.utc) - created
        if elapsed >= timedelta(minutes=30) and order["status"] not in CLOSED_STATUSES:
            # Supplier cancellation must succeed before permitting a new attempt.
            try:
                cancelled = await sim5_get(f"/user/cancel/{order['sim5_id']}", auth=True)
                order["status"] = cancelled.get("status", "CANCELED")
            except Sim5Error:
                pass
        if elapsed >= timedelta(minutes=30) and order["status"] in {"CANCELED", "CANCELLED", "REFUNDED"}:
            _release_voucher_after_supplier_cancel(order)
    persist_now()

    return {
        "ok": True,
        "status": order["status"],
        "expires": order["expires"],
        "created_at": order["created_at"],
        "phone": order["phone"],
        "country": order["country"],
        "service": order["service"],
        "flag": order.get("flag", ""),
        "new_sms": imported,
        "sms": data.get("sms") or [],
        "voucher_status": (_voucher_for_order(order) or {}).get("status"),
    }


# ══════════════════════════════════════════════════════════════
# POST /api/v1/orders/{order_id}/finish
# ══════════════════════════════════════════════════════════════
@router.post("/api/v1/orders/{order_id}/finish")
async def finish_order(order_id: int, request: Request):
    refresh()
    customer = await get_current_customer(request)
    order = _find_customer_order(customer["id"], order_id)
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")

    # 🔒 إصلاح: منع إنهاء طلب مغلق مسبقاً
    _ensure_order_open(order)
    if not _has_sms(order):
        raise HTTPException(409, "لا يمكن إنهاء الطلب قبل وصول رسالة SMS")

    try:
        data = await sim5_get(f"/user/finish/{order['sim5_id']}", auth=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    order["status"] = data.get("status", "FINISHED")
    persist_now()
    return {"ok": True, "status": order["status"]}


# ══════════════════════════════════════════════════════════════
# POST /api/v1/orders/{order_id}/cancel
# ══════════════════════════════════════════════════════════════
@router.post("/api/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request):
    refresh()
    customer = await get_current_customer(request)
    order = _find_customer_order(customer["id"], order_id)
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")

    # 🔒 إصلاح حرج: منع الاسترداد المزدوج
    # الكود القديم كان يسترد المبلغ في كل استدعاء، مما يسمح باستنزاف الرصيد
    _ensure_order_open(order)
    if _has_sms(order):
        raise HTTPException(409, "وصلت رسالة SMS بالفعل، لا يمكن استرجاع الكود")

    try:
        data = await sim5_get(f"/user/cancel/{order['sim5_id']}", auth=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    order["status"] = data.get("status", "CANCELED")
    if order["status"] not in {"CANCELED", "CANCELLED", "REFUNDED"}:
        raise HTTPException(409, "المورد لم يؤكد الإلغاء؛ الكود يبقى محجوزًا")

    # 🔒 إصلاح: علم refunded يمنع أي استرداد لاحق حتى لو تغيرت الحالة
    if order.get("refunded"):
        persist_now()
        return {"ok": True, "status": order["status"], "refund": 0.0}

    order["refunded"] = True

    refund = order["price"]
    if order.get("paid_by_voucher") and _release_voucher_after_supplier_cancel(order):
        # Restore voucher for a retry, rather than crediting a duplicate cash balance.
        refund_desc = f"استعادة كود {order['paid_by_voucher']} بعد إلغاء طلب #{order_id}"
        refund = 0.0
    else:
        customer["balance"] += refund
        refund_desc = f"استرداد طلب ملغي #{order_id}"
        if not order.get("voucher_release_settled"):
            ow = STORE["owner_wallet"]
            ow["total_profit"] -= order.get("profit", 0.0)
            ow["reserved_for_5sim"] -= order.get("fivesim_cost", 0.0)
            order["voucher_release_settled"] = True

    STORE["transactions"].append({
        "id": _next_id(STORE["transactions"]),
        "customer_id": customer["id"],
        "type": "refund",
        "description": refund_desc,
        "amount": refund,
        "balance_after": customer["balance"],
        "created_at": now_iso(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    })

    for n in STORE["numbers"]:
        if n.get("sim5_id") == order["sim5_id"]:
            n["status"] = "cancelled"

    persist_now()

    return {"ok": True, "status": order["status"], "refund": refund}


@router.post("/api/v1/orders/{order_id}/ban")
async def ban_order(order_id: int, request: Request):
    refresh()
    customer = await get_current_customer(request)
    order = _find_customer_order(customer["id"], order_id)
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")
    _ensure_order_open(order)
    if _has_sms(order):
        raise HTTPException(409, "وصلت رسالة SMS بالفعل")
    try:
        data = await sim5_get(f"/user/ban/{order['sim5_id']}", auth=True)
    except Sim5Error as exc:
        raise HTTPException(exc.status or 503, exc.message)
    order["status"] = data.get("status", "BANNED")
    persist_now()
    return {"ok": True, "status": order["status"], "note": "الحظر لا يعني استرداد قيمة الكود"}
