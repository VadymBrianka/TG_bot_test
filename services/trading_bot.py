import asyncio
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
import aiohttp

from db import supabase, run_db
from services.market_quotes import get_realtime_price


async def get_or_create_trading_wallet(user_uuid: str) -> dict[str, Any]:
    def _q():
        res = supabase.table("trading_wallets").select("*").eq("user_id", user_uuid).limit(1).execute().data
        if res:
            return res[0]
        ins = (
            supabase.table("trading_wallets")
            .insert({"user_id": user_uuid, "demo_usdt": 10000.0, "real_usdt": 0.0})
            .execute()
            .data
        )
        return ins[0]
    return await run_db(_q)


async def fetch_rsi_14(symbol: str) -> float | None:
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval=15m&limit=20"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                klines = await resp.json()
        closes = [float(k[4]) for k in klines]
        if len(closes) < 15:
            return None
        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains[-14:]) / 14.0
        avg_loss = sum(losses[-14:]) / 14.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 2)
    except Exception:
        return None


async def execute_binance_spot_order(
    api_key: str,
    api_secret: str,
    symbol: str,
    side: str,
    quote_usdt: float | None = None,
    quantity: float | None = None,
) -> tuple[bool, str]:
    """Execute a live Binance Spot MARKET order when user has configured Binance API keys."""
    if not api_key or not api_secret:
        return False, "No Binance keys"
    try:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": "MARKET",
            "timestamp": int(time.time() * 1000),
        }
        if side.upper() == "BUY" and quote_usdt:
            params["quoteOrderQty"] = round(quote_usdt, 2)
        elif quantity:
            params["quantity"] = round(quantity, 6)

        query = urlencode(params)
        sig = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"https://api.binance.com/api/v3/order?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": api_key}

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(url, headers=headers) as resp:
                data = await resp.json()
                if resp.status == 200:
                    return True, f"Binance Order #{data.get('orderId')}"
                return False, data.get("msg", f"HTTP {resp.status}")
    except Exception as e:
        return False, str(e)


async def step_trading_bot(bot_row: dict[str, Any], tg_bot=None, user_tg_id: int | None = None) -> dict[str, Any]:
    """Evaluate one trading bot tick against live market prices and execute BUY/SELL if triggered."""
    user_uuid = bot_row["user_id"]
    bot_id = bot_row["id"]
    mode = bot_row.get("mode", "demo")
    symbol = str(bot_row.get("symbol", "BTCUSDT")).upper()
    ticker = symbol.replace("USDT", "")
    strategy = bot_row.get("strategy", "tp_sl")
    order_usdt = float(bot_row.get("order_usdt") or 50.0)
    tp_pct = float(bot_row.get("take_profit_pct") or 3.0)
    sl_pct = float(bot_row.get("stop_loss_pct") or 2.0)

    pos_qty = float(bot_row.get("position_qty") or 0.0)
    entry_price = float(bot_row.get("entry_price") or 0.0)
    realized_pnl = float(bot_row.get("realized_pnl") or 0.0)

    price = await get_realtime_price(ticker, "crypto", use_cache=False)
    if not price or price <= 0:
        return {"action": "hold", "reason": "Ціна тимчасово недоступна"}

    wallet = await get_or_create_trading_wallet(user_uuid)
    bal_col = "real_usdt" if mode == "real" else "demo_usdt"
    bal = float(wallet.get(bal_col) or 0.0)

    use_binance_api = (
        mode == "real"
        and bool(wallet.get("binance_api_key"))
        and bool(wallet.get("binance_api_secret"))
    )

    async def _buy(reason: str):
        nonlocal bal, pos_qty, entry_price
        if bal < order_usdt:
            return {"action": "hold", "reason": f"Недостатньо {mode.upper()} USDT (потрібно {order_usdt})"}

        if use_binance_api:
            ok, msg = await execute_binance_spot_order(
                wallet["binance_api_key"], wallet["binance_api_secret"], symbol, "BUY", quote_usdt=order_usdt
            )
            if not ok:
                return {"action": "error", "reason": f"Binance API: {msg}"}

        bought_qty = order_usdt / price
        new_qty = pos_qty + bought_qty
        new_entry = ((pos_qty * entry_price) + order_usdt) / new_qty if new_qty > 0 else price
        new_bal = round(bal - order_usdt, 4)

        def _db_buy():
            supabase.table("trading_wallets").update({bal_col: new_bal}).eq("user_id", user_uuid).execute()
            supabase.table("trading_bots").update(
                {
                    "position_qty": round(new_qty, 8),
                    "entry_price": round(new_entry, 4),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", bot_id).execute()
            supabase.table("trading_orders").insert(
                {
                    "bot_id": bot_id,
                    "user_id": user_uuid,
                    "mode": mode,
                    "symbol": symbol,
                    "side": "BUY",
                    "price": price,
                    "quantity": round(bought_qty, 8),
                    "usdt_amount": order_usdt,
                    "pnl": 0,
                    "reason": reason,
                }
            ).execute()

        await run_db(_db_buy)
        if tg_bot and user_tg_id:
            try:
                await tg_bot.send_message(
                    user_tg_id,
                    f"🤖 <b>[{mode.upper()}] Купівля {symbol}</b>\n"
                    f"💵 Сума: <b>{order_usdt:.2f} USDT</b> @ <b>${price:,.4f}</b>\n"
                    f"📌 Причина: <i>{reason}</i>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return {"action": "BUY", "price": price, "quantity": round(bought_qty, 6), "reason": reason}

    async def _sell(reason: str):
        nonlocal bal, pos_qty, entry_price, realized_pnl
        if pos_qty <= 0:
            return {"action": "hold", "reason": "Немає відкритої позиції"}

        if use_binance_api:
            ok, msg = await execute_binance_spot_order(
                wallet["binance_api_key"], wallet["binance_api_secret"], symbol, "SELL", quantity=pos_qty
            )
            if not ok:
                return {"action": "error", "reason": f"Binance API: {msg}"}

        proceeds = pos_qty * price
        cost = pos_qty * entry_price
        pnl = round(proceeds - cost, 4)
        new_bal = round(bal + proceeds, 4)
        new_realized = round(realized_pnl + pnl, 4)

        def _db_sell():
            supabase.table("trading_wallets").update({bal_col: new_bal}).eq("user_id", user_uuid).execute()
            supabase.table("trading_bots").update(
                {
                    "position_qty": 0,
                    "entry_price": 0,
                    "realized_pnl": new_realized,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", bot_id).execute()
            supabase.table("trading_orders").insert(
                {
                    "bot_id": bot_id,
                    "user_id": user_uuid,
                    "mode": mode,
                    "symbol": symbol,
                    "side": "SELL",
                    "price": price,
                    "quantity": round(pos_qty, 8),
                    "usdt_amount": round(proceeds, 4),
                    "pnl": pnl,
                    "reason": reason,
                }
            ).execute()

        await run_db(_db_sell)
        if tg_bot and user_tg_id:
            emoji = "🟢" if pnl >= 0 else "🔴"
            try:
                await tg_bot.send_message(
                    user_tg_id,
                    f"🤖 <b>[{mode.upper()}] Продаж {symbol}</b>\n"
                    f"💵 Виручка: <b>{proceeds:.2f} USDT</b> @ <b>${price:,.4f}</b>\n"
                    f"{emoji} PnL: <b>{pnl:+.2f} USDT</b>\n"
                    f"📌 Причина: <i>{reason}</i>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return {"action": "SELL", "price": price, "pnl": pnl, "reason": reason}

    # Evaluate strategy
    if pos_qty <= 0:
        if strategy == "momentum":
            rsi = await fetch_rsi_14(symbol)
            if rsi is not None and rsi > 65:
                return {"action": "hold", "reason": f"RSI={rsi} (перекупленість >65, чекаємо відкату)"}
            return await _buy(f"Momentum вхід (RSI={rsi or 'N/A'})")
        return await _buy(f"Відкриття позиції ({strategy.upper()})")

    # Position is open -> check TP / SL / DCA
    pnl_pct = ((price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
    if pnl_pct >= tp_pct:
        return await _sell(f"Take Profit (+{pnl_pct:.2f}%)")
    if pnl_pct <= -sl_pct:
        if strategy == "dca" and bal >= order_usdt:
            return await _buy(f"DCA усереднення ({pnl_pct:.2f}%)")
        return await _sell(f"Stop Loss ({pnl_pct:.2f}%)")

    return {"action": "hold", "price": price, "pnl_pct": round(pnl_pct, 2), "reason": f"Позиція активна ({pnl_pct:+.2f}%)"}


async def run_trading_bots_loop(tg_bot) -> None:
    """Background loop executing active VIP trading bots every 60 seconds."""
    while True:
        try:
            bots = await run_db(
                lambda: supabase.table("trading_bots").select("*").eq("is_active", True).execute().data or []
            )
            if bots:
                uids = list({b["user_id"] for b in bots})
                users = await run_db(
                    lambda: supabase.table("users").select("id, telegram_id").in_("id", uids).execute().data or []
                )
                uid_to_tg = {u["id"]: u["telegram_id"] for u in users}
                for b in bots:
                    await step_trading_bot(b, tg_bot=tg_bot, user_tg_id=uid_to_tg.get(b["user_id"]))
        except Exception as e:
            logging.debug(f"Trading bots loop skipped: {e}")
        await asyncio.sleep(60)
