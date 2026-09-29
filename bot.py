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
from aiogram.types import Message, WebAppInfo, MenuButtonWebApp
from aiogram.filters import CommandStart, Command
from supabase import create_client, Client

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL")
SERVER_PORT = int(os.getenv("PORT", 8080))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not BOT_TOKEN or not MINIAPP_URL or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Перевірте змінні середовища BOT_TOKEN, MINIAPP_URL, SUPABASE_URL та SUPABASE_KEY!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_user_uuid_by_tg_id(tg_id: int) -> str | None:
    res = supabase.table("users").select("id").eq("telegram_id", tg_id).execute()
    if res.data:
        return res.data[0]["id"]
    return None

# --- ОНОВЛЕННЯ КУРСІВ ВАЛЮТ З НБУ ---
async def update_rates_from_nbu():
    try:
        url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    records = [{"currency": "UAH", "rate_to_base": 1.0}]
                    for item in data:
                        cc = str(item.get("cc")).strip().upper()
                        # Виключаємо суто криптовалютні позначення
                        if cc != "USDT":
                            rate = float(item.get("rate", 1.0))
                            records.append({"currency": cc, "rate_to_base": rate})
                    supabase.table("exchange_rates").upsert(records, on_conflict="currency").execute()
                    logging.info(f"Синхронізовано {len(records)} курсів валют з НБУ.")
    except Exception as e:
        logging.error(f"Помилка завантаження курсів НБУ: {e}")

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

async def handle_health(request: web.Request):
    return web.Response(text="FinTrack Engine is live! 🚀", status=200)

async def handle_get_currencies(request: web.Request):
    try:
        res = supabase.table("exchange_rates").select("currency, rate_to_base").order("currency").execute().data
        # Прибираємо USDT зі списку
        currencies = [r for r in res if r.get("currency") != "USDT"]
        return web.json_response({"currencies": currencies})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_get_categories(request: web.Request):
    try:
        res = supabase.table("categories").select("*").order("name").execute().data
        return web.json_response({"categories": res})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_dashboard_summary(request: web.Request):
    try:
        tg_id_param = request.query.get("telegram_id")
        if not tg_id_param:
            return web.json_response({"error": "telegram_id is required"}, status=400)
        
        tg_id = int(tg_id_param)
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        accs = supabase.table("accounts").select("*").eq("user_id", user_uuid).order("created_at").execute().data
        invs = supabase.table("investments").select("*").eq("user_id", user_uuid).order("updated_at", desc=True).execute().data
        goals = supabase.table("goals").select("*").eq("user_id", user_uuid).order("created_at").execute().data
        categories = supabase.table("categories").select("*").execute().data
        
        txs = supabase.table("transactions")\
            .select("*, categories(name, icon), from_acc:accounts!account_id(name, currency), to_acc:accounts!to_account_id(name, currency)")\
            .eq("user_id", user_uuid).order("transaction_date", desc=True).limit(35).execute().data

        rates_res = supabase.table("exchange_rates").select("*").execute().data
        rates = {r["currency"]: float(r["rate_to_base"]) for r in rates_res if r.get("currency") != "USDT"}
        rates["UAH"] = 1.0

        total_accounts_uah = sum(float(a["balance"]) * rates.get(a["currency"], 1.0) for a in accs)
        total_investments_uah = sum(
            float(i["quantity"]) * float(i.get("current_price") or i.get("buy_price_avg") or 0) * rates.get(i.get("currency", "USD"), 1.0) 
            for i in invs
        )
        total_goals_uah = sum(float(g["current_amount"]) * rates.get(g.get("currency", "UAH"), 1.0) for g in goals)
        net_worth = total_accounts_uah + total_investments_uah + total_goals_uah

        return web.json_response({
            "accounts": accs,
            "investments": invs,
            "goals": goals,
            "categories": categories,
            "transactions": txs,
            "exchange_rates": rates,
            "summary": {
                "net_worth": round(net_worth, 2),
                "accounts_total_uah": round(total_accounts_uah, 2),
                "investments_total_uah": round(total_investments_uah, 2),
                "goals_total_uah": round(total_goals_uah, 2)
            }
        })
    except Exception as e:
        logging.error(f"Dashboard Error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_create_transaction(request: web.Request):
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
        category_id = data.get("category_id") or None
        target_amount = float(data.get("target_amount", amount)) if to_account_id else None
        note = data.get("note", "")

        if not account_id:
            return web.json_response({"error": "Рахунок не обрано"}, status=400)

        acc_from_res = supabase.table("accounts").select("balance").eq("id", account_id).execute().data
        if not acc_from_res:
            return web.json_response({"error": "Рахунок не знайдено"}, status=404)
        acc_from_bal = float(acc_from_res[0]["balance"])

        if tx_type == "expense":
            supabase.table("accounts").update({"balance": acc_from_bal - amount}).eq("id", account_id).execute()
        elif tx_type == "income":
            supabase.table("accounts").update({"balance": acc_from_bal + amount}).eq("id", account_id).execute()
        elif tx_type == "transfer":
            if not to_account_id or to_account_id == account_id:
                return web.json_response({"error": "Оберіть два різних рахунки для переказу"}, status=400)
            
            acc_to_res = supabase.table("accounts").select("balance").eq("id", to_account_id).execute().data
            if not acc_to_res:
                return web.json_response({"error": "Рахунок отримувача не знайдено"}, status=404)
            acc_to_bal = float(acc_to_res[0]["balance"])

            supabase.table("accounts").update({"balance": acc_from_bal - amount}).eq("id", account_id).execute()
            supabase.table("accounts").update({"balance": acc_to_bal + target_amount}).eq("id", to_account_id).execute()

        supabase.table("transactions").insert({
            "user_id": user_uuid,
            "account_id": account_id,
            "to_account_id": to_account_id if tx_type == "transfer" else None,
            "category_id": category_id,
            "amount": amount,
            "target_amount": target_amount,
            "type": tx_type,
            "note": note
        }).execute()

        return web.json_response({"status": "success"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- ПЕРЕВІРКА ЦІН ТА ПОШУК (КРИПТА + АКЦІЇ) ---

async def handle_market_search(request: web.Request):
    query = request.query.get("q", "").strip().lower()
    asset_class = request.query.get("type", "stock").strip().lower()
    if not query:
        return web.json_response({"results": []})

    results = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            if asset_class == "crypto":
                # CoinGecko пошук
                url = f"https://api.coingecko.com/api/v3/search?query={query}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        cdata = await resp.json()
                        coins = cdata.get("coins", [])[:8]
                        for c in coins:
                            results.append({
                                "ticker": c.get("symbol", "").upper(),
                                "name": c.get("name", ""),
                                "currency": "USD",
                                "price": 0.0
                            })
                        if results:
                            return web.json_response({"results": results})

                # Fallback: базовий список
                common = [
                    {"ticker": "BTC", "name": "Bitcoin"},
                    {"ticker": "ETH", "name": "Ethereum"},
                    {"ticker": "SOL", "name": "Solana"},
                    {"ticker": "TON", "name": "Toncoin"},
                    {"ticker": "XRP", "name": "Ripple"},
                    {"ticker": "BNB", "name": "BNB"},
                    {"ticker": "DOGE", "name": "Dogecoin"},
                    {"ticker": "ADA", "name": "Cardano"}
                ]
                results = [c for c in common if query in c["ticker"].lower() or query in c["name"].lower()]
                return web.json_response({"results": results})

            elif asset_class == "stock":
                url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"
                headers = {"User-Agent": "Mozilla/5.0"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for q in data.get("quotes", []):
                            if q.get("quoteType") in ["EQUITY", "ETF"]:
                                results.append({
                                    "ticker": q.get("symbol"),
                                    "name": q.get("shortname") or q.get("longname") or q.get("symbol"),
                                    "currency": "USD",
                                    "price": 0.0
                                })
                        return web.json_response({"results": results})
    except Exception as e:
        logging.warning(f"Market search warning: {e}")

    return web.json_response({"results": results})

async def get_realtime_price(symbol: str, asset_class: str) -> float | None:
    symbol = symbol.strip().upper()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
            if asset_class == "crypto":
                # Спроба 1: Binance
                pair = f"{symbol}USDT"
                url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return round(float(data["price"]), 4)

                # Спроба 2: CoinCap
                url_cc = f"https://api.coincap.io/v2/assets?search={symbol}&limit=1"
                async with session.get(url_cc) as resp_cc:
                    if resp_cc.status == 200:
                        data = await resp_cc.json()
                        if data.get("data"):
                            return round(float(data["data"][0]["priceUsd"]), 4)
            else:
                # Yahoo Finance
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
                headers = {"User-Agent": "Mozilla/5.0"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        meta = data["chart"]["result"][0]["meta"]
                        price = meta.get("regularMarketPrice", 0)
                        return round(float(price), 4)
    except Exception as e:
        logging.warning(f"Price fetch failed for {symbol}: {e}")
    return None

async def handle_market_quote(request: web.Request):
    symbol = request.query.get("symbol", "").strip().upper()
    asset_class = request.query.get("type", "stock").strip().lower()
    if not symbol:
        return web.json_response({"error": "Symbol is required"}, status=400)

    price = await get_realtime_price(symbol, asset_class)
    if price is not None:
        return web.json_response({
            "symbol": symbol,
            "name": symbol,
            "price": price,
            "currency": "USD"
        })
    return web.json_response({"error": "Котирування не знайдено"}, status=404)

# Фонове динамічне оновлення цін усіх активів користувача
async def handle_refresh_investment_prices(request: web.Request):
    try:
        tg_id = int(request.query.get("telegram_id", 0))
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        invs = supabase.table("investments").select("id, ticker, asset_class")\
            .eq("user_id", user_uuid).in_("asset_class", ["stock", "crypto"]).execute().data

        updated_count = 0
        for item in invs:
            p = await get_realtime_price(item["ticker"], item["asset_class"])
            if p and p > 0:
                supabase.table("investments").update({
                    "current_price": p,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", item["id"]).execute()
                updated_count += 1

        return web.json_response({"status": "success", "updated": updated_count})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- АНАЛІТИКА ТА ПРОГНОЗУВАННЯ (ТИЖДЕНЬ, МІСЯЦЬ, РІК) ---

async def handle_analytics_and_forecast(request: web.Request):
    try:
        tg_id = int(request.query.get("telegram_id"))
        days_back = int(request.query.get("days", 30))

        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        txs = supabase.table("transactions")\
            .select("amount, type, note, transaction_date, category_id, account_id, categories(name, icon), accounts!account_id(name, currency)")\
            .eq("user_id", user_uuid).gte("transaction_date", start_date).execute().data

        total_income = sum(float(t["amount"]) for t in txs if t["type"] == "income")
        total_expense = sum(float(t["amount"]) for t in txs if t["type"] == "expense")

        daily_expense = total_expense / max(days_back, 1)
        daily_income = total_income / max(days_back, 1)
        daily_net = daily_income - daily_expense

        # Прогноз на 7, 30 та 365 днів
        accs = supabase.table("accounts").select("balance, currency").eq("user_id", user_uuid).execute().data
        rates_res = supabase.table("exchange_rates").select("*").execute().data
        rates = {r["currency"]: float(r["rate_to_base"]) for r in rates_res}
        rates["UAH"] = 1.0

        current_liquidity = sum(float(a["balance"]) * rates.get(a.get("currency", "UAH"), 1.0) for a in accs)

        forecast = {
            "week": {
                "income": round(daily_income * 7, 2),
                "expense": round(daily_expense * 7, 2),
                "net": round(daily_net * 7, 2),
                "balance": round(current_liquidity + (daily_net * 7), 2)
            },
            "month": {
                "income": round(daily_income * 30, 2),
                "expense": round(daily_expense * 30, 2),
                "net": round(daily_net * 30, 2),
                "balance": round(current_liquidity + (daily_net * 30), 2)
            },
            "year": {
                "income": round(daily_income * 365, 2),
                "expense": round(daily_expense * 365, 2),
                "net": round(daily_net * 365, 2),
                "balance": round(current_liquidity + (daily_net * 365), 2)
            }
        }

        # Аналітика по категоріях
        cat_stats = {}
        for t in txs:
            if t["type"] == "expense":
                cname = t["categories"]["name"] if t.get("categories") else "Інше"
                cicon = t["categories"]["icon"] if t.get("categories") else "💳"
                cat_stats[cname] = cat_stats.get(cname, {"name": cname, "icon": cicon, "total": 0})
                cat_stats[cname]["total"] += float(t["amount"])
        
        categories_breakdown = sorted(cat_stats.values(), key=lambda x: x["total"], reverse=True)

        # Аналітика по рахунках
        acc_stats = {}
        for t in txs:
            aname = t["accounts"]["name"] if t.get("accounts") else "Рахунок"
            if aname not in acc_stats:
                acc_stats[aname] = {"name": aname, "income": 0, "expense": 0}
            if t["type"] == "income":
                acc_stats[aname]["income"] += float(t["amount"])
            elif t["type"] == "expense":
                acc_stats[aname]["expense"] += float(t["amount"])

        runway_days = round(current_liquidity / daily_expense) if daily_expense > 0 else 999

        return web.json_response({
            "history_days": days_back,
            "historical_income": total_income,
            "historical_expense": total_expense,
            "daily_burn_rate": round(daily_expense, 2),
            "runway_days": runway_days,
            "current_liquidity": round(current_liquidity, 2),
            "forecast": forecast,
            "categories_breakdown": categories_breakdown,
            "accounts_breakdown": list(acc_stats.values())
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- ОПЕРАЦІЇ З ЦІЛЯМИ ---

async def handle_modify_goal_funds(request: web.Request):
    try:
        data = await request.json()
        goal_id = data.get("goal_id")
        account_id = data.get("account_id")
        amount = float(data.get("amount", 0))
        action = data.get("action")

        if not goal_id or not account_id or amount <= 0:
            return web.json_response({"error": "Некоректні параметри операції"}, status=400)

        goal = supabase.table("goals").select("*").eq("id", goal_id).execute().data[0]
        acc = supabase.table("accounts").select("*").eq("id", account_id).execute().data[0]

        goal_balance = float(goal["current_amount"])
        acc_balance = float(acc["balance"])

        goal_curr = goal.get("currency", "UAH").upper()
        acc_curr = acc.get("currency", "UAH").upper()

        rates_res = supabase.table("exchange_rates").select("*").execute().data
        rates = {r["currency"]: float(r["rate_to_base"]) for r in rates_res}
        rates["UAH"] = 1.0

        rate_goal = rates.get(goal_curr, 1.0)
        rate_acc = rates.get(acc_curr, 1.0)

        amount_in_acc_currency = amount * (rate_goal / rate_acc)

        if action == "deposit":
            if acc_balance < amount_in_acc_currency:
                return web.json_response({
                    "error": f"Недостатньо коштів на рахунку ({amount_in_acc_currency:.2f} {acc_curr})"
                }, status=400)
            supabase.table("accounts").update({"balance": acc_balance - amount_in_acc_currency}).eq("id", account_id).execute()
            supabase.table("goals").update({"current_amount": goal_balance + amount}).eq("id", goal_id).execute()
        elif action == "withdraw":
            if goal_balance < amount:
                return web.json_response({"error": "У цілі недостатньо коштів"}, status=400)
            supabase.table("goals").update({"current_amount": goal_balance - amount}).eq("id", goal_id).execute()
            supabase.table("accounts").update({"balance": acc_balance + amount_in_acc_currency}).eq("id", account_id).execute()

        return web.json_response({
            "status": "success",
            "converted_amount": round(amount_in_acc_currency, 2),
            "account_currency": acc_curr
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_close_goal(request: web.Request):
    try:
        goal_id = request.match_info.get("id")
        goal_res = supabase.table("goals").select("*").eq("id", goal_id).execute().data
        if not goal_res:
            return web.json_response({"error": "Ціль не знайдено"}, status=404)
        
        goal = goal_res[0]
        user_uuid = goal["user_id"]
        current_amount = float(goal.get("current_amount", 0))
        title = goal.get("title", "Ціль")
        goal_curr = goal.get("currency", "UAH")

        accs = supabase.table("accounts").select("id").eq("user_id", user_uuid).limit(1).execute().data
        acc_id = accs[0]["id"] if accs else None

        if current_amount > 0 and acc_id:
            supabase.table("transactions").insert({
                "user_id": user_uuid,
                "account_id": acc_id,
                "amount": current_amount,
                "type": "expense",
                "note": f"🎯 Досягнення цілі: {title} ({current_amount:,.2f} {goal_curr})"
            }).execute()

        supabase.table("goals").delete().eq("id", goal_id).execute()

        return web.json_response({"status": "success", "message": f"Ціль «{title}» закрито!"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- CRUD РАХУНКІВ ТА ЦІЛЕЙ ---

async def handle_create_account(request: web.Request):
    try:
        data = await request.json()
        tg_id = int(data.get("telegram_id"))
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        res = supabase.table("accounts").insert({
            "user_id": user_uuid,
            "name": data.get("name"),
            "account_type": data.get("account_type", "card"),
            "currency": str(data.get("currency", "UAH")).upper(),
            "balance": float(data.get("balance", 0.0))
        }).execute()
        return web.json_response({"status": "success", "account": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_update_account(request: web.Request):
    try:
        acc_id = request.match_info.get("id")
        data = await request.json()
        res = supabase.table("accounts").update({
            "name": data.get("name"),
            "account_type": data.get("account_type"),
            "currency": str(data.get("currency", "UAH")).upper(),
            "balance": float(data.get("balance", 0.0))
        }).eq("id", acc_id).execute()
        return web.json_response({"status": "updated", "account": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_delete_account(request: web.Request):
    try:
        acc_id = request.match_info.get("id")
        supabase.table("accounts").delete().eq("id", acc_id).execute()
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_create_goal(request: web.Request):
    try:
        data = await request.json()
        tg_id = int(data.get("telegram_id"))
        user_uuid = get_user_uuid_by_tg_id(tg_id)
        if not user_uuid:
            return web.json_response({"error": "User not found"}, status=404)

        res = supabase.table("goals").insert({
            "user_id": user_uuid,
            "title": data.get("title"),
            "target_amount": float(data.get("target_amount", 0)),
            "current_amount": float(data.get("current_amount", 0)),
            "currency": str(data.get("currency", "UAH")).upper(),
            "deadline": data.get("deadline") or None
        }).execute()
        return web.json_response({"status": "success", "goal": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_update_goal(request: web.Request):
    try:
        goal_id = request.match_info.get("id")
        data = await request.json()
        res = supabase.table("goals").update({
            "title": data.get("title"),
            "target_amount": float(data.get("target_amount", 0)),
            "currency": str(data.get("currency", "UAH")).upper(),
            "deadline": data.get("deadline") or None
        }).eq("id", goal_id).execute()
        return web.json_response({"status": "updated", "goal": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_delete_goal(request: web.Request):
    try:
        goal_id = request.match_info.get("id")
        supabase.table("goals").delete().eq("id", goal_id).execute()
        return web.json_response({"status": "deleted"})
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
        asset_class = data.get("asset_class", "stock")

        payload = {
            "user_id": user_uuid,
            "name": data.get("name") or data.get("ticker", "").upper(),
            "ticker": data.get("ticker", "").upper(),
            "asset_class": asset_class,
            "quantity": qty,
            "buy_price_avg": buy_price,
            "current_price": cur_price,
            "currency": str(data.get("currency", "USD")).upper(),
            "interest_rate": float(data.get("interest_rate", 0.0) or 0.0),
            "maturity_date": data.get("maturity_date") or None,
            "term_months": int(data.get("term_months", 0)) if data.get("term_months") else None
        }

        res = supabase.table("investments").insert(payload).execute()
        return web.json_response({"status": "success", "investment": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_update_investment(request: web.Request):
    try:
        inv_id = request.match_info.get("id")
        data = await request.json()
        payload = {
            "name": data.get("name") or data.get("ticker", "").upper(),
            "ticker": data.get("ticker", "").upper(),
            "asset_class": data.get("asset_class"),
            "quantity": float(data.get("quantity", 0)),
            "buy_price_avg": float(data.get("buy_price_avg", 0)),
            "current_price": float(data.get("current_price", 0)),
            "currency": str(data.get("currency", "USD")).upper(),
            "interest_rate": float(data.get("interest_rate", 0.0) or 0.0),
            "maturity_date": data.get("maturity_date") or None,
            "term_months": int(data.get("term_months", 0)) if data.get("term_months") else None,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        res = supabase.table("investments").update(payload).eq("id", inv_id).execute()
        return web.json_response({"status": "updated", "investment": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def handle_delete_investment(request: web.Request):
    try:
        inv_id = request.match_info.get("id")
        supabase.table("investments").delete().eq("id", inv_id).execute()
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- BOT COMMANDS ---
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
        logging.warning(f"Menu button err: {e}")

    await message.answer(
        f"Вітаю, {user.first_name}! 💼\n\n"
        "<b>FinTrack Pro</b> готовий до роботи:\n\n"
        "⚡ <b>Швидкий запис у чаті:</b>\n"
        "• <code>Кава 75</code> або <code>Сільпо 420</code>\n"
        "• <code>+25000 Зарплата</code>\n\n"
        "📋 <b>Команди:</b>\n"
        "• /balance — залишки на рахунках\n"
        "• /today — доходи та витрати за день\n"
        "• /month — повний звіт за поточний місяць\n\n"
        "Відкривайте додаток кнопкою <b>«Капітал»</b> зліва внизу 🚀",
        parse_mode="HTML"
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку натисніть /start")
        return

    accs = supabase.table("accounts").select("*").eq("user_id", user_uuid).execute().data
    text = "💳 <b>Залишки на ваших рахунках:</b>\n\n"
    total = 0.0
    for a in accs:
        b = float(a["balance"])
        total += b
        text += f"• {a['name']}: <b>{b:,.2f} {a['currency']}</b>\n"
    text += f"\n💰 <b>Сумарно: {total:,.2f} UAH</b>"
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
        f"💰 Чистий результат: <b>{(inc - exp):,.2f} грн</b>",
        parse_mode="HTML"
    )

@dp.message(Command("month"))
async def cmd_month(message: Message):
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        await message.answer("Спочатку запустіть бота через /start")
        return

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    txs = supabase.table("transactions").select("*").eq("user_id", user_uuid).gte("transaction_date", month_start).execute().data

    income = sum(float(t["amount"]) for t in txs if t["type"] == "income")
    expense = sum(float(t["amount"]) for t in txs if t["type"] == "expense")
    net = income - expense
    month_name = now.strftime("%B %Y")

    await message.answer(
        f"📊 <b>Фінансовий звіт за {month_name}:</b>\n\n"
        f"🟢 Доходи: <b>+{income:,.2f} грн</b>\n"
        f"🔴 Витрати: <b>-{expense:,.2f} грн</b>\n"
        f"────────────────────\n"
        f"💵 Чисте сальдо: <b>{'+' if net >= 0 else ''}{net:,.2f} грн</b>\n"
        f"📝 Операцій: <b>{len(txs)}</b>",
        parse_mode="HTML"
    )

@dp.message(F.text.regexp(r"^\+(\d+(?:\.\d+)?)\s*(.*)$"))
async def handle_quick_income(message: Message):
    match = message.text.split(maxsplit=1)
    amount = float(match[0].replace("+", ""))
    desc = match[1] if len(match) > 1 else "Дохід"
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return

    accs = supabase.table("accounts").select("id, balance").eq("user_id", user_uuid).limit(1).execute().data
    if accs:
        acc = accs[0]
        supabase.table("accounts").update({"balance": float(acc["balance"]) + amount}).eq("id", acc["id"]).execute()
        supabase.table("transactions").insert({
            "user_id": user_uuid, "account_id": acc["id"], "amount": amount, "type": "income", "note": desc
        }).execute()
        await message.reply(f"🟢 <b>Дохід додано:</b> +{amount:,.2f} грн\nОпис: <i>{desc}</i>", parse_mode="HTML")

@dp.message(F.text.regexp(r"^(.+)\s+(\d+(?:\.\d+)?)$"))
async def handle_quick_expense(message: Message):
    parts = message.text.rsplit(maxsplit=1)
    desc = parts[0]
    amount = float(parts[1])
    user_uuid = get_user_uuid_by_tg_id(message.from_user.id)
    if not user_uuid:
        return

    accs = supabase.table("accounts").select("id, balance").eq("user_id", user_uuid).limit(1).execute().data
    if accs:
        acc = accs[0]
        supabase.table("accounts").update({"balance": float(acc["balance"]) - amount}).eq("id", acc["id"]).execute()
        supabase.table("transactions").insert({
            "user_id": user_uuid, "account_id": acc["id"], "amount": amount, "type": "expense", "note": desc
        }).execute()
        await message.reply(f"🔴 <b>Витрату записано:</b> -{amount:,.2f} грн\nОпис: <i>{desc}</i>", parse_mode="HTML")

async def main():
    asyncio.create_task(update_rates_from_nbu())

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/v1/currencies", handle_get_currencies)
    app.router.add_get("/api/v1/categories", handle_get_categories)
    app.router.add_get("/api/v1/dashboard/summary", handle_dashboard_summary)
    app.router.add_post("/api/v1/transactions", handle_create_transaction)
    
    app.router.add_get("/api/v1/market/search", handle_market_search)
    app.router.add_get("/api/v1/market/quote", handle_market_quote)
    app.router.add_post("/api/v1/investments/refresh-prices", handle_refresh_investment_prices)

    app.router.add_post("/api/v1/accounts", handle_create_account)
    app.router.add_put("/api/v1/accounts/{id}", handle_update_account)
    app.router.add_delete("/api/v1/accounts/{id}", handle_delete_account)
    
    app.router.add_post("/api/v1/goals", handle_create_goal)
    app.router.add_put("/api/v1/goals/{id}", handle_update_goal)
    app.router.add_post("/api/v1/goals/funds", handle_modify_goal_funds)
    app.router.add_post("/api/v1/goals/{id}/close", handle_close_goal)
    app.router.add_delete("/api/v1/goals/{id}", handle_delete_goal)

    app.router.add_post("/api/v1/investments", handle_create_investment)
    app.router.add_put("/api/v1/investments/{id}", handle_update_investment)
    app.router.add_delete("/api/v1/investments/{id}", handle_delete_investment)

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