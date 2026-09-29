import os
import json
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
from dotenv import load_dotenv

import aiohttp
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

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
        logging.error(f"InitData error: {e}")
        return None

def get_user_uuid_by_tg_id(tg_id: int) -> str | None:
    res = supabase.table("users").select("id").eq("telegram_id", tg_id).execute()
    if res.data:
        return res.data[0]["id"]
    return None

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

# --- HTTP ЕНДПОІНТИ ---

async def handle_health(request: web.Request):
    return web.Response(text="FinTrack Engine is live! 🚀", status=200)

async def handle_market_quote(request: web.Request):
    """Підтягування реальних цін для акцій або криптовалюти"""
    symbol = request.query.get("symbol", "").upper().strip()
    asset_type = request.query.get("type", "stock")

    if not symbol:
        return web.json_response({"error": "Symbol required"}, status=400)

    price = 0.0
    name = symbol

    try:
        async with aiohttp.ClientSession() as session:
            if asset_type == "crypto":
                # Пошук ціни крипти через CoinGecko API
                cg_map = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "USDT": "tether", "TON": "the-open-network", "BNB": "binancecoin"}
                cg_id = cg_map.get(symbol, symbol.lower())
                async with session.get(f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if cg_id in data:
                            price = float(data[cg_id]["usd"])
            else:
                # Пошук ціни акцій через відкритий endpoint Yahoo Finance
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d"
                headers = {"User-Agent": "Mozilla/5.0"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
                        price = float(meta.get("regularMarketPrice", 0.0))
                        name = meta.get("shortName", symbol)
    except Exception as e:
        logging.warning(f"Error fetching market price for {symbol}: {e}")

    return web.json_response({"symbol": symbol, "name": name, "price": price})

async def handle_dashboard_summary(request: web.Request):
    try:
        tg_id_param = request.query.get("telegram_id")
        if not tg_id_param:
            return web.json_response({"error": "telegram_id required"}, status=400)
        
        user_uuid = get_user_uuid_by_tg_id(int(tg_id_param))
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        accs = supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").execute().data
        invs = supabase.table("investments").select("*").eq("user_id", user_uuid).order("updated_at", desc=True).execute().data
        goals = supabase.table("goals").select("*").eq("user_id", user_uuid).order("created_at").execute().data
        txs = supabase.table("transactions").select("*").eq("user_id", user_uuid).order("transaction_date", desc=True).limit(30).execute().data

        rates_res = supabase.table("exchange_rates").select("*").execute().data
        rates = {r["currency"]: float(r["rate_to_base"]) for r in rates_res}

        total_accounts_uah = sum(float(a["balance"]) * rates.get(a["currency"], 1.0) for a in accs)
        total_investments_uah = sum(
            float(i["quantity"]) * float(i["current_price"]) * rates.get(i["currency"], 1.0) 
            for i in invs
        )
        total_goals_uah = sum(float(g["current_amount"]) * rates.get(g["currency"], 1.0) for g in goals)
        net_worth = total_accounts_uah + total_investments_uah + total_goals_uah

        return web.json_response({
            "accounts": accs,
            "investments": invs,
            "goals": goals,
            "transactions": txs,
            "summary": {
                "net_worth": round(net_worth, 2),
                "accounts_total_uah": round(total_accounts_uah, 2),
                "investments_total_uah": round(total_investments_uah, 2),
                "goals_total_uah": round(total_goals_uah, 2)
            }
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_create_transaction(request: web.Request):
    """Фіксований переказ коштів навіть з/на рахунки з від'ємним балансом"""
    try:
        data = await request.json()
        tg_id = data.get("telegram_id")
        user_uuid = get_user_uuid_by_tg_id(int(tg_id))
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        amount = float(data.get("amount", 0))
        tx_type = data.get("type", "expense")
        account_id = data.get("account_id")
        to_account_id = data.get("to_account_id")
        target_amount = float(data.get("target_amount", amount)) if to_account_id else amount
        note = data.get("note", "")

        if tx_type == "expense":
            acc = supabase.table("accounts").select("balance").eq("id", account_id).execute().data[0]
            new_bal = float(acc["balance"]) - amount
            supabase.table("accounts").update({"balance": new_bal}).eq("id", account_id).execute()

        elif tx_type == "income":
            acc = supabase.table("accounts").select("balance").eq("id", account_id).execute().data[0]
            new_bal = float(acc["balance"]) + amount
            supabase.table("accounts").update({"balance": new_bal}).eq("id", account_id).execute()

        elif tx_type == "transfer":
            if not to_account_id or account_id == to_account_id:
                return web.json_response({"error": "Рахунок відправника і отримувача мають бути різними"}, status=400)

            # Отримуємо рахунок відправника (списання)
            acc_from = supabase.table("accounts").select("balance").eq("id", account_id).execute().data[0]
            # Отримуємо рахунок отримувача (зарахування)
            acc_to = supabase.table("accounts").select("balance").eq("id", to_account_id).execute().data[0]

            # Оновлюємо рахунки абсолютно незалежно:
            # Навіть якщо acc_to в мінусі (-3000), додавання target_amount (+1000) зробить його -2000
            new_from_balance = float(acc_from["balance"]) - amount
            new_to_balance = float(acc_to["balance"]) + target_amount

            supabase.table("accounts").update({"balance": new_from_balance}).eq("id", account_id).execute()
            supabase.table("accounts").update({"balance": new_to_balance}).eq("id", to_account_id).execute()

        supabase.table("transactions").insert({
            "user_id": user_uuid,
            "account_id": account_id,
            "to_account_id": to_account_id if tx_type == "transfer" else None,
            "amount": amount,
            "target_amount": target_amount if tx_type == "transfer" else None,
            "type": tx_type,
            "note": note
        }).execute()

        return web.json_response({"status": "success"})
    except Exception as e:
        logging.error(f"Transaction execution error: {e}")
        return web.json_response({"error": str(e)}, status=500)

# CRUD для рахунків
async def handle_create_account(request: web.Request):
    data = await request.json()
    user_uuid = get_user_uuid_by_tg_id(int(data.get("telegram_id")))
    res = supabase.table("accounts").insert({
        "user_id": user_uuid,
        "name": data.get("name"),
        "account_type": data.get("account_type", "card"),
        "currency": data.get("currency", "UAH"),
        "balance": float(data.get("balance", 0.0))
    }).execute()
    return web.json_response({"status": "success", "account": res.data[0]})

async def handle_update_account(request: web.Request):
    acc_id = request.match_info.get("id")
    data = await request.json()
    res = supabase.table("accounts").update({
        "name": data.get("name"),
        "balance": float(data.get("balance", 0.0))
    }).eq("id", acc_id).execute()
    return web.json_response({"status": "updated", "account": res.data[0]})

async def handle_delete_account(request: web.Request):
    acc_id = request.match_info.get("id")
    supabase.table("accounts").delete().eq("id", acc_id).execute()
    return web.json_response({"status": "deleted"})

# CRUD для інвестицій
async def handle_create_investment(request: web.Request):
    data = await request.json()
    user_uuid = get_user_uuid_by_tg_id(int(data.get("telegram_id")))
    res = supabase.table("investments").insert({
        "user_id": user_uuid,
        "ticker": data.get("ticker").upper(),
        "name": data.get("name") or data.get("ticker").upper(),
        "asset_class": data.get("asset_class", "stock"),
        "quantity": float(data.get("quantity", 0)),
        "buy_price_avg": float(data.get("buy_price_avg", 0)),
        "current_price": float(data.get("current_price", 0)),
        "currency": data.get("currency", "USD"),
        "interest_rate": float(data.get("interest_rate", 0)),
        "maturity_date": data.get("maturity_date") or None,
        "term_months": int(data.get("term_months", 0)) if data.get("term_months") else None
    }).execute()
    return web.json_response({"status": "success", "investment": res.data[0]})

async def handle_update_investment(request: web.Request):
    inv_id = request.match_info.get("id")
    data = await request.json()
    res = supabase.table("investments").update({
        "ticker": data.get("ticker").upper(),
        "quantity": float(data.get("quantity", 0)),
        "buy_price_avg": float(data.get("buy_price_avg", 0)),
        "current_price": float(data.get("current_price", 0)),
        "interest_rate": float(data.get("interest_rate", 0)),
        "currency": data.get("currency", "USD"),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", inv_id).execute()
    return web.json_response({"status": "updated", "investment": res.data[0]})

async def handle_delete_investment(request: web.Request):
    inv_id = request.match_info.get("id")
    supabase.table("investments").delete().eq("id", inv_id).execute()
    return web.json_response({"status": "deleted"})

# CRUD для цілей
async def handle_create_goal(request: web.Request):
    data = await request.json()
    user_uuid = get_user_uuid_by_tg_id(int(data.get("telegram_id")))
    res = supabase.table("goals").insert({
        "user_id": user_uuid,
        "title": data.get("title"),
        "target_amount": float(data.get("target_amount", 0)),
        "current_amount": 0,
        "currency": "UAH",
        "deadline": data.get("deadline") or None
    }).execute()
    return web.json_response({"status": "success", "goal": res.data[0]})

async def handle_modify_goal_funds(request: web.Request):
    data = await request.json()
    goal = supabase.table("goals").select("*").eq("id", data.get("goal_id")).execute().data[0]
    acc = supabase.table("accounts").select("*").eq("id", data.get("account_id")).execute().data[0]
    amt = float(data.get("amount", 0))
    action = data.get("action")

    if action == "deposit":
        supabase.table("accounts").update({"balance": float(acc["balance"]) - amt}).eq("id", acc["id"]).execute()
        supabase.table("goals").update({"current_amount": float(goal["current_amount"]) + amt}).eq("id", goal["id"]).execute()
    else:
        supabase.table("goals").update({"current_amount": float(goal["current_amount"]) - amt}).eq("id", goal["id"]).execute()
        supabase.table("accounts").update({"balance": float(acc["balance"]) + amt}).eq("id", acc["id"]).execute()

    return web.json_response({"status": "success"})

async def handle_delete_goal(request: web.Request):
    goal_id = request.match_info.get("id")
    supabase.table("goals").delete().eq("id", goal_id).execute()
    return web.json_response({"status": "deleted"})

async def handle_analytics_and_forecast(request: web.Request):
    tg_id = int(request.query.get("telegram_id"))
    days_back = int(request.query.get("days", 30))
    user_uuid = get_user_uuid_by_tg_id(tg_id)

    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    txs = supabase.table("transactions").select("*").eq("user_id", user_uuid).gte("transaction_date", start_date).execute().data

    total_income = sum(float(t["amount"]) for t in txs if t["type"] == "income")
    total_expense = sum(float(t["amount"]) for t in txs if t["type"] == "expense")
    daily_expense = total_expense / max(days_back, 1)
    daily_income = total_income / max(days_back, 1)

    accs = supabase.table("accounts").select("balance").eq("user_id", user_uuid).execute().data
    current_liquidity = sum(float(a["balance"]) for a in accs)

    return web.json_response({
        "historical_income": total_income,
        "historical_expense": total_expense,
        "daily_burn_rate": round(daily_expense, 2),
        "projected_income": round(daily_income * 30, 2),
        "projected_expense": round(daily_expense * 30, 2),
        "projected_savings": round((daily_income - daily_expense) * 30, 2),
        "projected_end_balance": round(current_liquidity + ((daily_income - daily_expense) * 30), 2),
        "runway_days": round(current_liquidity / daily_expense) if daily_expense > 0 else 999
    })

# --- БОТ ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    user_data = supabase.table("users").upsert({
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name
    }, on_conflict="telegram_id").execute().data[0]

    accs = supabase.table("accounts").select("id").eq("user_id", user_data["id"]).execute().data
    if not accs:
        supabase.table("accounts").insert({
            "user_id": user_data["id"],
            "name": "Основна картка",
            "account_type": "card",
            "currency": "UAH",
            "balance": 0.00
        }).execute()

    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="📊 Капітал", web_app=WebAppInfo(url=MINIAPP_URL))
        )
    except Exception as e:
        logging.warning(f"Menu button err: {e}")

    await message.answer(
        f"Вітаю, {user.first_name}! 💼\nВідкривайте фінансовий кабінет кнопкою <b>«Капітал»</b> зліва внизу.",
        parse_mode="HTML"
    )

async def main():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/v1/market/quote", handle_market_quote)
    app.router.add_get("/api/v1/dashboard/summary", handle_dashboard_summary)
    app.router.add_post("/api/v1/transactions", handle_create_transaction)
    app.router.add_post("/api/v1/accounts", handle_create_account)
    app.router.add_put("/api/v1/accounts/{id}", handle_update_account)
    app.router.add_delete("/api/v1/accounts/{id}", handle_delete_account)
    app.router.add_post("/api/v1/goals", handle_create_goal)
    app.router.add_post("/api/v1/goals/funds", handle_modify_goal_funds)
    app.router.add_delete("/api/v1/goals/{id}", handle_delete_goal)
    app.router.add_post("/api/v1/investments", handle_create_investment)
    app.router.add_put("/api/v1/investments/{id}", handle_update_investment)
    app.router.add_delete("/api/v1/investments/{id}", handle_delete_investment)
    app.router.add_get("/api/v1/analytics/forecast", handle_analytics_and_forecast)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())