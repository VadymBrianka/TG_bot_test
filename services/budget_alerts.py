import logging
from datetime import datetime, timezone
from aiogram import Bot
from db import supabase, run_db, get_exchange_rates


async def check_budget_threshold(bot: Bot, user_uuid: str, tg_id: int, category_id: str | None) -> None:
    """Check if user's monthly spending in category_id reached >=80% or >=100% of budget."""
    if not category_id:
        return
    try:
        def _get_budget():
            u_res = supabase.table("users").select("budget_alerts_enabled").eq("id", user_uuid).limit(1).execute().data
            if u_res and u_res[0].get("budget_alerts_enabled") is False:
                return None
            res = (
                supabase.table("budgets")
                .select("*")
                .eq("user_id", user_uuid)
                .eq("category_id", category_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None

        budget = await run_db(_get_budget)
        if not budget:
            return

        limit_val = float(budget.get("monthly_limit") or 0)
        if limit_val <= 0:
            return
        budget_curr = str(budget.get("currency") or "UAH").upper()

        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        def _get_month_data():
            txs = (
                supabase.table("transactions")
                .select("amount, account_id")
                .eq("user_id", user_uuid)
                .eq("type", "expense")
                .eq("category_id", category_id)
                .gte("transaction_date", month_start)
                .execute()
                .data
                or []
            )
            accs = supabase.table("accounts").select("id, currency").eq("user_id", user_uuid).execute().data or []
            cat = supabase.table("categories").select("name, icon").eq("id", category_id).limit(1).execute().data
            return txs, accs, (cat[0] if cat else {"name": "Категорія", "icon": "🏷️"})

        txs, accs, cat_info = await run_db(_get_month_data)
        rates = await get_exchange_rates()
        acc_curr_map = {a["id"]: str(a.get("currency") or "UAH").upper() for a in accs}

        spent_uah = sum(
            float(t.get("amount") or 0) * rates.get(acc_curr_map.get(t.get("account_id"), "UAH"), 1.0)
            for t in txs
        )
        limit_uah = limit_val * rates.get(budget_curr, 1.0)
        if limit_uah <= 0:
            return

        pct = (spent_uah / limit_uah) * 100
        if pct >= 100:
            await bot.send_message(
                tg_id,
                f"🚨 <b>Перевищено бюджет!</b>\n"
                f"{cat_info.get('icon', '🏷️')} <b>{cat_info.get('name')}</b>: "
                f"витрачено {spent_uah / rates.get(budget_curr, 1.0):,.2f} / {limit_val:,.2f} {budget_curr} ({pct:.0f}%)",
                parse_mode="HTML",
            )
        elif pct >= 80:
            await bot.send_message(
                tg_id,
                f"⚠️ <b>Увага, бюджет майже вичерпано!</b>\n"
                f"{cat_info.get('icon', '🏷️')} <b>{cat_info.get('name')}</b>: "
                f"витрачено {spent_uah / rates.get(budget_curr, 1.0):,.2f} / {limit_val:,.2f} {budget_curr} ({pct:.0f}%)",
                parse_mode="HTML",
            )
    except Exception as e:
        logging.debug(f"Budget check skipped: {e}")
