import asyncio
from datetime import datetime, timezone
from aiohttp import web

from db import supabase, run_db
from services.subscriptions import require_plan
from services.market_quotes import get_realtime_price
from services.trading_bot import (
    ALL_PAIRS,
    get_or_create_demo_wallet,
    get_or_create_bot_config,
    step_autonomous_bot,
    close_position_at_price,
)


async def handle_trading_overview(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        wallet = await get_or_create_demo_wallet(user_uuid)
        cfg = await get_or_create_bot_config(user_uuid)

        open_rows = await run_db(
            lambda: supabase.table("trading_positions")
            .select("*")
            .eq("user_id", user_uuid)
            .eq("status", "open")
            .order("opened_at", desc=True)
            .execute()
            .data
            or []
        )
        closed_rows = await run_db(
            lambda: supabase.table("trading_positions")
            .select("*")
            .eq("user_id", user_uuid)
            .eq("status", "closed")
            .order("closed_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )

        # Enrich open positions with live prices & unrealized PnL
        if open_rows:
            tickers = list({str(p["symbol"]).replace("USDT", "") for p in open_rows})
            prices_list = await asyncio.gather(*(get_realtime_price(t, "crypto", use_cache=True) for t in tickers))
            price_map = {t: p for t, p in zip(tickers, prices_list)}
            for p in open_rows:
                t = str(p["symbol"]).replace("USDT", "")
                cur_p = price_map.get(t) or float(p["entry_price"])
                entry_p = float(p["entry_price"])
                qty = float(p["quantity"])
                invested = float(p["usdt_invested"])
                cur_val = qty * cur_p
                unr_pnl = round(cur_val - invested, 2)
                unr_pct = round(((cur_p - entry_p) / entry_p) * 100.0, 2) if entry_p > 0 else 0.0
                p["current_price"] = round(cur_p, 4)
                p["unrealized_pnl"] = unr_pnl
                p["unrealized_pct"] = unr_pct

        total_realized = round(sum(float(c.get("pnl_usdt") or 0) for c in closed_rows), 2)
        wins = sum(1 for c in closed_rows if float(c.get("pnl_usdt") or 0) > 0)
        win_rate = round((wins / len(closed_rows)) * 100, 1) if closed_rows else 0.0

        # Build Equity Curve chronologically
        equity_curve = []
        cum = 10000.0
        for c in reversed(closed_rows):
            cum = round(cum + float(c.get("pnl_usdt") or 0), 2)
            equity_curve.append({"date": str(c.get("closed_at") or "")[:16], "equity": cum})

        return web.json_response(
            {
                "demo_usdt": float(wallet.get("demo_usdt") or 0),
                "bot": cfg,
                "all_pairs": ALL_PAIRS,
                "open_positions": open_rows,
                "closed_positions": closed_rows,
                "equity_curve": equity_curve,
                "stats": {
                    "total_realized_pnl": total_realized,
                    "win_rate": win_rate,
                    "total_closed": len(closed_rows),
                },
            }
        )
    except Exception as e:
        return web.json_response({"error": f"Виконайте migrations.sql у Supabase: {e}"}, status=400)


async def handle_trading_save_bot(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    await get_or_create_bot_config(user_uuid)
    auto_select = bool(body.get("auto_select", True))
    symbols = [str(s).upper() for s in (body.get("symbols") or []) if s]
    risk_profile = str(body.get("risk_profile") or "balanced").lower()
    if risk_profile not in ("conservative", "balanced", "aggressive"):
        risk_profile = "balanced"
    is_active = bool(body.get("is_active", True))

    res = await run_db(
        lambda: supabase.table("trading_bots")
        .update(
            {
                "auto_select": auto_select,
                "symbols": symbols,
                "risk_profile": risk_profile,
                "is_active": is_active,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("user_id", user_uuid)
        .execute()
    )
    return web.json_response({"status": "saved", "bot": res.data[0] if res.data else {}})


async def handle_trading_step(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    res = await step_autonomous_bot(user_uuid, tg_bot=request.app.get("bot"), user_tg_id=request["tg_id"])
    return web.json_response(res)


async def handle_trading_close_position(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    pos_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    rows = await run_db(
        lambda: supabase.table("trading_positions")
        .select("*")
        .eq("id", pos_id)
        .eq("user_id", user_uuid)
        .eq("status", "open")
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return web.json_response({"error": "Позицію не знайдено"}, status=404)

    pos = rows[0]
    cur_price = await get_realtime_price(str(pos["symbol"]).replace("USDT", ""), "crypto", use_cache=False)
    if not cur_price or cur_price <= 0:
        cur_price = float(pos["entry_price"])

    res = await close_position_at_price(pos, cur_price, "Закрито вручну", request.app.get("bot"), request["tg_id"])
    return web.json_response({"status": "closed", **res})


async def handle_trading_demo_reset(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    await get_or_create_demo_wallet(user_uuid)
    await run_db(
        lambda: supabase.table("trading_positions").delete().eq("user_id", user_uuid).eq("status", "open").execute()
    )
    await run_db(
        lambda: supabase.table("trading_wallets")
        .update({"demo_usdt": 10000.0, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("user_id", user_uuid)
        .execute()
    )
    return web.json_response({"status": "reset", "demo_usdt": 10000.0})


async def handle_trading_leaderboard(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    def _load():
        closed = (
            supabase.table("trading_positions")
            .select("user_id, pnl_usdt")
            .eq("status", "closed")
            .execute()
            .data
            or []
        )
        if not closed:
            return []
        pnl_by_user: dict[str, dict] = {}
        for c in closed:
            uid = c["user_id"]
            if uid not in pnl_by_user:
                pnl_by_user[uid] = {"pnl": 0.0, "trades": 0, "wins": 0}
            p = float(c.get("pnl_usdt") or 0)
            pnl_by_user[uid]["pnl"] += p
            pnl_by_user[uid]["trades"] += 1
            if p > 0:
                pnl_by_user[uid]["wins"] += 1

        uids = list(pnl_by_user.keys())
        users = supabase.table("users").select("id, first_name").in_("id", uids).execute().data or []
        names = {u["id"]: (u.get("first_name") or "Трейдер") for u in users}

        board = []
        for uid, st in pnl_by_user.items():
            board.append(
                {
                    "name": names.get(uid, "Трейдер"),
                    "pnl_usdt": round(st["pnl"], 2),
                    "roi_pct": round((st["pnl"] / 10000.0) * 100.0, 2),
                    "trades": st["trades"],
                    "win_rate": round((st["wins"] / st["trades"]) * 100, 1) if st["trades"] else 0.0,
                }
            )
        return sorted(board, key=lambda x: x["pnl_usdt"], reverse=True)[:10]

    board = await run_db(_load)
    return web.json_response({"leaderboard": board})
