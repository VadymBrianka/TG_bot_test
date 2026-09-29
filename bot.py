import os
import json
import hmac
import hashlib
import asyncio
import logging
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

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
    raise ValueError("Перевірте змінні оточення у файлі .env!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- ВАЛІДАЦІЯ TELEGRAM INITDATA ---
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

# --- CORS MIDDLEWARE ---
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, ngrok-skip-browser-warning"
    return response

# --- HTTP ЕНДПОІНТИ (API ДЛЯ MINI APP) ---

async def handle_health(request: web.Request):
    return web.Response(text="Financial Bot is running healthy! 🚀", status=200)

async def handle_get_dashboard(request: web.Request):
    """Повертає баланси рахунків, цілі та аналітику витрат для Mini App"""
    try:
        user_id_param = request.query.get("user_id")
        if not user_id_param:
            return web.json_response({"error": "user_id required"}, status=400)
        
        user_id = int(user_id_param)

        # 1. Рахунки
        accounts_res = supabase.table("accounts").select("*").eq("user_id", user_id).execute()
        
        # 2. Останні транзакції
        tx_res = supabase.table("transactions").select("*, categories(name, icon)")\
            .eq("user_id", user_id).order("created_at", desc=True).limit(15).execute()
        
        # 3. Цілі накопичень
        goals_res = supabase.table("savings_goals").select("*").eq("user_id", user_id).execute()
        
        # 4. Інвестиції
        investments_res = supabase.table("investment_assets").select("*").eq("user_id", user_id).execute()

        # Підрахунок загального капіталу (Net Worth)
        total_accounts_balance = sum(float(acc["balance"]) for acc in accounts_res.data)
        total_investments = sum(float(inv["quantity"]) * float(inv["current_price"]) for inv in investments_res.data)
        
        # Розрахунок середніх щоденних витрат за останні 30 днів для прогнозу
        total_expenses_month = sum(float(tx["amount"]) for tx in tx_res.data if tx["type"] == "expense")
        forecast_daily_limit = round(max((total_accounts_balance - 500) / 30, 0), 2) if total_accounts_balance > 500 else 0

        return web.json_response({
            "accounts": accounts_res.data,
            "transactions": tx_res.data,
            "goals": goals_res.data,
            "investments": investments_res.data,
            "summary": {
                "net_worth": total_accounts_balance + total_investments,
                "accounts_total": total_accounts_balance,
                "investments_total": total_investments,
                "recent_expenses": total_expenses_month,
                "recommended_daily_limit": forecast_daily_limit
            }
        })
    except Exception as e:
        logging.error(f"Dashboard API Error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_add_transaction(request: web.Request):
    """Створення транзакції через Mini App"""
    try:
        data = await request.json()
        init_data = data.get("initData")
        user_id = data.get("userId")
        
        if init_data:
            val = validate_init_data(init_data, BOT_TOKEN)
            if val:
                u_info = json.loads(val.get("user", "{}"))
                user_id = u_info.get("id")

        if not user_id:
            return web.json_response({"error": "Unauthorized"}, status=403)

        amount = float(data.get("amount", 0))
        tx_type = data.get("type", "expense")
        desc = data.get("description", "Транзакція з Mini App")
        account_id = data.get("account_id")

        # Якщо рахунок не передано, беремо перший активний рахунок користувача
        if not account_id:
            acc_list = supabase.table("accounts").select("id").eq("user_id", user_id).limit(1).execute()
            if acc_list.data:
                account_id = acc_list.data[0]["id"]
            else:
                new_acc = supabase.table("accounts").insert({
                    "user_id": user_id, "name": "Основний", "balance": 0.0
                }).execute()
                account_id = new_acc.data[0]["id"]

        tx_payload = {
            "user_id": user_id,
            "account_id": account_id,
            "amount": amount,
            "type": tx_type,
            "description": desc
        }
        supabase.table("transactions").insert(tx_payload).execute()

        # Повідомлення в чат
        type_symbol = "🔴 Витрата" if tx_type == "expense" else "🟢 Дохід"
        await bot.send_message(
            chat_id=user_id,
            text=f"{type_symbol}: <b>{amount:.2f} грн</b>\nОпис: <i>{desc}</i>",
            parse_mode="HTML"
        )

        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Add transaction API error: {e}")
        return web.json_response({"error": str(e)}, status=500)

# --- ОБРОБНИКИ ЧАТ-БОТА ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    chat_id = message.chat.id

    # 1. Реєструємо/оновлюємо користувача в Supabase
    supabase.table("users").upsert({
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name
    }).execute()

    # 2. Створюємо базовий рахунок, якщо ще немає
    existing_acc = supabase.table("accounts").select("id").eq("user_id", user.id).execute()
    if not existing_acc.data:
        supabase.table("accounts").insert({
            "user_id": user.id,
            "name": "Основна картка",
            "type": "card",
            "balance": 0.00
        }).execute()

    # 3. Встановлюємо системну Menu Button (зліва біля скріпки)
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text="📊 Капітал", web_app=WebAppInfo(url=MINIAPP_URL))
        )
    except Exception as e:
        logging.warning(f"Menu Button error: {e}")

    await message.answer(
        f"Привіт, {user.first_name}! 💼\n\n"
        "Я ваш особистий фінансовий трекер:\n"
        "• <b>Швидкий запис:</b> пишіть у чат <code>Кава 75</code> або <code>Таксі 220</code>\n"
        "• <b>Запис доходу:</b> пишіть <code>+25000 Зарплата</code>\n"
        "• <b>Аналітика та цілі:</b> відкривайте Mini App внизу зліва 📊",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Відкрити панель капіталу", web_app=WebAppInfo(url=MINIAPP_URL))]
        ]),
        parse_mode="HTML"
    )

# Швидкий дохід: "+15000 премія"
@dp.message(F.text.regexp(r"^\+(\d+(?:\.\d+)?)\s*(.*)$"))
async def handle_quick_income(message: Message):
    match = message.text.split(maxsplit=1)
    amount = float(match[0].replace("+", ""))
    desc = match[1] if len(match) > 1 else "Дохід"
    user_id = message.from_user.id

    acc = supabase.table("accounts").select("id").eq("user_id", user_id).limit(1).execute()
    if not acc.data:
        acc = supabase.table("accounts").insert({"user_id": user_id, "name": "Основна", "balance": 0}).execute()
    
    account_id = acc.data[0]["id"]
    supabase.table("transactions").insert({
        "user_id": user_id,
        "account_id": account_id,
        "amount": amount,
        "type": "income",
        "description": desc
    }).execute()

    await message.reply(f"🟢 <b>Дохід додано:</b> +{amount:.2f} грн\nОпис: <i>{desc}</i>", parse_mode="HTML")

# Швидка витрата: "Продукти 450" або "Кава 70"
@dp.message(F.text.regexp(r"^(.+)\s+(\d+(?:\.\d+)?)$"))
async def handle_quick_expense(message: Message):
    parts = message.text.rsplit(maxsplit=1)
    desc = parts[0]
    amount = float(parts[1])
    user_id = message.from_user.id

    acc = supabase.table("accounts").select("id").eq("user_id", user_id).limit(1).execute()
    if not acc.data:
        acc = supabase.table("accounts").insert({"user_id": user_id, "name": "Основна", "balance": 0}).execute()

    account_id = acc.data[0]["id"]
    supabase.table("transactions").insert({
        "user_id": user_id,
        "account_id": account_id,
        "amount": amount,
        "type": "expense",
        "description": desc
    }).execute()

    await message.reply(f"🔴 <b>Витрату записано:</b> -{amount:.2f} грн\nКатегорія/Опис: <i>{desc}</i>", parse_mode="HTML")

# Перегляд балансу в чаті
@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    accounts = supabase.table("accounts").select("*").eq("user_id", user_id).execute().data
    
    if not accounts:
        await message.answer("У вас ще немає створених рахунків. Натисніть /start.")
        return

    text = "💳 <b>Ваші рахунки:</b>\n\n"
    total = 0.0
    for acc in accounts:
        b = float(acc["balance"])
        total += b
        text += f"• {acc['name']}: <b>{b:,.2f} {acc['currency']}</b>\n"
    
    text += f"\n💰 <b>Загальний баланс: {total:,.2f} UAH</b>"
    await message.answer(text, parse_mode="HTML")

# --- ДОДАТКОВІ ЕНДПОІНТИ КЕРУВАННЯ ТА АНАЛІТИКИ ---

async def handle_create_account(request: web.Request):
    """Створення нового рахунку (картка, готівка, депозит)"""
    try:
        data = await request.json()
        user_id = data.get("userId")
        name = data.get("name")
        acc_type = data.get("type", "card")
        balance = float(data.get("balance", 0.0))
        currency = data.get("currency", "UAH")

        if not user_id or not name:
            return web.json_response({"error": "Missing fields"}, status=400)

        res = supabase.table("accounts").insert({
            "user_id": user_id,
            "name": name,
            "type": acc_type,
            "balance": balance,
            "currency": currency
        }).execute()

        return web.json_response({"status": "success", "account": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_create_investment(request: web.Request):
    """Додавання інвестиційного активу"""
    try:
        data = await request.json()
        user_id = data.get("userId")
        ticker = data.get("ticker_or_name")
        asset_type = data.get("asset_type", "stock")
        quantity = float(data.get("quantity", 0))
        buy_price = float(data.get("buy_price_avg", 0))
        current_price = float(data.get("current_price", buy_price))
        currency = data.get("currency", "USD")

        if not user_id or not ticker:
            return web.json_response({"error": "Missing fields"}, status=400)

        res = supabase.table("investment_assets").insert({
            "user_id": user_id,
            "ticker_or_name": ticker,
            "asset_type": asset_type,
            "quantity": quantity,
            "buy_price_avg": buy_price,
            "current_price": current_price,
            "currency": currency
        }).execute()

        return web.json_response({"status": "success", "asset": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_analytics_and_forecast(request: web.Request):
    """Аналіз за обраний період (дні) та прогноз на наступні N днів на базі історії"""
    try:
        user_id = int(request.query.get("user_id"))
        days_back = int(request.query.get("days", 30))
        forecast_days = int(request.query.get("forecast_days", 30))

        since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

        # Отримуємо транзакції за обраний історичний інтервал
        res = supabase.table("transactions")\
            .select("amount, type, description, created_at")\
            .eq("user_id", user_id)\
            .gte("created_at", since_date)\
            .execute()

        transactions = res.data

        total_income = sum(float(t["amount"]) for t in transactions if t["type"] == "income")
        total_expense = sum(float(t["amount"]) for t in transactions if t["type"] == "expense")

        # Середньодобові темпи (Run-rate)
        daily_income_avg = total_income / max(days_back, 1)
        daily_expense_avg = total_expense / max(days_back, 1)

        # Прогноз на наступний період
        projected_income = round(daily_income_avg * forecast_days, 2)
        projected_expense = round(daily_expense_avg * forecast_days, 2)
        projected_net_savings = round(projected_income - projected_expense, 2)

        # Поточний залишок на рахунках
        accs = supabase.table("accounts").select("balance").eq("user_id", user_id).execute()
        current_liquidity = sum(float(a["balance"]) for a in accs.data)
        projected_end_balance = round(current_liquidity + projected_net_savings, 2)

        # Розрахунок Runway (на скільки днів вистачить коштів при поточному темпі витрат)
        runway_days = round(current_liquidity / daily_expense_avg) if daily_expense_avg > 0 else 999

        return web.json_response({
            "history_period_days": days_back,
            "forecast_period_days": forecast_days,
            "historical_income": total_income,
            "historical_expense": total_expense,
            "daily_burn_rate": round(daily_expense_avg, 2),
            "projected_income": projected_income,
            "projected_expense": projected_expense,
            "projected_savings": projected_net_savings,
            "projected_end_balance": projected_end_balance,
            "runway_days": runway_days
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- СТАРТ СЕРВЕРА ТА БОТА ---
async def main():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/dashboard", handle_get_dashboard)
    app.router.add_post("/api/transaction", handle_add_transaction)
    app.router.add_post("/api/account", handle_create_account)
    app.router.add_post("/api/investment", handle_create_investment)
    app.router.add_get("/api/analytics", handle_analytics_and_forecast)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    logging.info(f"API сервер запущено на порті {SERVER_PORT}...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())