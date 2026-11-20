import asyncio
import logging
from datetime import datetime, timedelta, timezone, date
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db import supabase, run_db, get_exchange_rates
from services.nbu import update_rates_from_nbu
from services.market_quotes import get_realtime_price
from services.subscriptions import downgrade_expired_subscriptions


async def _check_price_alerts(bot: Bot) -> None:
    try:
        alerts = await run_db(
            lambda: supabase.table("price_alerts").select("*").eq("is_triggered", False).execute().data or []
        )
        if not alerts:
            return

        user_ids = list({a["user_id"] for a in alerts})
        users_data = await run_db(
            lambda: supabase.table("users").select("id, telegram_id").in_("id", user_ids).execute().data or []
        )
        user_tg_map = {u["id"]: u["telegram_id"] for u in users_data}

        for alert in alerts:
            ticker = str(alert.get("ticker", "")).upper()
            asset_class = str(alert.get("asset_class", "crypto"))
            target = float(alert.get("target_price") or 0)
            cond = alert.get("condition", "above")
            price = await get_realtime_price(ticker, asset_class, use_cache=True)
            if not price or price <= 0:
                continue

            triggered = (cond == "above" and price >= target) or (cond == "below" and price <= target)
            if triggered:
                await run_db(
                    lambda aid=alert["id"]: supabase.table("price_alerts")
                    .update({"is_triggered": True})
                    .eq("id", aid)
                    .execute()
                )
                tg_id = user_tg_map.get(alert["user_id"])
                if tg_id:
                    arrow = "📈" if cond == "above" else "📉"
                    cond_txt = "досягла / перевищила" if cond == "above" else "впала до / нижче"
                    try:
                        await bot.send_message(
                            tg_id,
                            f"{arrow} <b>Алерт ціни: {ticker}</b>\n"
                            f"Поточна ціна: <b>${price:,.4f}</b> ({cond_txt} ціль ${target:,.4f})",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
    except Exception as e:
        logging.debug(f"Price alerts check skipped: {e}")


async def _run_daily_notifications(bot: Bot) -> None:
    today = datetime.now(timezone.utc).date()
    today_str = today.isoformat()
    soon_str = (today + timedelta(days=3)).isoformat()

    # 1. Recurring payments due today
    try:
        due = await run_db(
            lambda: supabase.table("recurring_payments")
            .select("*")
            .eq("is_active", True)
            .lte("next_run_date", today_str)
            .execute()
            .data
            or []
        )
        if due:
            user_ids = list({r["user_id"] for r in due})
            users_data = await run_db(
                lambda: supabase.table("users").select("id, telegram_id").in_("id", user_ids).execute().data or []
            )
            user_tg_map = {u["id"]: u["telegram_id"] for u in users_data}
            for item in due:
                tg_id = user_tg_map.get(item["user_id"])
                if not tg_id:
                    continue
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Підтвердити",
                                callback_data=f"rec_ok:{item['id']}",
                            ),
                            InlineKeyboardButton(
                                text="⏭ Пропустити",
                                callback_data=f"rec_skip:{item['id']}",
                            ),
                        ]
                    ]
                )
                sign = "+" if item.get("type") == "income" else "-"
                try:
                    await bot.send_message(
                        tg_id,
                        f"🔔 <b>Регулярний платіж:</b> {item['title']}\n"
                        f"Сума: <b>{sign}{float(item['amount']):,.2f}</b>",
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as e:
        logging.debug(f"Recurring check skipped: {e}")

    # 2. Goal deadlines & Investment maturity dates within 3 days
    try:
        users = await run_db(lambda: supabase.table("users").select("*").execute().data or [])
        for u in users:
            if u.get("notifications_enabled") is False:
                continue
            tg_id = u.get("telegram_id")
            uid = u.get("id")
            if not tg_id or not uid:
                continue

            goals = await run_db(
                lambda u_id=uid: supabase.table("goals")
                .select("title, target_amount, current_amount, currency, deadline")
                .eq("user_id", u_id)
                .gte("deadline", today_str)
                .lte("deadline", soon_str)
                .execute()
                .data
                or []
            )
            for g in goals:
                if float(g.get("current_amount") or 0) < float(g.get("target_amount") or 0):
                    try:
                        await bot.send_message(
                            tg_id,
                            f"⏳ <b>Наближається дедлайн цілі:</b> {g['title']}\n"
                            f"Зібрано {float(g['current_amount']):,.2f} / {float(g['target_amount']):,.2f} {g['currency']}\n"
                            f"Дедлайн: <b>{g['deadline']}</b>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

            invs = await run_db(
                lambda u_id=uid: supabase.table("investments")
                .select("name, ticker, asset_class, quantity, buy_price_avg, currency, maturity_date")
                .eq("user_id", u_id)
                .gte("maturity_date", today_str)
                .lte("maturity_date", soon_str)
                .execute()
                .data
                or []
            )
            for inv in invs:
                try:
                    await bot.send_message(
                        tg_id,
                        f"🏦 <b>Наближається дата погашення:</b> {inv.get('name') or inv.get('ticker')}\n"
                        f"Дата: <b>{inv['maturity_date']}</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except Exception as e:
        logging.debug(f"Deadline/maturity check skipped: {e}")

    # 3. Weekly digest on Mondays
    if today.weekday() == 0:
        try:
            rates = await get_exchange_rates()
            week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            users = await run_db(lambda: supabase.table("users").select("*").execute().data or [])
            for u in users:
                if u.get("weekly_digest") is False:
                    continue
                tg_id = u.get("telegram_id")
                uid = u.get("id")
                if not tg_id or not uid:
                    continue
                accs = await run_db(
                    lambda u_id=uid: supabase.table("accounts").select("id, currency").eq("user_id", u_id).execute().data or []
                )
                acc_curr = {a["id"]: str(a.get("currency") or "UAH").upper() for a in accs}
                txs = await run_db(
                    lambda u_id=uid: supabase.table("transactions")
                    .select("amount, type, account_id")
                    .eq("user_id", u_id)
                    .gte("transaction_date", week_ago)
                    .execute()
                    .data
                    or []
                )
                inc = sum(
                    float(t["amount"]) * rates.get(acc_curr.get(t.get("account_id"), "UAH"), 1.0)
                    for t in txs
                    if t.get("type") == "income"
                )
                exp = sum(
                    float(t["amount"]) * rates.get(acc_curr.get(t.get("account_id"), "UAH"), 1.0)
                    for t in txs
                    if t.get("type") == "expense"
                )
                if inc == 0 and exp == 0:
                    continue
                try:
                    await bot.send_message(
                        tg_id,
                        f"📬 <b>Тижневий фінансовий дайджест:</b>\n\n"
                        f"🟢 Доходи: <b>+{inc:,.2f} UAH</b>\n"
                        f"🔴 Витрати: <b>-{exp:,.2f} UAH</b>\n"
                        f"💵 Сальдо: <b>{(inc - exp):+,.2f} UAH</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception as e:
            logging.debug(f"Weekly digest skipped: {e}")


async def start_scheduler(bot: Bot) -> None:
    """Periodic background scheduler for NBU rates (6h), price alerts (15m), and daily digests/reminders."""
    last_nbu_ts = 0.0
    last_daily_date: date | None = None

    while True:
        try:
            now_mono = asyncio.get_running_loop().time()
            if now_mono - last_nbu_ts >= 6 * 3600:
                await update_rates_from_nbu()
                last_nbu_ts = now_mono

            await _check_price_alerts(bot)
            await downgrade_expired_subscriptions(bot)

            today = datetime.now(timezone.utc).date()
            if last_daily_date != today:
                await _run_daily_notifications(bot)
                last_daily_date = today
        except Exception as e:
            logging.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(15 * 60)
