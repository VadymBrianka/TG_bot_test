import logging
from datetime import datetime, timezone
from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp, BufferedInputFile, BotCommand, BotCommandScopeChat

from config import MINIAPP_URL
from db import supabase, run_db, get_user_uuid_by_tg_id, get_exchange_rates, is_admin
from services.export_csv import build_transactions_csv

router = Router()

USER_COMMANDS = [
    BotCommand(command="start", description="Запуск та відкриття Mini App"),
    BotCommand(command="balance", description="Залишки на рахунках"),
    BotCommand(command="today", description="Доходи та витрати за сьогодні"),
    BotCommand(command="month", description="Фінансовий звіт за місяць"),
    BotCommand(command="stats", description="Топ-5 категорій витрат за місяць"),
    BotCommand(command="debts", description="Борги та позики"),
    BotCommand(command="debt", description="Додати борг: /debt мені Олег 500 UAH"),
    BotCommand(command="goals", description="Мої цілі накопичення"),
    BotCommand(command="portfolio", description="Активи, депозити та ОВДП"),
    BotCommand(command="budgets", description="Місячні ліміти (бюджети)"),
    BotCommand(command="budget", description="Встановити бюджет: /budget Продукти 10000"),
    BotCommand(command="alerts", description="Мої алерти цін"),
    BotCommand(command="alert", description="Створити алерт: /alert BTC > 100000"),
    BotCommand(command="recurring", description="Регулярні платежі"),
    BotCommand(command="export", description="Отримати CSV-виписку в чат"),
    BotCommand(command="help", description="Довідка по командах і швидкому запису"),
]

ADMIN_EXTRA_COMMANDS = [
    BotCommand(command="admin", description="🛡️ Панель адміністратора"),
    BotCommand(command="broadcast", description="📢 Розсилка всім користувачам"),
    BotCommand(command="setrole", description="👤 Змінити роль користувача"),
]


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
            menu_button=MenuButtonWebApp(text="💎 Капітал", web_app=WebAppInfo(url=MINIAPP_URL)),
        )
        if await is_admin(user.id):
            await message.bot.set_my_commands(
                USER_COMMANDS + ADMIN_EXTRA_COMMANDS,
                scope=BotCommandScopeChat(chat_id=chat_id),
            )
    except Exception as e:
        logging.warning(f"Menu/commands err: {e}")

    await message.answer(
        f"Вітаю, {user.first_name}! 💼\n\n"
        "<b>FinTrack Pro</b> готовий до роботи:\n\n"
        "⚡ <b>Швидкий запис у чаті:</b>\n"
        "• <code>Кава 75</code> або <code>Сільпо 420</code>\n"
        "• <code>Таксі 5 USD</code> (з автоконвертацією або вибором рахунку)\n"
        "• <code>+25000 Зарплата</code>\n\n"
        "📋 <b>Основні команди:</b>\n"
        "• /balance, /today, /month\n"
        "• /goals, /portfolio, /budgets, /recurring\n"
        "• /alerts, /export, /help\n\n"
        "Відкривайте додаток кнопкою <b>«Капітал»</b> зліва внизу 🚀",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    admin_hint = "\n\n🛡️ <b>Адмін:</b> /admin, /broadcast, /setrole" if (message.from_user and await is_admin(message.from_user.id)) else ""
    await message.answer(
        "📖 <b>Довідка FinTrack Hub</b>\n\n"
        "⚡ <b>Швидкий запис:</b>\n"
        "• <code>Кава 75</code> — витрата на основний рахунок\n"
        "• <code>Uber 5 USD</code> — витрата в іншій валюті\n"
        "• <code>+25000 Зарплата</code> — дохід\n\n"
        "📊 <b>Звіти та дані:</b>\n"
        "• /balance — залишки по рахунках\n"
        "• /today — підсумок за сьогодні (UAH)\n"
        "• /month — звіт за місяць (UAH)\n"
        "• /goals — цілі накопичення\n"
        "• /portfolio — портфель активів\n"
        "• /budgets — місячні бюджети\n"
        "• /budget <code>Продукти 10000 UAH</code> — встановити ліміт\n"
        "• /alerts — активні алерти цін\n"
        "• /alert <code>BTC &gt; 100000</code> — створити алерт ціни\n"
        "• /recurring — регулярні платежі\n"
        "• /export — надіслати CSV-виписку в чат"
        + admin_hint,
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


@router.message(Command("goals"))
async def cmd_goals(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    goals = await run_db(
        lambda: supabase.table("goals").select("*").eq("user_id", user_uuid).order("created_at").execute().data or []
    )
    if not goals:
        await message.answer("🎯 У вас ще немає цілей. Додайте їх у Mini App!")
        return

    lines = ["🎯 <b>Ваші цілі накопичення:</b>\n"]
    for g in goals:
        cur = float(g.get("current_amount") or 0)
        tgt = float(g.get("target_amount") or 1)
        pct = round((cur / tgt) * 100)
        dl = f" (до {g['deadline']})" if g.get("deadline") else ""
        lines.append(f"• <b>{g['title']}</b>: {cur:,.2f} / {tgt:,.2f} {g.get('currency', 'UAH')} (<b>{pct}%</b>){dl}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    invs = await run_db(
        lambda: supabase.table("investments").select("*").eq("user_id", user_uuid).execute().data or []
    )
    if not invs:
        await message.answer("📈 Портфель активів порожній.")
        return

    rates = await get_exchange_rates()
    lines = ["📈 <b>Портфель активів:</b>\n"]
    total_uah = 0.0
    for inv in invs:
        qty = float(inv.get("quantity") or 1)
        price = float(inv.get("current_price") or inv.get("buy_price_avg") or 0)
        curr = str(inv.get("currency") or "USD").upper()
        val = qty * price
        total_uah += val * rates.get(curr, 1.0)
        lines.append(f"• <b>{inv.get('name') or inv.get('ticker')}</b>: {val:,.2f} {curr}")

    lines.append(f"\n💰 <b>Разом: ~{total_uah:,.2f} UAH</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("budgets"))
async def cmd_budgets(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    try:
        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        def _load():
            b = supabase.table("budgets").select("*").eq("user_id", user_uuid).execute().data or []
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
            return b, txs, accs, cats

        budgets, txs, accs, cats = await run_db(_load)
        if not budgets:
            await message.answer("📊 Бюджети ще не задані.\nПриклад: <code>/budget Продукти 10000</code>", parse_mode="HTML")
            return
        rates = await get_exchange_rates()
        acc_curr = {a["id"]: str(a.get("currency", "UAH")).upper() for a in accs}
        cat_map = {c["id"]: c for c in cats}
        spent_uah: dict[str, float] = {}
        for t in txs:
            cid = t.get("category_id")
            if cid:
                spent_uah[cid] = spent_uah.get(cid, 0.0) + float(t["amount"]) * rates.get(acc_curr.get(t.get("account_id"), "UAH"), 1.0)

        lines = ["📊 <b>Місячні бюджети:</b>\n"]
        for b in budgets:
            c = cat_map.get(b["category_id"], {})
            curr = str(b.get("currency") or "UAH").upper()
            rate = rates.get(curr, 1.0)
            spent = spent_uah.get(b["category_id"], 0.0) / rate
            lim = float(b["monthly_limit"])
            pct = round((spent / (lim or 1)) * 100)
            lines.append(f"• {c.get('icon', '🏷️')} <b>{c.get('name', 'Категорія')}</b>: {spent:,.0f} / {lim:,.0f} {curr} (<b>{pct}%</b>)")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.answer("⚠️ Таблиця <code>budgets</code> ще не створена в БД (виконайте <code>migrations.sql</code>).", parse_mode="HTML")


@router.message(Command("budget"))
async def cmd_set_budget(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply("Формат: <code>/budget Продукти 10000 [UAH]</code>", parse_mode="HTML")
        return

    cat_query = parts[1].lower()
    try:
        limit_val = float(parts[2].replace(",", "."))
    except ValueError:
        await message.reply("Сума має бути числом.")
        return
    curr = parts[3].upper() if len(parts) > 3 else "UAH"

    cats = await run_db(lambda: supabase.table("categories").select("id, name, icon, type").eq("type", "expense").execute().data or [])
    matched = next((c for c in cats if cat_query in str(c["name"]).lower()), None)
    if not matched:
        await message.reply(f"Категорію витрат «{parts[1]}» не знайдено.")
        return

    try:
        await run_db(
            lambda: supabase.table("budgets")
            .upsert(
                {"user_id": user_uuid, "category_id": matched["id"], "monthly_limit": limit_val, "currency": curr},
                on_conflict="user_id,category_id",
            )
            .execute()
        )
        await message.reply(
            f"✅ Бюджет для {matched.get('icon', '🏷️')} <b>{matched['name']}</b> встановлено: <b>{limit_val:,.2f} {curr}/міс</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.reply(f"❌ Не вдалося зберегти бюджет: {e}")


@router.message(Command("alerts"))
async def cmd_alerts(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    try:
        items = await run_db(
            lambda: supabase.table("price_alerts").select("*").eq("user_id", user_uuid).order("created_at", desc=True).execute().data or []
        )
        if not items:
            await message.answer("🚨 Алертів цін немає.\nПриклад: <code>/alert BTC &gt; 100000</code>", parse_mode="HTML")
            return
        lines = ["🚨 <b>Ваші алерти цін:</b>\n"]
        for a in items:
            sign = "≥" if a["condition"] == "above" else "≤"
            st = "✅ спрацював" if a.get("is_triggered") else "⏳ активний"
            lines.append(f"• <b>{a['ticker']}</b> {sign} ${float(a['target_price']):,.2f} ({st})")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.answer("⚠️ Таблиця <code>price_alerts</code> ще не створена в БД (виконайте <code>migrations.sql</code>).", parse_mode="HTML")


@router.message(Command("alert"))
async def cmd_create_alert(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    parts = (message.text or "").split()
    if len(parts) != 4 or parts[2] not in (">", "<", "above", "below"):
        await message.reply("Формат: <code>/alert BTC &gt; 100000</code> або <code>/alert AAPL &lt; 200</code>", parse_mode="HTML")
        return

    ticker = parts[1].upper()
    cond = "above" if parts[2] in (">", "above") else "below"
    try:
        target = float(parts[3].replace(",", "."))
    except ValueError:
        await message.reply("Ціна має бути числом.")
        return

    crypto_set = {"BTC", "ETH", "SOL", "TON", "BNB", "XRP", "DOGE", "ADA", "USDT"}
    asset_class = "crypto" if ticker in crypto_set else "stock"
    try:
        await run_db(
            lambda: supabase.table("price_alerts")
            .insert({"user_id": user_uuid, "ticker": ticker, "asset_class": asset_class, "condition": cond, "target_price": target})
            .execute()
        )
        await message.reply(f"✅ Алерт створено: <b>{ticker}</b> {'≥' if cond == 'above' else '≤'} ${target:,.2f}", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ Не вдалося створити алерт: {e}")


@router.message(Command("recurring"))
async def cmd_recurring(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    try:
        items = await run_db(
            lambda: supabase.table("recurring_payments").select("*").eq("user_id", user_uuid).eq("is_active", True).execute().data or []
        )
        if not items:
            await message.answer("🔁 Регулярних платежів ще немає. Додайте їх у Mini App → Налаштування.")
            return
        lines = ["🔁 <b>Регулярні платежі:</b>\n"]
        for r in items:
            sign = "+" if r.get("type") == "income" else "-"
            lines.append(f"• <b>{r['title']}</b>: {sign}{float(r['amount']):,.2f} ({r['day_of_month']}-го числа, наст. {r['next_run_date']})")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.answer("⚠️ Таблиця <code>recurring_payments</code> ще не створена в БД (виконайте <code>migrations.sql</code>).", parse_mode="HTML")


@router.message(Command("export"))
async def cmd_export(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    csv_text = await build_transactions_csv(user_uuid, days=365)
    filename = f"fintrack_export_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    doc = BufferedInputFile(csv_text.encode("utf-8"), filename=filename)
    await message.answer_document(doc, caption="📊 <b>Ваш експорт транзакцій за останні 365 днів</b>", parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    def _load():
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
        return txs, accs, cats

    txs, accs, cats = await run_db(_load)
    if not txs:
        await message.answer("📊 За цей місяць витрат ще немає.")
        return

    rates = await get_exchange_rates()
    acc_curr = {a["id"]: str(a.get("currency") or "UAH").upper() for a in accs}
    cat_map = {c["id"]: c for c in cats}

    by_cat: dict[str, float] = {}
    total_uah = 0.0
    for t in txs:
        uah = float(t["amount"]) * rates.get(acc_curr.get(t.get("account_id"), "UAH"), 1.0)
        total_uah += uah
        cid = t.get("category_id") or "other"
        by_cat[cid] = by_cat.get(cid, 0.0) + uah

    top = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)[:5]
    lines = ["🏆 <b>Топ-5 категорій витрат за місяць (UAH):</b>\n"]
    for cid, val in top:
        c = cat_map.get(cid, {"name": "Інше", "icon": "⚪"})
        pct = round((val / (total_uah or 1)) * 100)
        lines.append(f"• {c.get('icon', '🏷️')} <b>{c['name']}</b>: {val:,.0f} UAH (<b>{pct}%</b>)")
    lines.append(f"\n💸 <b>Всього витрачено: {total_uah:,.0f} UAH</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("debts"))
async def cmd_debts(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    try:
        rows = await run_db(
            lambda: supabase.table("debts")
            .select("*")
            .eq("user_id", user_uuid)
            .eq("is_settled", False)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        if not rows:
            await message.answer("🤝 Активних боргів немає.\nДодати: <code>/debt мені Олег 500 UAH</code>", parse_mode="HTML")
            return
        lines = ["🤝 <b>Активні борги та позики:</b>\n"]
        for d in rows:
            badge = "🟢 Вам винні:" if d["direction"] == "owed_to_me" else "🔴 Ви винні:"
            note = f" — <i>{d['note']}</i>" if d.get("note") else ""
            lines.append(f"{badge} <b>{d['person_name']}</b> — <b>{float(d['amount']):,.2f} {d['currency']}</b>{note}")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception:
        await message.answer("⚠️ Таблиця <code>debts</code> ще не створена в БД (виконайте <code>migrations.sql</code>).", parse_mode="HTML")


@router.message(Command("debt"))
async def cmd_add_debt(message: Message):
    if not message.from_user:
        return
    user_uuid = await get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return
    parts = (message.text or "").split(maxsplit=5)
    if len(parts) < 4:
        await message.reply("Формат: <code>/debt мені Олег 500 [UAH] [примітка]</code> або <code>/debt я Аня 200</code>", parse_mode="HTML")
        return

    dir_word = parts[1].lower()
    direction = "owed_to_me" if dir_word in ("мені", "мне", "me", "+") else "i_owe"
    person = parts[2]
    try:
        amount = float(parts[3].replace(",", "."))
    except ValueError:
        await message.reply("Сума має бути числом.")
        return
    curr = parts[4].upper() if len(parts) > 4 and len(parts[4]) <= 5 and parts[4].isalpha() else "UAH"
    note = parts[5] if len(parts) > 5 else (parts[4] if len(parts) > 4 and curr == "UAH" and not parts[4].isalpha() else "")

    try:
        await run_db(
            lambda: supabase.table("debts")
            .insert({"user_id": user_uuid, "direction": direction, "person_name": person, "amount": amount, "currency": curr, "note": note})
            .execute()
        )
        label = "Вам винен" if direction == "owed_to_me" else "Ви винні"
        await message.reply(f"🤝 Записано! {label} <b>{person}</b>: <b>{amount:,.2f} {curr}</b>", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ Помилка: {e}")
