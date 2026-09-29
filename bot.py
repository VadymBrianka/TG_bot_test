import os
import json
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    MenuButtonWebApp
)
from aiogram.filters import CommandStart, Command
from supabase import create_client, Client

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL")
SERVER_PORT = int(os.getenv("PORT", 8080))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not BOT_TOKEN or not MINIAPP_URL or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Перевірте BOT_TOKEN, MINIAPP_URL, SUPABASE_URL та SUPABASE_KEY у середовищі!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- ДОПОМІЖНІ ФУНКЦІЇ АВТОРИЗАЦІЇ ---

def validate_init_data(init_data: str, token: str) -> dict | None:
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calculated_hash, received_hash):
            return parsed_data
        return None
    except Exception as e:
        logging.error(f"Помилка перевірки initData: {e}")
        return None

def get_user_uuid_by_tg_id(tg_id: int) -> str | None:
    res = supabase.table("users").select("id").eq("telegram_id", tg_id).execute()
    if res.data:
        return res.data[0]["id"]
    return None

# --- CORS MIDDLEWARE ---

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, ngrok-skip-browser-warning"
    return response

# --- HTTP ЕНДПОІНТИ (API ДЛЯ MINI APP) ---

async def handle_health(request: web.Request):
    return web.Response(text="FinTrack Engine is live! 🚀", status=200)

async def handle_dashboard_summary(request: web.Request):
    try:
        tg_id_param = request.query.get("telegram_id")
        if not tg_id_param:
            return web.json_response({"error": "telegram_id is required"}, status=400)
        
        tg_id = int(tg_id_param)
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        # Рахунки, інвестиції, цілі, останні транзакції
        accs = supabase.table("accounts").select("*").eq("user_id", user_uuid).execute().data
        invs = supabase.table("investments").select("*").eq("user_id", user_uuid).execute().data
        goals = supabase.table("goals").select("*").eq("user_id", user_uuid).execute().data
        txs = supabase.table("transactions").select("*, categories(name, icon)")\
            .eq("user_id", user_uuid).order("transaction_date", desc=True).limit(20).execute().data

        rates_res = supabase.table("exchange_rates").select("*").execute().data
        rates = {r["currency"]: float(r["rate_to_base"]) for r in rates_res}

        # Розрахунок капіталу в базовій валюті (UAH)
        total_accounts_uah = sum(float(a["balance"]) * rates.get(a["currency"], 1.0) for a in accs)
        total_investments_uah = sum(
            float(i["quantity"]) * float(i["current_price"]) * rates.get(i["currency"], 1.0) 
            for i in invs
        )
        net_worth = total_accounts_uah + total_investments_uah

        return web.json_response({
            "accounts": accs,
            "investments": invs,
            "goals": goals,
            "transactions": txs,
            "summary": {
                "net_worth": round(net_worth, 2),
                "accounts_total_uah": round(total_accounts_uah, 2),
                "investments_total_uah": round(total_investments_uah, 2)
            }
        })
    except Exception as e:
        logging.error(f"Dashboard API Error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_create_transaction(request: web.Request):
    try:
        data = await request.json()
        init_data = data.get("initData")
        tg_id = data.get("telegram_id")

        if init_data:
            val = validate_init_data(init_data, BOT_TOKEN)
            if val:
                u_info = json.loads(val.get("user", "{}"))
                tg_id = u_info.get("id")

        if not tg_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        user_uuid = get_user_uuid_by_tg_id(int(tg_id))
        if not user_uuid:
            return web.json_response({"error": "User not registered"}, status=404)

        amount = float(data.get("amount", 0))
        tx_type = data.get("type", "expense")
        account_id = data.get("account_id")
        to_account_id = data.get("to_account_id")
        target_amount = float(data.get("target_amount", amount)) if to_account_id else None
        note = data.get("note", "")

        # Зміна балансів рахунків
        if tx_type == "expense":
            acc = supabase.table("accounts").select("balance").eq("id", account_id).execute().data[0]
            new_bal = float(acc["balance"]) - amount
            supabase.table("accounts").update({"balance": new_bal}).eq("id", account_id).execute()
        elif tx_type == "income":
            acc = supabase.table("accounts").select("balance").eq("id", account_id).execute().data[0]
            new_bal = float(acc["balance"]) + amount
            supabase.table("accounts").update({"balance": new_bal}).eq("id", account_id).execute()
        elif tx_type == "transfer" and to_account_id:
            acc_from = supabase.table("accounts").select("balance").eq("id", account_id).execute().data[0]
            acc_to = supabase.table("accounts").select("balance").eq("id", to_account_id).execute().data[0]
            supabase.table("accounts").update({"balance": float(acc_from["balance"]) - amount}).eq("id", account_id).execute()
            supabase.table("accounts").update({"balance": float(acc_to["balance"]) + target_amount}).eq("id", to_account_id).execute()

        # Фіксація транзакції
        supabase.table("transactions").insert({
            "user_id": user_uuid,
            "account_id": account_id,
            "to_account_id": to_account_id,
            "amount": amount,
            "target_amount": target_amount,
            "type": tx_type,
            "note": note
        }).execute()

        # Нотифікація в чат бота
        type_str = "🔴 Витрата" if tx_type == "expense" else ("🟢 Дохід" if tx_type == "income" else "🔄 Переказ")
        await bot.send_message(
            chat_id=int(tg_id),
            text=f"{type_str}: <b>{amount:,.2f}</b>\nНотатка: <i>{note or 'Без опису'}</i>",
            parse_mode="HTML"
        )

        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Transaction API Error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_create_account(request: web.Request):
    try:
        data = await request.json()
        tg_id = int(data.get("telegram_id"))
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        new_acc = supabase.table("accounts").insert({
            "user_id": user_uuid,
            "name": data.get("name"),
            "account_type": data.get("account_type", "card"),
            "currency": data.get("currency", "UAH"),
            "balance": float(data.get("balance", 0.0))
        }).execute()

        return web.json_response({"status": "success", "account": new_acc.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_create_investment(request: web.Request):
    try:
        data = await request.json()
        tg_id = int(data.get("telegram_id"))
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        qty = float(data.get("quantity", 0))
        buy_price = float(data.get("buy_price_avg", 0))
        cur_price = float(data.get("current_price", buy_price))

        new_inv = supabase.table("investments").insert({
            "user_id": user_uuid,
            "ticker": data.get("ticker").upper(),
            "asset_class": data.get("asset_class", "stock"),
            "quantity": qty,
            "buy_price_avg": buy_price,
            "current_price": cur_price,
            "currency": data.get("currency", "USD")
        }).execute()

        return web.json_response({"status": "success", "investment": new_inv.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_analytics_and_forecast(request: web.Request):
    try:
        tg_id = int(request.query.get("telegram_id"))
        days_back = int(request.query.get("days", 30))
        forecast_days = int(request.query.get("forecast_days", 30))

        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        txs = supabase.table("transactions").select("amount, type, transaction_date")\
            .eq("user_id", user_uuid).gte("transaction_date", start_date).execute().data

        total_income = sum(float(t["amount"]) for t in txs if t["type"] == "income")
        total_expense = sum(float(t["amount"]) for t in txs if t["type"] == "expense")

        daily_expense = total_expense / max(days_back, 1)
        daily_income = total_income / max(days_back, 1)

        projected_income = round(daily_income * forecast_days, 2)
        projected_expense = round(daily_expense * forecast_days, 2)
        projected_net_savings = round(projected_income - projected_expense, 2)

        accs = supabase.table("accounts").select("balance").eq("user_id", user_uuid).execute().data
        current_liquidity = sum(float(a["balance"]) for a in accs)
        projected_end_balance = round(current_liquidity + projected_net_savings, 2)
        runway_days = round(current_liquidity / daily_expense) if daily_expense > 0 else 999

        return web.json_response({
            "history_days": days_back,
            "forecast_days": forecast_days,
            "historical_income": total_income,
            "historical_expense": total_expense,
            "daily_burn_rate": round(daily_expense, 2),
            "projected_income": projected_income,
            "projected_expense": projected_expense,
            "projected_savings": projected_net_savings,
            "projected_end_balance": projected_end_balance,
            "runway_days": runway_days
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- ОБРОБНИКИ ЧАТ-БОТА (AIOGRAM) ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    chat_id = message.chat.id

    user_data = supabase.table("users").upsert({
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name
    }, on_conflict="telegram_id").execute().data[0]

    user_uuid = user_data["id"]

    accs = supabase.table("accounts").select("id").eq("user_id", user_uuid).execute().data
    if not accs:
        supabase.table("accounts").insert({
            "user_id": user_uuid,
            "name": "Основна картка",
            "account_type": "card",
            "currency": "UAH",
            "balance": 0.00
        }).execute()

    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text="📊 Капітал", web_app=WebAppInfo(url=MINIAPP_URL))
        )
    except Exception as e:
        logging.warning(f"Menu Button set error: {e}")

    await message.answer(
        f"Вітаю, {user.first_name}! 💼\n\n"
        "<b>FinTrack Bot & Mini App</b> готовий до роботи:\n\n"
        "⚡ <b>Швидкі записи в чаті:</b>\n"
        "• <code>Кава 75</code> або <code>Продукти 350</code> — додати витрату\n"
        "• <code>+25000 Зарплата</code> — додати дохід\n\n"
        "📋 <b>Команди:</b>\n"
        "• /balance — залишки по рахунках\n"
        "• /today — виписка за сьогодні\n"
        "• /month — звіт за поточний місяць\n\n"
        "📈 Відкривайте повний інтерфейс аналітики кнопкою <b>«Капітал»</b> зліва внизу.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Відкрити панель капіталу", web_app=WebAppInfo(url=MINIAPP_URL))]
        ]),
        parse_mode="HTML"
    )

@dp.message(F.text.regexp(r"^\+(\d+(?:\.\d+)?)\s*(.*)$"))
async def handle_quick_income(message: Message):
    match = message.text.split(maxsplit=1)
    amount = float(match[0].replace("+", ""))
    desc = match[1] if len(match) > 1 else "Дохід"
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)

    if not user_uuid:
        await message.reply("Спочатку натисніть /start")
        return

    accs = supabase.table("accounts").select("id, balance").eq("user_id", user_uuid).limit(1).execute().data
    if not accs:
        return
    acc = accs[0]

    supabase.table("accounts").update({"balance": float(acc["balance"]) + amount}).eq("id", acc["id"]).execute()
    supabase.table("transactions").insert({
        "user_id": user_uuid,
        "account_id": acc["id"],
        "amount": amount,
        "type": "income",
        "note": desc
    }).execute()

    await message.reply(f"🟢 <b>Дохід додано:</b> +{amount:,.2f} грн\nОпис: <i>{desc}</i>", parse_mode="HTML")

@dp.message(F.text.regexp(r"^(.+)\s+(\d+(?:\.\d+)?)$"))
async def handle_quick_expense(message: Message):
    parts = message.text.rsplit(maxsplit=1)
    desc = parts[0]
    amount = float(parts[1])
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)

    if not user_uuid:
        await message.reply("Спочатку натисніть /start")
        return

    accs = supabase.table("accounts").select("id, balance").eq("user_id", user_uuid).limit(1).execute().data
    if not accs:
        return
    acc = accs[0]

    supabase.table("accounts").update({"balance": float(acc["balance"]) - amount}).eq("id", acc["id"]).execute()
    supabase.table("transactions").insert({
        "user_id": user_uuid,
        "account_id": acc["id"],
        "amount": amount,
        "type": "expense",
        "note": desc
    }).execute()

    await message.reply(f"🔴 <b>Витрату записано:</b> -{amount:,.2f} грн\nОпис: <i>{desc}</i>", parse_mode="HTML")

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Будь ласка, почніть із команди /start.")
        return

    accs = supabase.table("accounts").select("*").eq("user_id", user_uuid).execute().data
    text = "💳 <b>Залишки на ваших рахунках:</b>\n\n"
    for a in accs:
        text += f"• {a['name']}: <b>{float(a['balance']):,.2f} {a['currency']}</b>\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("today"))
async def cmd_today(message: Message):
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
    txs = supabase.table("transactions").select("*").eq("user_id", user_uuid).gte("transaction_date", today_start).execute().data

    inc = sum(float(t["amount"]) for t in txs if t["type"] == "income")
    exp = sum(float(t["amount"]) for t in txs if t["type"] == "expense")

    await message.answer(
        f"📅 <b>Підсумок за сьогодні:</b>\n\n"
        f"🟢 Доходи: +{inc:,.2f} грн\n"
        f"🔴 Витрати: -{exp:,.2f} грн\n"
        f"💰 Сальдо: <b>{(inc - exp):,.2f} грн</b>",
        parse_mode="HTML"
    )

# --- ЗАПУСК ДОДАТКУ ---

async def main():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/v1/dashboard/summary", handle_dashboard_summary)
    app.router.add_post("/api/v1/transactions", handle_create_transaction)
    app.router.add_post("/api/v1/accounts", handle_create_account)
    app.router.add_post("/api/v1/investments", handle_create_investment)
    app.router.add_get("/api/v1/analytics/forecast", handle_analytics_and_forecast)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    logging.info(f"API сервер запущено на порті {SERVER_PORT}...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())