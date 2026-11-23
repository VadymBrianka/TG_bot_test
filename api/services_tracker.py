from aiohttp import web
from db import supabase, run_db, get_exchange_rates
from services.subscriptions import require_plan
from services.analytics_events import track_event


async def handle_list_services(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    rates = await get_exchange_rates()
    try:
        rows = await run_db(
            lambda: supabase.table("service_subscriptions")
            .select("*")
            .eq("user_id", user_uuid)
            .order("next_billing_date")
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []

    monthly_total_uah = 0.0
    for r in rows:
        if r.get("is_active"):
            amt = float(r.get("amount") or 0)
            curr = str(r.get("currency") or "UAH").upper()
            uah = amt * rates.get(curr, 1.0)
            if r.get("billing_cycle") == "yearly":
                uah = uah / 12.0
            monthly_total_uah += uah

    return web.json_response({"services": rows, "monthly_total_uah": round(monthly_total_uah, 2)})


async def handle_create_service(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    name = str(body.get("name") or "").strip()
    amount = float(body.get("amount") or 0)
    currency = str(body.get("currency") or "UAH").strip().upper()
    billing_cycle = str(body.get("billing_cycle") or "monthly").lower()
    next_billing_date = str(body.get("next_billing_date") or "").strip()
    reminder_days = max(0, min(14, int(body.get("reminder_days") or 2)))

    if not name or amount <= 0 or not next_billing_date:
        return web.json_response({"error": "Вкажіть назву, суму (>0) та дату наступного списання"}, status=400)
    if billing_cycle not in ("monthly", "yearly"):
        billing_cycle = "monthly"

    try:
        res = await run_db(
            lambda: supabase.table("service_subscriptions")
            .insert(
                {
                    "user_id": user_uuid,
                    "name": name[:120],
                    "amount": round(amount, 2),
                    "currency": currency[:10],
                    "billing_cycle": billing_cycle,
                    "next_billing_date": next_billing_date[:10],
                    "reminder_days": reminder_days,
                    "is_active": True,
                }
            )
            .execute()
        )
        await track_event(user_uuid, "service_tracker_add")
        return web.json_response({"status": "success", "service": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Помилка збереження (перевірте migrations.sql): {e}"}, status=400)


async def handle_toggle_service(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    svc_id = request.match_info.get("id")
    try:
        rows = await run_db(
            lambda: supabase.table("service_subscriptions")
            .select("is_active")
            .eq("id", svc_id)
            .eq("user_id", user_uuid)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return web.json_response({"error": "Сервіс не знайдено"}, status=404)
        new_state = not bool(rows[0].get("is_active", True))
        await run_db(
            lambda: supabase.table("service_subscriptions")
            .update({"is_active": new_state})
            .eq("id", svc_id)
            .eq("user_id", user_uuid)
            .execute()
        )
        return web.json_response({"status": "success", "is_active": new_state})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_delete_service(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    svc_id = request.match_info.get("id")
    try:
        await run_db(
            lambda: supabase.table("service_subscriptions")
            .delete()
            .eq("id", svc_id)
            .eq("user_id", user_uuid)
            .execute()
        )
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
