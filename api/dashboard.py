import logging
from aiohttp import web
from db import supabase, run_db, get_exchange_rates


async def handle_health(request: web.Request):
    try:
        await run_db(lambda: supabase.table("exchange_rates").select("currency").limit(1).execute())
        return web.json_response(
            {"status": "ok", "db": "active", "message": "FinTrack Engine is live! 🚀"},
            status=200,
        )
    except Exception as e:
        logging.error(f"Healthcheck DB error: {e}")
        return web.json_response({"status": "degraded", "db": "error", "detail": str(e)}, status=500)


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

        def _load():
            accs = supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []
            invs = supabase.table("investments").select("*").eq("user_id", user_uuid).order("updated_at", desc=True).execute().data or []
            goals = supabase.table("goals").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []
            try:
                cats = (
                    supabase.table("categories")
                    .select("*")
                    .or_(f"user_id.is.null,user_id.eq.{user_uuid}")
                    .order("name")
                    .execute()
                    .data
                    or []
                )
            except Exception:
                cats = supabase.table("categories").select("*").order("name").execute().data or []

            raw_txs = (
                supabase.table("transactions")
                .select("*, categories(name, icon)")
                .eq("user_id", user_uuid)
                .order("transaction_date", desc=True)
                .limit(50)
                .execute()
                .data
                or []
            )
            user_row = supabase.table("users").select("*").eq("id", user_uuid).limit(1).execute().data
            return accs, invs, goals, cats, raw_txs, (user_row[0] if user_row else {})

        accs, invs, goals, cats, raw_txs, user_row = await run_db(_load)
        rates = await get_exchange_rates()

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

        return web.json_response(
            {
                "accounts": accs,
                "investments": invs,
                "goals": goals,
                "categories": cats,
                "transactions": txs,
                "exchange_rates": rates,
                "settings": {
                    "default_account_id": user_row.get("default_account_id"),
                    "weekly_digest": user_row.get("weekly_digest", True),
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
