from datetime import datetime, timezone, date
from aiohttp import web
from aiogram.types import BufferedInputFile
from db import supabase, run_db, get_exchange_rates, create_transaction_atomic
from services.export_csv import build_transactions_csv
from services.subscriptions import require_plan
from schemas import (
    BudgetCreate,
    RecurringCreate,
    PriceAlertCreate,
    UserSettingsUpdate,
    CsvImportRequest,
    parse_json_body,
)


# --- BUDGETS ---

async def handle_list_budgets(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        def _load():
            budgets = supabase.table("budgets").select("*").eq("user_id", user_uuid).execute().data or []
            txs = (
                supabase.table("transactions")
                .select("amount, category_id, account_id")
                .eq("user_id", user_uuid)
                .eq("type", "expense")
                .gte("transaction_date", month_start)
                .execute()
                .data
                or []
            )
            accs = supabase.table("accounts").select("id, currency").eq("user_id", user_uuid).execute().data or []
            cats = supabase.table("categories").select("id, name, icon").execute().data or []
            return budgets, txs, accs, cats

        budgets, txs, accs, cats = await run_db(_load)
        rates = await get_exchange_rates()
        acc_curr = {a["id"]: str(a.get("currency", "UAH")).upper() for a in accs}
        cat_map = {c["id"]: c for c in cats}

        spent_by_cat_uah: dict[str, float] = {}
        for t in txs:
            cid = t.get("category_id")
            if cid:
                uah = float(t.get("amount") or 0) * rates.get(acc_curr.get(t.get("account_id"), "UAH"), 1.0)
                spent_by_cat_uah[cid] = spent_by_cat_uah.get(cid, 0.0) + uah

        enriched = []
        for b in budgets:
            curr = str(b.get("currency") or "UAH").upper()
            rate = rates.get(curr, 1.0)
            spent_in_curr = spent_by_cat_uah.get(b["category_id"], 0.0) / rate
            cat = cat_map.get(b["category_id"], {})
            enriched.append(
                {
                    **b,
                    "category_name": cat.get("name", "Категорія"),
                    "category_icon": cat.get("icon", "🏷️"),
                    "spent": round(spent_in_curr, 2),
                }
            )
        return web.json_response({"budgets": enriched})
    except Exception:
        return web.json_response({"budgets": [], "migration_required": True})


async def handle_create_budget(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied
    body, err_resp = await parse_json_body(request, BudgetCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("budgets")
            .upsert(
                {
                    "user_id": user_uuid,
                    "category_id": body.category_id,
                    "monthly_limit": body.monthly_limit,
                    "currency": body.currency,
                },
                on_conflict="user_id,category_id",
            )
            .execute()
        )
        return web.json_response({"status": "success", "budget": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося зберегти бюджет: {e}"}, status=400)


async def handle_delete_budget(request: web.Request):
    bid = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("budgets").delete().eq("id", bid).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Бюджет не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


# --- RECURRING PAYMENTS ---

def _next_run_for_day(day: int) -> str:
    today = datetime.now(timezone.utc).date()
    try:
        candidate = today.replace(day=min(day, 28))
    except ValueError:
        candidate = today
    if candidate <= today:
        month = today.month + 1
        year = today.year
        if month > 12:
            month = 1
            year += 1
        candidate = date(year, month, min(day, 28))
    return candidate.isoformat()


async def handle_list_recurring(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        items = await run_db(
            lambda: supabase.table("recurring_payments")
            .select("*")
            .eq("user_id", user_uuid)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return web.json_response({"recurring": items})
    except Exception:
        return web.json_response({"recurring": [], "migration_required": True})


async def handle_create_recurring(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied
    body, err_resp = await parse_json_body(request, RecurringCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("recurring_payments")
            .insert(
                {
                    "user_id": user_uuid,
                    "account_id": body.account_id,
                    "category_id": body.category_id or None,
                    "title": body.title,
                    "amount": body.amount,
                    "type": body.type,
                    "day_of_month": body.day_of_month,
                    "next_run_date": _next_run_for_day(body.day_of_month),
                    "is_active": True,
                }
            )
            .execute()
        )
        return web.json_response({"status": "success", "item": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося створити платіж: {e}"}, status=400)


async def handle_delete_recurring(request: web.Request):
    rid = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("recurring_payments").delete().eq("id", rid).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Запис не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


# --- PRICE ALERTS ---

async def handle_list_alerts(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        items = await run_db(
            lambda: supabase.table("price_alerts")
            .select("*")
            .eq("user_id", user_uuid)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return web.json_response({"alerts": items})
    except Exception:
        return web.json_response({"alerts": [], "migration_required": True})


async def handle_create_alert(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied
    body, err_resp = await parse_json_body(request, PriceAlertCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("price_alerts")
            .insert(
                {
                    "user_id": user_uuid,
                    "ticker": body.ticker.strip().upper(),
                    "asset_class": body.asset_class,
                    "condition": body.condition,
                    "target_price": body.target_price,
                    "is_triggered": False,
                }
            )
            .execute()
        )
        return web.json_response({"status": "success", "alert": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося створити алерт: {e}"}, status=400)


async def handle_delete_alert(request: web.Request):
    aid = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("price_alerts").delete().eq("id", aid).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Алерт не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


# --- USER SETTINGS ---

async def handle_update_settings(request: web.Request):
    body, err_resp = await parse_json_body(request, UserSettingsUpdate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    payload = {}
    if body.default_account_id is not None:
        payload["default_account_id"] = body.default_account_id or None
    if body.base_currency is not None:
        payload["base_currency"] = body.base_currency
    if body.weekly_digest is not None:
        payload["weekly_digest"] = body.weekly_digest
    if body.monthly_digest is not None:
        payload["monthly_digest"] = body.monthly_digest
    if body.budget_alerts_enabled is not None:
        payload["budget_alerts_enabled"] = body.budget_alerts_enabled
    if body.daily_reminder is not None:
        payload["daily_reminder"] = body.daily_reminder
    if body.notifications_enabled is not None:
        payload["notifications_enabled"] = body.notifications_enabled

    if not payload:
        return web.json_response({"status": "ok"})

    try:
        await run_db(lambda: supabase.table("users").update(payload).eq("id", user_uuid).execute())
        return web.json_response({"status": "updated", "settings": payload})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося зберегти налаштування: {e}"}, status=400)


# --- EXPORT / IMPORT CSV ---

async def handle_export_csv(request: web.Request):
    user_uuid: str = request["user_uuid"]
    days = min(max(int(request.query.get("days", 365)), 1), 3650)
    csv_text = await build_transactions_csv(user_uuid, days=days)
    return web.Response(
        text=csv_text,
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": 'attachment; filename="fintrack_export.csv"'},
    )


async def handle_export_send_telegram(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied
    user_uuid: str = request["user_uuid"]
    tg_id: int = request["tg_id"]
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot instance unavailable"}, status=500)

    days = min(max(int(request.query.get("days", 365)), 1), 3650)
    csv_text = await build_transactions_csv(user_uuid, days=days)
    filename = f"fintrack_export_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    doc = BufferedInputFile(csv_text.encode("utf-8"), filename=filename)
    await bot.send_document(
        chat_id=tg_id,
        document=doc,
        caption=f"📊 <b>Експорт транзакцій за {days} дн.</b>",
        parse_mode="HTML",
    )
    return web.json_response({"status": "sent", "filename": filename})


async def handle_import_csv(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied
    body, err_resp = await parse_json_body(request, CsvImportRequest)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    imported = 0
    for item in body.items:
        amt = abs(float(item.amount))
        if amt <= 0:
            continue
        await create_transaction_atomic(
            user_uuid=user_uuid,
            account_id=body.account_id,
            to_account_id=None,
            category_id=item.category_id,
            amount=amt,
            target_amount=None,
            tx_type=item.type,
            note=item.note or "Імпорт з виписки",
            transaction_date=item.date,
        )
        imported += 1
    return web.json_response({"status": "success", "imported": imported})


# --- CASHFLOW CALENDAR (PRO+) ---

async def handle_get_calendar(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    now = datetime.now(timezone.utc)
    year = int(request.query.get("year") or now.year)
    month = int(request.query.get("month") or now.month)
    month_prefix = f"{year:04d}-{month:02d}"

    def _load():
        events = []
        try:
            recs = supabase.table("recurring_payments").select("*").eq("user_id", user_uuid).eq("is_active", True).execute().data or []
            for r in recs:
                day = min(int(r.get("day_of_month") or 1), 28)
                events.append(
                    {
                        "date": f"{month_prefix}-{day:02d}",
                        "kind": "recurring",
                        "title": f"🔁 {r['title']}",
                        "amount": float(r["amount"]),
                        "type": r.get("type", "expense"),
                    }
                )
        except Exception:
            pass

        try:
            goals = supabase.table("goals").select("title, deadline, target_amount, current_amount, currency").eq("user_id", user_uuid).execute().data or []
            for g in goals:
                dl = str(g.get("deadline") or "")
                if dl.startswith(month_prefix):
                    events.append(
                        {
                            "date": dl[:10],
                            "kind": "goal",
                            "title": f"🎯 Дедлайн цілі: {g['title']}",
                            "amount": float(g["target_amount"]) - float(g["current_amount"]),
                            "currency": g.get("currency", "UAH"),
                        }
                    )
        except Exception:
            pass

        try:
            debts = supabase.table("debts").select("*").eq("user_id", user_uuid).eq("is_settled", False).execute().data or []
            for d in debts:
                dd = str(d.get("due_date") or "")
                if dd.startswith(month_prefix):
                    events.append(
                        {
                            "date": dd[:10],
                            "kind": "debt",
                            "title": f"🤝 {'Отримати від' if d['direction'] == 'owed_to_me' else 'Повернути'} {d['person_name']}",
                            "amount": float(d["amount"]),
                            "currency": d.get("currency", "UAH"),
                        }
                    )
        except Exception:
            pass

        try:
            invs = supabase.table("investments").select("name, ticker, maturity_date, quantity, buy_price_avg, currency").eq("user_id", user_uuid).execute().data or []
            for inv in invs:
                md = str(inv.get("maturity_date") or "")
                if md.startswith(month_prefix):
                    events.append(
                        {
                            "date": md[:10],
                            "kind": "maturity",
                            "title": f"🏦 Погашення: {inv.get('name') or inv.get('ticker')}",
                            "amount": float(inv.get("quantity") or 1) * float(inv.get("buy_price_avg") or 0),
                            "currency": inv.get("currency", "UAH"),
                        }
                    )
        except Exception:
            pass

        events.sort(key=lambda x: x["date"])
        return events

    events = await run_db(_load)
    return web.json_response({"year": year, "month": month, "events": events})
