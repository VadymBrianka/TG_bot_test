import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
import aiohttp

from db import supabase, run_db
from services.market_quotes import get_realtime_price

ALL_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "SUIUSDT",
]

RISK_PROFILES: dict[str, dict[str, Any]] = {
    "conservative": {"alloc_pct": 0.08, "tp_pct": 1.4, "sl_pct": 0.9, "max_positions": 4, "max_per_tick": 2, "leverage": 2},
    "balanced": {"alloc_pct": 0.14, "tp_pct": 2.0, "sl_pct": 1.3, "max_positions": 6, "max_per_tick": 3, "leverage": 5},
    "aggressive": {"alloc_pct": 0.22, "tp_pct": 2.8, "sl_pct": 1.8, "max_positions": 10, "max_per_tick": 4, "leverage": 10},
}


def _calc_ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


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
        base_payload = {
            "user_id": user_uuid,
            "auto_select": True,
            "symbols": [],
            "risk_profile": "balanced",
            "leverage": 5,
            "is_active": False,
        }
        try:
            ins = supabase.table("trading_bots").insert(base_payload).execute().data
            return ins[0]
        except Exception as e:
            if "order_usdt" in str(e):
                legacy_payload = {**base_payload, "order_usdt": 100.0}
                ins = supabase.table("trading_bots").insert(legacy_payload).execute().data
                return ins[0]
            raise
    return await run_db(_q)


async def _analyze_pair_signal(session: aiohttp.ClientSession, symbol: str) -> dict[str, Any] | None:
    """Analyze 5m Binance klines (RSI-14 + true EMA9/EMA21 + Bollinger pullback + volume surge)."""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=35"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            klines = await resp.json()
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        if len(closes) < 22:
            return None

        price = closes[-1]
        # RSI-14
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in diffs[-14:]]
        losses = [max(-d, 0.0) for d in diffs[-14:]]
        avg_g = sum(gains) / 14.0
        avg_l = sum(losses) / 14.0
        rsi = 100.0 if avg_l == 0 else round(100.0 - (100.0 / (1.0 + avg_g / avg_l)), 2)

        ema9 = _calc_ema(closes[-21:], 9)
        ema21 = _calc_ema(closes[-25:], 21)

        # Bollinger Bands (20)
        win20 = closes[-20:]
        sma20 = sum(win20) / 20.0
        std20 = (sum((x - sma20) ** 2 for x in win20) / 20.0) ** 0.5
        lower_bb = sma20 - 1.8 * std20
        upper_bb = sma20 + 1.8 * std20

        # Volume surge
        avg_vol = sum(volumes[-20:-1]) / max(len(volumes[-20:-1]), 1)
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

        # Evaluate LONG score
        long_score = 0.0
        long_tag = "Long Scalp"
        if price <= lower_bb and rsi < 42:
            long_score += 45.0 + (45.0 - rsi) * 1.5
            long_tag = f"BB Bounce (RSI {rsi})"
        elif rsi < 38:
            long_score += (46.0 - rsi) * 2.2
            long_tag = f"Oversold Long (RSI {rsi})"
        elif ema9 > ema21 and rsi < 66:
            long_score += 28.0 + (66.0 - rsi) * 0.6
            long_tag = f"EMA9>21 Uptrend (RSI {rsi})"
        elif closes[-1] > closes[-2] and rsi <= 68:
            long_score += 14.0
            long_tag = f"Bull Pulse (RSI {rsi})"

        # Evaluate SHORT score
        short_score = 0.0
        short_tag = "Short Scalp"
        if price >= upper_bb and rsi > 58:
            short_score += 45.0 + (rsi - 55.0) * 1.5
            short_tag = f"BB Reject Short (RSI {rsi})"
        elif rsi > 62:
            short_score += (rsi - 54.0) * 2.2
            short_tag = f"Overbought Short (RSI {rsi})"
        elif ema9 < ema21 and rsi > 34:
            short_score += 28.0 + (rsi - 34.0) * 0.6
            short_tag = f"EMA9<21 Downtrend (RSI {rsi})"
        elif closes[-1] < closes[-2] and rsi >= 32:
            short_score += 14.0
            short_tag = f"Bear Breakdown (RSI {rsi})"

        if short_score > long_score:
            side = "short"
            score = short_score
            strategy_tag = short_tag
        else:
            side = "long"
            score = long_score
            strategy_tag = long_tag

        if vol_ratio >= 1.35 and score > 0:
            score += min(15.0, (vol_ratio - 1.0) * 10.0)
            strategy_tag += f" • Vol x{vol_ratio:.1f}"

        return {"symbol": symbol, "side": side, "price": price, "rsi": rsi, "score": score, "strategy": strategy_tag}
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
    entry_price = float(pos["entry_price"])
    invested = float(pos["usdt_invested"])
    side = str(pos.get("side") or "long").lower()
    leverage = max(1, int(pos.get("leverage") or 1))

    if entry_price > 0:
        if side == "short":
            price_chg_pct = ((entry_price - exit_price) / entry_price) * 100.0
        else:
            price_chg_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:
        price_chg_pct = 0.0

    roe_pct = round(price_chg_pct * leverage, 2)
    pnl_usdt = round(invested * (roe_pct / 100.0), 4)
    proceeds = max(0.0, round(invested + pnl_usdt, 4))
    pnl_usdt = round(proceeds - invested, 4)
    pnl_pct = roe_pct

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
        tp = float(pos.get("tp_pct") or 2.0)
        sl = float(pos.get("sl_pct") or 1.3)
        side = str(pos.get("side") or "long").lower()
        lev = max(1, int(pos.get("leverage") or 1))
        if entry > 0:
            chg_pct = ((entry - cur_price) / entry) * 100.0 if side == "short" else ((cur_price - entry) / entry) * 100.0
        else:
            chg_pct = 0.0
        roe_pct = chg_pct * lev

        if roe_pct <= -95.0:
            closed_now.append(await close_position_at_price(pos, cur_price, f"Liquidation ({roe_pct:.1f}% ROE)", tg_bot, user_tg_id))
        elif chg_pct >= tp:
            closed_now.append(await close_position_at_price(pos, cur_price, f"Take Profit (+{roe_pct:.1f}% ROE)", tg_bot, user_tg_id))
        elif chg_pct >= tp * 0.65 and chg_pct >= 0.85:
            # Lock in partial/quick profit if position is already well in green (>65% of TP)
            try:
                opened_dt = datetime.fromisoformat(str(pos.get("opened_at") or "").replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 60.0
            except Exception:
                age_min = 0.0
            if age_min >= 10:
                closed_now.append(await close_position_at_price(pos, cur_price, f"Smart Profit Lock (+{roe_pct:.1f}% ROE)", tg_bot, user_tg_id))
        elif chg_pct <= -sl:
            closed_now.append(await close_position_at_price(pos, cur_price, f"Stop Loss ({roe_pct:.1f}% ROE)", tg_bot, user_tg_id))

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
    profile_key = str(cfg.get("risk_profile") or "balanced").lower()
    profile = RISK_PROFILES.get(profile_key, RISK_PROFILES["balanced"])
    max_concurrent = int(profile["max_positions"])

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
                valid.sort(key=lambda x: x["score"], reverse=True)
                slots_left = max_concurrent - len(open_positions)
                per_tick = min(slots_left, int(profile.get("max_per_tick", 3)))
                picks = valid[:per_tick]
                alloc_pct = float(profile["alloc_pct"])
                tp_val = float(profile["tp_pct"])
                sl_val = float(profile["sl_pct"])
                leverage_val = max(1, min(20, int(cfg.get("leverage") or profile.get("leverage", 5))))

                for best in picks:
                    if demo_bal < 50.0:
                        break
                    invest_usdt = round(min(max(demo_bal * alloc_pct, 50.0), min(demo_bal, 3000.0)), 2)
                    entry_price = float(best["price"])
                    side_val = str(best.get("side") or "long").lower()
                    qty = round((invest_usdt * leverage_val) / entry_price, 8)
                    demo_bal = round(demo_bal - invest_usdt, 4)

                    def _open(b=best, inv=invest_usdt, ep=entry_price, q=qty, nb=demo_bal, sd=side_val, lv=leverage_val):
                        supabase.table("trading_wallets").update(
                            {"demo_usdt": nb, "updated_at": datetime.now(timezone.utc).isoformat()}
                        ).eq("user_id", user_uuid).execute()
                        return (
                            supabase.table("trading_positions")
                            .insert(
                                {
                                    "user_id": user_uuid,
                                    "symbol": b["symbol"],
                                    "side": sd,
                                    "leverage": lv,
                                    "strategy": f"{b['strategy']} ({profile_key})",
                                    "usdt_invested": inv,
                                    "quantity": q,
                                    "entry_price": round(ep, 4),
                                    "tp_pct": tp_val,
                                    "sl_pct": sl_val,
                                    "status": "open",
                                }
                            )
                            .execute()
                            .data[0]
                        )

                    pos_row = await run_db(_open)
                    opened_now.append(pos_row)
                    if tg_bot and user_tg_id:
                        side_badge = "🟢 LONG" if side_val == "long" else "🔴 SHORT"
                        try:
                            await tg_bot.send_message(
                                user_tg_id,
                                f"🤖 <b>[DEMO] {side_badge} {leverage_val}x • {best['symbol']}</b>\n"
                                f"💵 Маржа: <b>{invest_usdt:.2f} USDT</b> (Обсяг: ${invest_usdt * leverage_val:,.0f})\n"
                                f"📍 Вхід: <b>${entry_price:,.4f}</b>\n"
                                f"📊 Сигнал: <i>{best['strategy']}</i>\n"
                                f"🎯 TP: +{tp_val * leverage_val:.1f}% ROE | 🛡 SL: -{sl_val * leverage_val:.1f}% ROE",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass

    return {"opened": len(opened_now), "closed": len(closed_now), "demo_usdt": demo_bal}


async def run_trading_bots_loop(tg_bot) -> None:
    """Background loop executing active VIP demo trading bots every 60 seconds."""
    while True:
        try:
            now_dt = datetime.now(timezone.utc)
            # Distributed dedup guard across overlapping Render containers
            def _claim_tick():
                res = supabase.table("app_settings").select("value").eq("key", "trading_last_tick").limit(1).execute().data
                if res and res[0].get("value"):
                    try:
                        prev = datetime.fromisoformat(str(res[0]["value"]).replace("Z", "+00:00"))
                        if (now_dt - prev).total_seconds() < 25:
                            return False
                    except Exception:
                        pass
                supabase.table("app_settings").upsert(
                    {"key": "trading_last_tick", "value": now_dt.isoformat(), "updated_at": now_dt.isoformat()},
                    on_conflict="key",
                ).execute()
                return True

            if await run_db(_claim_tick):
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
        await asyncio.sleep(30)
