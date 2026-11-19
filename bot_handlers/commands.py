import logging
from datetime import datetime, timezone
from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp

from config import MINIAPP_URL
from db import supabase, run_db, get_user_uuid_by_tg_id, get_exchange_rates

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if not user:
        return
    chat_id = message.chat.id

    def _upsert():
        u = (
            supabase.table("users")
            .upsert(
                {"telegram_id": user.id, "username": user.username, "first_name": user.first_name},
                on_conflict="telegram_id",
            )
            .execute()
            .data[0]
        )
        uid = u["id"]
        accs = supabase.table("accounts").select("id").eq("user_id", uid).execute().data
        if not accs:
            supabase.table("accounts").insert(
                {
                    "user_id": uid,
                    "name": "Основна картка",
                    "account_type": "card",
                    "currency": "UAH",
                    "balance": 0.00,
                }
            ).execute()
        return uid

    await run_db(_upsert)

    try:
        await message.bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text="📊 Капітал", web_app=WebAppInfo(url=MINIAPP_URL)),
        )
    except Exception as e:
        logging.warning(f"Menu button err: {e}")

    await message.answer(
        f"Вітаю, {user.first_name}! 💼\n\n"
        "<b>FinTrack Pro</b> готовий до роботи:\n\n"
        "⚡ <b>Швидкий запис у чаті:</b>\n"
        "• <code>Кава 75</code> або <code>Сільпо 420</code>\n"
        "• <code>Таксі 5 USD</code> (з автоконвертацією або вибором рахунку)\n"
        "• <code>+25000 Зарплата</code>\n\n"
        "📋 <b>Команди:</b>\n"
        "• /balance — залишки на рахунках\n"
        "• /today — доходи та витрати за день (у UAH)\n"
        "• /month — повний звіт за поточний місяць (у UAH)\n\n"
        "Відкривайте додаток кнопкою <b>«Капітал»</b> зліва внизу 🚀",
        parse_mode="HTML",
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    accs = await run_db(
        lambda: supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []
    )
    rates = await get_exchange_rates()

    text = "💳 <b>Залишки на ваших рахунках:</b>\n\n"
    total_uah = 0.0
    for a in accs:
        b = float(a["balance"])
        curr = str(a.get("currency", "UAH")).upper()
        rate = rates.get(curr, 1.0)
        total_uah += b * rate
        equiv = f" (~{b * rate:,.2f} грн)" if curr != "UAH" else ""
        text += f"• {a['name']}: <b>{b:,.2f} {curr}</b>{equiv}\n"

    text += f"\n💰 <b>Сумарний капітал рахунків: {total_uah:,.2f} UAH</b>"
    await message.answer(text, parse_mode="HTML")


async def _calc_period_summary(user_uuid: str, start_iso: str) -> tuple[float, float, int]:
    def _load():
        txs = (
            supabase.table("transactions")
            .select("amount, type, account_id")
            .eq("user_id", user_uuid)
            .gte("transaction_date", start_iso)
            .execute()
            .data
            or []
        )
        accs = supabase.table("accounts").select("id, currency").eq("user_id", user_uuid).execute().data or []
        return txs, accs

    txs, accs = await run_db(_load)
    rates = await get_exchange_rates()
    acc_curr = {a["id"]: str(a.get("currency", "UAH")).upper() for a in accs}

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
    return inc, exp, len(txs)


@router.message(Command("today"))
async def cmd_today(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    inc, exp, _ = await _calc_period_summary(user_uuid, today_start)

    await message.answer(
        f"📅 <b>Підсумок за сьогодні (у UAH):</b>\n\n"
        f"🟢 Доходи: +{inc:,.2f} UAH\n"
        f"🔴 Витрати: -{exp:,.2f} UAH\n"
        f"💰 Чистий результат: <b>{(inc - exp):,.2f} UAH</b>",
        parse_mode="HTML",
    )


@router.message(Command("month"))
async def cmd_month(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку запустіть бота через /start")
        return

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    income, expense, count = await _calc_period_summary(user_uuid, month_start)
    net = income - expense
    month_name = now.strftime("%B %Y")

    await message.answer(
        f"📊 <b>Фінансовий звіт за {month_name} (у UAH):</b>\n\n"
        f"🟢 Доходи: <b>+{income:,.2f} UAH</b>\n"
        f"🔴 Витрати: <b>-{expense:,.2f} UAH</b>\n"
        f"────────────────────\n"
        f"💵 Чисте сальдо: <b>{'+' if net >= 0 else ''}{net:,.2f} UAH</b>\n"
        f"📝 Операцій: <b>{count}</b>",
        parse_mode="HTML",
    )
