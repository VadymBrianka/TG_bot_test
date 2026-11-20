import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
import aiohttp

from db import supabase, run_db
from services.market_quotes import get_realtime_price

ALL_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]


async def get_or_create_demo_wallet(user_uuid: str) -> dict[str, Any]:
    def _q():
        res = supabase.table("trading_wallets").select("*").eq("user_id", user_uuid).limit(1).execute().data
        if res:
            return res[0]
        ins = supabase.table("trading_wallets").insert({"user_id": user_uuid, "demo_usdt": 10000.0}).execute().data
        return ins[0]
    return await run_db(_q)


async def get_or_create_bot_config(user_uuid: str) -> dict[str, Any]:
    def _q():
        res = supabase.table("trading_bots").select("*").eq("user_id", user_uuid).limit(1).execute().data
        if res:
            return res[0]
        ins = (
            supabase.table("trading_bots")
            .insert({"user_id": user_uuid, "auto_select": True, "symbols": [], "is_active": False})
            .execute()
            .data
        )
        return ins[0]
    return await run_db(_q)


async def _analyze_pair_signal(session: aiohttp.ClientSession, symbol: str) -> dict[str, Any] | None:
    """Analyze 15m Binance klines (RSI-14 + EMA7/EMA21) and score entry attractiveness."""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=30"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            klines = await resp.json()
        closes = [float(k[4]) for k in klines]
        if len(closes) < 21:
            return None

        price = closes[-1]
        # RSI-14
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in diffs[-14:]]
        losses = [max(-d, 0.0) for d in diffs[-14:]]
        avg_g = sum(gains) / 14.0
        avg_l = sum(losses) / 14.0
        rsi = 100.0 if avg_l == 0 else round(100.0 - (100.0 / (1.0 + avg_g / avg_l)), 2)

        ema7 = sum(closes[-7:]) / 7.0
        ema21 = sum(closes[-21:]) / 21.0

        # Higher score = better bullish / pullback entry
        score = 0.0
        strategy_tag = "Momentum"
        if rsi < 38:
            score += (45 - rsi) * 2.0
            strategy_tag = f"Oversold RSI {rsi}"
        elif ema7 > ema21 and rsi < 62:
            score += 25.0 + (62 - rsi) * 0.5
            strategy_tag = f"Uptrend EMA (RSI {rsi})"
        elif rsi <= 65:
            score += 10.0
            strategy_tag = f"Balanced (RSI {rsi})"
        else:
            score -= 20.0  # overbought, avoid buying top

        return {"symbol": symbol, "price": price, "rsi": rsi, "score": score, "strategy": strategy_tag}
    except Exception:
        return None


async def close_position_at_price(
    pos: dict[str, Any],
    exit_price: float,
    reason: str,
    tg_bot=None,
    user_tg_id: int | None = None,
) -> dict[str, Any]:
    user_uuid = pos["user_id"]
    qty = float(pos["quantity"])
    entry_price = float(pos["entry_price"])
    invested = float(pos["usdt_invested"])

    proceeds = round(qty * exit_price, 4)
    pnl_usdt = round(proceeds - invested, 4)
    pnl_pct = round(((exit_price - entry_price) / entry_price) * 100.0, 2) if entry_price > 0 else 0.0

    wallet = await get_or_create_demo_wallet(user_uuid)
    new_bal = round(float(wallet.get("demo_usdt") or 0) + proceeds, 4)
    now_iso = datetime.now(timezone.utc).isoformat()

    def _db():
        supabase.table("trading_wallets").update({"demo_usdt": new_bal, "updated_at": now_iso}).eq("user_id", user_uuid).execute()
        supabase.table("trading_positions").update(
            {
                "exit_price": round(exit_price, 4),
                "pnl_usdt": pnl_usdt,
                "pnl_pct": pnl_pct,
                "status": "closed",
                "close_reason": reason,
                "closed_at": now_iso,
            }
        ).eq("id", pos["id"]).execute()

    await run_db(_db)
    if tg_bot and user_tg_id:
        emoji = "🟢" if pnl_usdt >= 0 else "🔴"
        try:
            await tg_bot.send_message(
                user_tg_id,
                f"🤖 <b>[DEMO] Позицію {pos['symbol']} закрито</b>\n"
                f"Вхід: <b>${entry_price:,.4f}</b> → Вихід: <b>${exit_price:,.4f}</b>\n"
                f"{emoji} PnL: <b>{pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)</b>\n"
                f"📌 {reason}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return {"symbol": pos["symbol"], "pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct, "reason": reason}


async def step_autonomous_bot(user_uuid: str, tg_bot=None, user_tg_id: int | None = None) -> dict[str, Any]:
    """Run one autonomous trading cycle: manage open positions + open new profitable setups."""
    wallet = await get_or_create_demo_wallet(user_uuid)
    cfg = await get_or_create_bot_config(user_uuid)
    demo_bal = float(wallet.get("demo_usdt") or 0.0)

    open_positions = await run_db(
        lambda: supabase.table("trading_positions")
        .select("*")
        .eq("user_id", user_uuid)
        .eq("status", "open")
        .execute()
        .data
        or []
    )

    closed_now = []
    for pos in open_positions:
        ticker = str(pos["symbol"]).replace("USDT", "")
        cur_price = await get_realtime_price(ticker, "crypto", use_cache=False)
        if not cur_price or cur_price <= 0:
            continue
        entry = float(pos["entry_price"])
        tp = float(pos.get("tp_pct") or 2.8)
        sl = float(pos.get("sl_pct") or 1.8)
        chg_pct = ((cur_price - entry) / entry) * 100.0 if entry > 0 else 0.0

        if chg_pct >= tp:
            closed_now.append(await close_position_at_price(pos, cur_price, f"Take Profit (+{chg_pct:.2f}%)", tg_bot, user_tg_id))
        elif chg_pct <= -sl:
            closed_now.append(await close_position_at_price(pos, cur_price, f"Stop Loss ({chg_pct:.2f}%)", tg_bot, user_tg_id))

    # Refresh wallet & open positions count
    wallet = await get_or_create_demo_wallet(user_uuid)
    demo_bal = float(wallet.get("demo_usdt") or 0.0)
    open_positions = await run_db(
        lambda: supabase.table("trading_positions")
        .select("*")
        .eq("user_id", user_uuid)
        .eq("status", "open")
        .execute()
        .data
        or []
    )

    opened_now = []
    open_symbols = {p["symbol"] for p in open_positions}
    max_concurrent = 4

    if len(open_positions) < max_concurrent and demo_bal >= 50.0:
        auto_select = bool(cfg.get("auto_select", True))
        chosen = [str(s).upper() for s in (cfg.get("symbols") or []) if s]
        candidates = ALL_PAIRS if (auto_select or not chosen) else chosen
        candidates = [s for s in candidates if s not in open_symbols]

        if candidates:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as session:
                signals = await asyncio.gather(*(_analyze_pair_signal(session, s) for s in candidates))
            valid = [s for s in signals if s is not None and s["score"] > 0]
            if not valid:
                # Fallback to live price on first candidate if klines unavailable
                for s in candidates[:2]:
                    p = await get_realtime_price(s.replace("USDT", ""), "crypto", use_cache=False)
                    if p and p > 0:
                        valid.append({"symbol": s, "price": p, "rsi": 50.0, "score": 10.0, "strategy": "Market Entry"})
                        break

            if valid:
                best = max(valid, key=lambda x: x["score"])
                # Dynamic position sizing: 8%..12% of demo balance based on signal strength
                alloc_pct = 0.12 if best["score"] >= 25 else 0.08
                invest_usdt = round(min(max(demo_bal * alloc_pct, 50.0), min(demo_bal, 1500.0)), 2)
                entry_price = float(best["price"])
                qty = round(invest_usdt / entry_price, 8)
                new_bal = round(demo_bal - invest_usdt, 4)

                def _open():
                    supabase.table("trading_wallets").update(
                        {"demo_usdt": new_bal, "updated_at": datetime.now(timezone.utc).isoformat()}
                    ).eq("user_id", user_uuid).execute()
                    return (
                        supabase.table("trading_positions")
                        .insert(
                            {
                                "user_id": user_uuid,
                                "symbol": best["symbol"],
                                "strategy": best["strategy"],
                                "usdt_invested": invest_usdt,
                                "quantity": qty,
                                "entry_price": round(entry_price, 4),
                                "tp_pct": 2.8,
                                "sl_pct": 1.8,
                                "status": "open",
                            }
                        )
                        .execute()
                        .data[0]
                    )

                pos_row = await run_db(_open)
                opened_now.append(pos_row)
                if tg_bot and user_tg_id:
                    try:
                        await tg_bot.send_message(
                            user_tg_id,
                            f"🤖 <b>[DEMO] Відкрито позицію {best['symbol']}</b>\n"
                            f"💵 Сума: <b>{invest_usdt:.2f} USDT</b> @ <b>${entry_price:,.4f}</b>\n"
                            f"📊 Сигнал: <i>{best['strategy']}</i>\n"
                            f"🎯 TP: +2.8% | 🛡 SL: -1.8%",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

    return {"opened": len(opened_now), "closed": len(closed_now), "demo_usdt": demo_bal}


async def run_trading_bots_loop(tg_bot) -> None:
    """Background loop executing active VIP demo trading bots every 60 seconds."""
    while True:
        try:
            bots = await run_db(
                lambda: supabase.table("trading_bots").select("user_id").eq("is_active", True).execute().data or []
            )
            if bots:
                uids = list({b["user_id"] for b in bots})
                users = await run_db(
                    lambda: supabase.table("users").select("id, telegram_id").in_("id", uids).execute().data or []
                )
                uid_to_tg = {u["id"]: u["telegram_id"] for u in users}
                for uid in uids:
                    await step_autonomous_bot(uid, tg_bot=tg_bot, user_tg_id=uid_to_tg.get(uid))
        except Exception as e:
            logging.debug(f"Trading loop skipped: {e}")
        await asyncio.sleep(60)
