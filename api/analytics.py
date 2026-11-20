import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from aiohttp import web
from db import supabase, run_db, get_exchange_rates
from services.subscriptions import require_plan
from services.advisor_ai import generate_financial_advice


async def handle_analytics_and_forecast(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied
    try:
        user_uuid: str = request["user_uuid"]
        date_from_str = request.query.get("date_from", "").strip()
        date_to_str = request.query.get("date_to", "").strip()

        now = datetime.now(timezone.utc)
        if date_from_str and date_to_str:
            start_dt = datetime.fromisoformat(f"{date_from_str}T00:00:00+00:00")
            end_dt = datetime.fromisoformat(f"{date_to_str}T23:59:59+00:00")
            days_back = max((end_dt.date() - start_dt.date()).days + 1, 1)
        else:
            days_back = min(max(int(request.query.get("days", 30)), 1), 365)
            end_dt = now
            start_dt = now - timedelta(days=days_back)

        prev_start_dt = start_dt - timedelta(days=days_back)

        def _load():
            txs = (
                supabase.table("transactions")
                .select("amount, target_amount, type, note, transaction_date, category_id, account_id, to_account_id")
                .eq("user_id", user_uuid)
                .gte("transaction_date", prev_start_dt.isoformat())
                .lte("transaction_date", end_dt.isoformat())
                .execute()
                .data
                or []
            )
            cats = supabase.table("categories").select("id, name, icon").execute().data or []
            accs = supabase.table("accounts").select("id, name, balance, currency").eq("user_id", user_uuid).execute().data or []
            return txs, cats, accs

        all_txs, cats, all_accounts = await run_db(_load)
        rates = await get_exchange_rates()

        cat_map = {c["id"]: {"name": c["name"], "icon": c["icon"]} for c in cats}
        acc_curr_map = {a["id"]: str(a.get("currency", "UAH")).upper() for a in all_accounts}

        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
        cur_txs = [t for t in all_txs if start_iso <= (t.get("transaction_date") or "") <= end_iso]
        prev_txs = [t for t in all_txs if (t.get("transaction_date") or "") < start_iso]

        def _to_uah(tx: dict) -> float:
            curr = acc_curr_map.get(tx.get("account_id"), "UAH")
            return float(tx.get("amount") or 0) * rates.get(curr, 1.0)

        total_income = sum(_to_uah(t) for t in cur_txs if t.get("type") == "income")
        total_expense = sum(_to_uah(t) for t in cur_txs if t.get("type") == "expense")

        prev_income = sum(_to_uah(t) for t in prev_txs if t.get("type") == "income")
        prev_expense = sum(_to_uah(t) for t in prev_txs if t.get("type") == "expense")

        def _pct_change(cur_v: float, prev_v: float) -> float | None:
            if prev_v <= 0:
                return None
            return round(((cur_v - prev_v) / prev_v) * 100, 1)

        daily_expense = total_expense / max(days_back, 1)
        daily_income = total_income / max(days_back, 1)
        daily_net = daily_income - daily_expense

        current_liquidity = sum(
            float(a.get("balance", 0)) * rates.get(str(a.get("currency", "UAH")).upper(), 1.0)
            for a in all_accounts
        )

        forecast = {
            "week": {
                "income": round(daily_income * 7, 2),
                "expense": round(daily_expense * 7, 2),
                "net": round(daily_net * 7, 2),
                "balance": round(current_liquidity + (daily_net * 7), 2),
            },
            "month": {
                "income": round(daily_income * 30, 2),
                "expense": round(daily_expense * 30, 2),
                "net": round(daily_net * 30, 2),
                "balance": round(current_liquidity + (daily_net * 30), 2),
            },
            "year": {
                "income": round(daily_income * 365, 2),
                "expense": round(daily_expense * 365, 2),
                "net": round(daily_net * 365, 2),
                "balance": round(current_liquidity + (daily_net * 365), 2),
            },
        }

        # Category breakdown in UAH
        cat_stats: dict[str, dict] = {}
        for t in cur_txs:
            if t.get("type") == "expense":
                cid = t.get("category_id")
                cat_info = cat_map.get(cid)
                cname = cat_info["name"] if cat_info else "Інше"
                cicon = cat_info["icon"] if cat_info else "⚪"
                if cname not in cat_stats:
                    cat_stats[cname] = {"name": cname, "icon": cicon, "total": 0.0}
                cat_stats[cname]["total"] += _to_uah(t)

        for v in cat_stats.values():
            v["total"] = round(v["total"], 2)
        categories_breakdown = sorted(cat_stats.values(), key=lambda x: x["total"], reverse=True)

        # Daily cashflow series for chart
        daily_map: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
        for t in cur_txs:
            tx_type = t.get("type")
            if tx_type not in ("income", "expense"):
                continue
            day_key = str(t.get("transaction_date", ""))[:10]
            if day_key:
                daily_map[day_key][tx_type] += _to_uah(t)

        daily_series = [
            {
                "date": d,
                "income": round(vals["income"], 2),
                "expense": round(vals["expense"], 2),
            }
            for d, vals in sorted(daily_map.items())
        ]

        # Account turnover (in each account's native currency)
        acc_dict = {
            a["id"]: {
                "id": a["id"],
                "name": a["name"],
                "currency": a.get("currency", "UAH"),
                "income": 0.0,
                "expense": 0.0,
                "transfers_in": 0.0,
                "transfers_out": 0.0,
            }
            for a in all_accounts
        }
        for t in cur_txs:
            from_id = t.get("account_id")
            to_id = t.get("to_account_id")
            amt = float(t.get("amount", 0))
            t_amt = float(t.get("target_amount") or amt)
            if t.get("type") == "income" and from_id in acc_dict:
                acc_dict[from_id]["income"] += amt
            elif t.get("type") == "expense" and from_id in acc_dict:
                acc_dict[from_id]["expense"] += amt
            elif t.get("type") == "transfer":
                if from_id in acc_dict:
                    acc_dict[from_id]["transfers_out"] += amt
                if to_id in acc_dict:
                    acc_dict[to_id]["transfers_in"] += t_amt

        runway_days = round(current_liquidity / daily_expense) if daily_expense > 0 else 999

        return web.json_response(
            {
                "history_days": days_back,
                "historical_income": round(total_income, 2),
                "historical_expense": round(total_expense, 2),
                "daily_burn_rate": round(daily_expense, 2),
                "runway_days": runway_days,
                "current_liquidity": round(current_liquidity, 2),
                "mom_comparison": {
                    "prev_income": round(prev_income, 2),
                    "prev_expense": round(prev_expense, 2),
                    "income_change_pct": _pct_change(total_income, prev_income),
                    "expense_change_pct": _pct_change(total_expense, prev_expense),
                },
                "daily_series": daily_series,
                "forecast": forecast,
                "categories_breakdown": categories_breakdown,
                "accounts_breakdown": list(acc_dict.values()),
            }
        )
    except Exception as e:
        logging.error(f"Analytics error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_analytics_advice(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        advice = await generate_financial_advice(user_uuid)
        return web.json_response({"advice": advice})
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        logging.error(f"Advice error: {e}")
        return web.json_response({"error": str(e) or "Помилка ШІ-радника"}, status=502)
