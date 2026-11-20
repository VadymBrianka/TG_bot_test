import logging
from datetime import datetime, timezone
from aiohttp import web

from db import supabase, run_db, get_exchange_rates, create_transaction_atomic
from services.subscriptions import require_plan, get_crypto_wallets
from services.blockchain_verify import verify_crypto_payment
from services.trading_bot import get_or_create_trading_wallet, step_trading_bot


async def handle_trading_overview(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        wallet = await get_or_create_trading_wallet(user_uuid)
        bots = await run_db(
            lambda: supabase.table("trading_bots")
            .select("*")
            .eq("user_id", user_uuid)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        orders = await run_db(
            lambda: supabase.table("trading_orders")
            .select("*")
            .eq("user_id", user_uuid)
            .order("created_at", desc=True)
            .limit(30)
            .execute()
            .data
            or []
        )
        deposit_wallets = await get_crypto_wallets()
        return web.json_response(
            {
                "wallet": {
                    "demo_usdt": float(wallet.get("demo_usdt") or 0),
                    "real_usdt": float(wallet.get("real_usdt") or 0),
                    "has_binance_keys": bool(wallet.get("binance_api_key") and wallet.get("binance_api_secret")),
                },
                "bots": bots,
                "orders": orders,
                "deposit_wallets": deposit_wallets,
            }
        )
    except Exception as e:
        return web.json_response({"error": f"Виконайте migrations.sql у Supabase: {e}"}, status=400)


async def handle_trading_demo_reset(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    await get_or_create_trading_wallet(user_uuid)
    await run_db(
        lambda: supabase.table("trading_wallets")
        .update({"demo_usdt": 10000.0, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("user_id", user_uuid)
        .execute()
    )
    return web.json_response({"status": "reset", "demo_usdt": 10000.0})


async def handle_trading_transfer(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    account_id = str(body.get("account_id") or "").strip()
    direction = str(body.get("direction") or "deposit").lower()  # deposit | withdraw
    amount_usdt = float(body.get("amount_usdt") or 0)
    if not account_id or amount_usdt <= 0 or direction not in ("deposit", "withdraw"):
        return web.json_response({"error": "Некоректні параметри переказу"}, status=400)

    acc_res = await run_db(
        lambda: supabase.table("accounts")
        .select("*")
        .eq("id", account_id)
        .eq("user_id", user_uuid)
        .limit(1)
        .execute()
        .data
    )
    if not acc_res:
        return web.json_response({"error": "Рахунок не знайдено"}, status=404)
    acc = acc_res[0]

    rates = await get_exchange_rates()
    usd_rate = rates.get("USD", 41.5)
    acc_curr = str(acc.get("currency") or "UAH").upper()
    acc_rate = 1.0 if acc_curr == "UAH" else rates.get(acc_curr, usd_rate if acc_curr == "USDT" else 1.0)
    amount_in_acc = round(amount_usdt * (usd_rate / acc_rate), 2)

    wallet = await get_or_create_trading_wallet(user_uuid)
    real_usdt = float(wallet.get("real_usdt") or 0)

    if direction == "deposit":
        if float(acc["balance"]) < amount_in_acc:
            return web.json_response(
                {"error": f"Недостатньо коштів на рахунку ({amount_in_acc} {acc_curr})"},
                status=400,
            )
        await create_transaction_atomic(
            user_uuid=user_uuid,
            account_id=account_id,
            to_account_id=None,
            category_id=None,
            amount=amount_in_acc,
            target_amount=None,
            tx_type="expense",
            note=f"🤖 Поповнення торгового рахунку ({amount_usdt} USDT)",
        )
        new_real = round(real_usdt + amount_usdt, 4)
        await run_db(
            lambda: supabase.table("trading_wallets")
            .update({"real_usdt": new_real, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("user_id", user_uuid)
            .execute()
        )
        return web.json_response({"status": "deposited", "real_usdt": new_real})

    else:
        if real_usdt < amount_usdt:
            return web.json_response({"error": "Недостатньо USDT на торговому рахунку"}, status=400)
        new_real = round(real_usdt - amount_usdt, 4)
        await run_db(
            lambda: supabase.table("trading_wallets")
            .update({"real_usdt": new_real, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("user_id", user_uuid)
            .execute()
        )
        await create_transaction_atomic(
            user_uuid=user_uuid,
            account_id=account_id,
            to_account_id=None,
            category_id=None,
            amount=amount_in_acc,
            target_amount=None,
            tx_type="income",
            note=f"🤖 Виведення з торгового рахунку ({amount_usdt} USDT)",
        )
        return web.json_response({"status": "withdrawn", "real_usdt": new_real})


async def handle_trading_crypto_deposit(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    network_key = str(body.get("network_key") or "USDT_TRC20").strip()
    tx_hash = str(body.get("tx_hash") or "").strip()
    amount_usdt = float(body.get("amount_usdt") or 0)
    if not tx_hash or amount_usdt <= 0:
        return web.json_response({"error": "Вкажіть суму USDT та TX Hash"}, status=400)

    wallets = await get_crypto_wallets()
    expected_wallet = wallets.get(network_key, "")
    ok, msg = await verify_crypto_payment(network_key, tx_hash, expected_wallet, amount_usdt)
    if not ok:
        return web.json_response({"error": f"Не вдалося підтвердити переказ: {msg}"}, status=400)

    wallet = await get_or_create_trading_wallet(user_uuid)
    new_real = round(float(wallet.get("real_usdt") or 0) + amount_usdt, 4)
    await run_db(
        lambda: supabase.table("trading_wallets")
        .update({"real_usdt": new_real, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("user_id", user_uuid)
        .execute()
    )
    return web.json_response({"status": "credited", "real_usdt": new_real, "message": msg})


async def handle_trading_save_binance_keys(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    api_key = str(body.get("api_key") or "").strip()
    api_secret = str(body.get("api_secret") or "").strip()
    await get_or_create_trading_wallet(user_uuid)
    await run_db(
        lambda: supabase.table("trading_wallets")
        .update(
            {
                "binance_api_key": api_key,
                "binance_api_secret": api_secret,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("user_id", user_uuid)
        .execute()
    )
    return web.json_response({"status": "saved", "has_binance_keys": bool(api_key and api_secret)})


async def handle_trading_create_bot(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    mode = str(body.get("mode") or "demo").lower()
    symbol = str(body.get("symbol") or "BTCUSDT").upper()
    strategy = str(body.get("strategy") or "tp_sl").lower()
    order_usdt = float(body.get("order_usdt") or 50)
    tp = float(body.get("take_profit_pct") or 3.0)
    sl = float(body.get("stop_loss_pct") or 2.0)

    if mode not in ("demo", "real") or strategy not in ("tp_sl", "dca", "momentum") or order_usdt <= 0:
        return web.json_response({"error": "Некоректні параметри бота"}, status=400)

    await get_or_create_trading_wallet(user_uuid)
    res = await run_db(
        lambda: supabase.table("trading_bots")
        .insert(
            {
                "user_id": user_uuid,
                "mode": mode,
                "symbol": symbol,
                "strategy": strategy,
                "order_usdt": order_usdt,
                "take_profit_pct": tp,
                "stop_loss_pct": sl,
                "is_active": True,
            }
        )
        .execute()
    )
    return web.json_response({"status": "created", "bot": res.data[0]})


async def handle_trading_toggle_bot(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    bot_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    rows = await run_db(
        lambda: supabase.table("trading_bots").select("is_active").eq("id", bot_id).eq("user_id", user_uuid).limit(1).execute().data
    )
    if not rows:
        return web.json_response({"error": "Бота не знайдено"}, status=404)
    new_state = not bool(rows[0]["is_active"])
    await run_db(
        lambda: supabase.table("trading_bots").update({"is_active": new_state}).eq("id", bot_id).eq("user_id", user_uuid).execute()
    )
    return web.json_response({"status": "toggled", "is_active": new_state})


async def handle_trading_step_bot(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    bot_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    rows = await run_db(
        lambda: supabase.table("trading_bots").select("*").eq("id", bot_id).eq("user_id", user_uuid).limit(1).execute().data
    )
    if not rows:
        return web.json_response({"error": "Бота не знайдено"}, status=404)

    result = await step_trading_bot(rows[0], tg_bot=request.app.get("bot"), user_tg_id=request["tg_id"])
    return web.json_response(result)


async def handle_trading_delete_bot(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied

    bot_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    await run_db(
        lambda: supabase.table("trading_bots").delete().eq("id", bot_id).eq("user_id", user_uuid).execute()
    )
    return web.json_response({"status": "deleted"})
