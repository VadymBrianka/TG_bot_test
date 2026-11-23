import asyncio
import logging
from aiohttp import web
from config import ADMIN_ID
from db import supabase, run_db, get_exchange_rates
from services.subscriptions import PLAN_ORDER


async def handle_health(request: web.Request):
    try:
        await run_db(lambda: supabase.table("exchange_rates").select("currency").limit(1).execute())
        return web.json_response(
            {"status": "ok", "db": "active", "message": "FinTrack Engine is live! 🚀"},
            status=200,
        )
    except Exception as e:
        logging.error(f"Healthcheck DB error: {e}")
        return web.json_response({"status": "degraded", "db": "error"}, status=500)


async def handle_get_currencies(request: web.Request):
    try:
        res = await run_db(
            lambda: supabase.table("exchange_rates").select("currency, rate_to_base").order("currency").execute().data or []
        )
        currencies = [r for r in res if r.get("currency") != "USDT"]
        return web.json_response({"currencies": currencies})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_dashboard_summary(request: web.Request):
    try:
        user_uuid: str = request["user_uuid"]
        tg_id: int = request["tg_id"]
        user_row = request.get("user_row") or {}

        def _load_cats():
            try:
                return (
                    supabase.table("categories")
                    .select("*")
                    .or_(f"user_id.is.null,user_id.eq.{user_uuid}")
                    .order("name")
                    .execute()
                    .data
                    or []
                )
            except Exception:
                return supabase.table("categories").select("*").order("name").execute().data or []

        accs, invs, goals, cats, raw_txs, rates = await asyncio.gather(
            run_db(lambda: supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []),
            run_db(lambda: supabase.table("investments").select("*").eq("user_id", user_uuid).order("updated_at", desc=True).execute().data or []),
            run_db(lambda: supabase.table("goals").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []),
            run_db(_load_cats),
            run_db(
                lambda: supabase.table("transactions")
                .select("*, categories(name, icon)")
                .eq("user_id", user_uuid)
                .order("transaction_date", desc=True)
                .limit(50)
                .execute()
                .data
                or []
            ),
            get_exchange_rates(),
        )

        acc_map = {a["id"]: {"name": a["name"], "currency": a["currency"]} for a in accs}
        txs = []
        for t in raw_txs:
            t["from_acc"] = acc_map.get(t.get("account_id"))
            t["to_acc"] = acc_map.get(t.get("to_account_id"))
            txs.append(t)

        total_accounts_uah = sum(float(a["balance"]) * rates.get(str(a.get("currency", "UAH")).upper(), 1.0) for a in accs)
        total_investments_uah = sum(
            float(i["quantity"])
            * float(i.get("current_price") or i.get("buy_price_avg") or 0)
            * rates.get(str(i.get("currency", "USD")).upper(), 1.0)
            for i in invs
        )
        total_goals_uah = sum(float(g["current_amount"]) * rates.get(str(g.get("currency", "UAH")).upper(), 1.0) for g in goals)
        net_worth = total_accounts_uah + total_investments_uah + total_goals_uah

        role = str(user_row.get("role") or ("admin" if tg_id == ADMIN_ID else "user")).lower()
        if tg_id == ADMIN_ID or role == "admin":
            subscription = {"plan": "vip", "expires_at": None, "is_active": True, "is_admin_override": True}
        else:
            plan = str(user_row.get("subscription_plan") or "free").lower()
            if plan not in PLAN_ORDER:
                plan = "free"
            exp_raw = user_row.get("subscription_expires_at")
            if plan != "free" and exp_raw:
                from datetime import datetime, timezone
                try:
                    if datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00")) < datetime.now(timezone.utc):
                        plan = "free"
                        exp_raw = None
                except Exception:
                    pass
            subscription = {"plan": plan, "expires_at": exp_raw, "is_active": True}

        return web.json_response(
            {
                "role": role,
                "subscription": subscription,
                "accounts": accs,
                "investments": invs,
                "goals": goals,
                "categories": cats,
                "transactions": txs,
                "exchange_rates": rates,
                "settings": {
                    "default_account_id": user_row.get("default_account_id"),
                    "base_currency": user_row.get("base_currency", "UAH"),
                    "weekly_digest": user_row.get("weekly_digest", True),
                    "monthly_digest": user_row.get("monthly_digest", True),
                    "budget_alerts_enabled": user_row.get("budget_alerts_enabled", True),
                    "daily_reminder": user_row.get("daily_reminder", False),
                    "notifications_enabled": user_row.get("notifications_enabled", True),
                },
                "summary": {
                    "net_worth": round(net_worth, 2),
                    "accounts_total_uah": round(total_accounts_uah, 2),
                    "investments_total_uah": round(total_investments_uah, 2),
                    "goals_total_uah": round(total_goals_uah, 2),
                },
            }
        )
    except Exception as e:
        logging.error(f"Dashboard Error: {e}")
        return web.json_response({"error": str(e)}, status=500)
