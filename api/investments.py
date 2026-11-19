from datetime import datetime, timezone
from aiohttp import web
from db import supabase, run_db, get_exchange_rates, create_transaction_atomic
from schemas import InvestmentCreate, InvestmentUpdate, parse_json_body
from services.market_quotes import fetch_prices_parallel
from services.subscriptions import require_plan


async def handle_create_investment(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied
    body, err_resp = await parse_json_body(request, InvestmentCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    raw_ticker = (body.ticker or "").strip().upper()
    raw_name = (body.name or "").strip()

    if not raw_ticker:
        if body.asset_class == "deposit":
            raw_ticker = (raw_name[:12] or "DEPOSIT").upper()
        elif body.asset_class == "bond":
            raw_ticker = (raw_name[:12] or "OVDP").upper()
        else:
            return web.json_response({"error": "Вкажіть тикер активу"}, status=400)

    cur_price = body.current_price if body.current_price is not None else body.buy_price_avg
    payload = {
        "user_id": user_uuid,
        "name": raw_name or raw_ticker,
        "ticker": raw_ticker,
        "asset_class": body.asset_class,
        "quantity": body.quantity,
        "buy_price_avg": body.buy_price_avg,
        "current_price": cur_price,
        "currency": body.currency,
        "interest_rate": body.interest_rate,
        "maturity_date": body.maturity_date or None,
        "term_months": body.term_months,
    }

    try:
        if body.account_id:
            acc_res = await run_db(
                lambda: supabase.table("accounts")
                .select("id, balance, currency")
                .eq("id", body.account_id)
                .eq("user_id", user_uuid)
                .limit(1)
                .execute()
                .data
            )
            if not acc_res:
                return web.json_response({"error": "Рахунок списання не знайдено"}, status=404)
            acc = acc_res[0]
            rates = await get_exchange_rates()
            inv_total = body.quantity * body.buy_price_avg
            rate_inv = rates.get(body.currency, 1.0)
            rate_acc = rates.get(str(acc.get("currency", "UAH")).upper(), 1.0)
            debit_amt = round(inv_total * (rate_inv / rate_acc), 2)
            if float(acc["balance"]) < debit_amt:
                return web.json_response(
                    {"error": f"Недостатньо коштів на рахунку ({debit_amt:.2f} {acc['currency']})"},
                    status=400,
                )
            await create_transaction_atomic(
                user_uuid=user_uuid,
                account_id=body.account_id,
                to_account_id=None,
                category_id=None,
                amount=debit_amt,
                target_amount=None,
                tx_type="expense",
                note=f"📈 Купівля активу: {payload['name']} ({body.quantity} × {body.buy_price_avg} {body.currency})",
            )

        res = await run_db(lambda: supabase.table("investments").insert(payload).execute())
        return web.json_response({"status": "success", "investment": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_update_investment(request: web.Request):
    inv_id = request.match_info.get("id")
    body, err_resp = await parse_json_body(request, InvestmentUpdate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    raw_ticker = (body.ticker or "").strip().upper() or (body.name or "ASSET")[:12].upper()
    payload = {
        "name": (body.name or raw_ticker).strip(),
        "ticker": raw_ticker,
        "asset_class": body.asset_class,
        "quantity": body.quantity,
        "buy_price_avg": body.buy_price_avg,
        "current_price": body.current_price,
        "currency": body.currency,
        "interest_rate": body.interest_rate,
        "maturity_date": body.maturity_date or None,
        "term_months": body.term_months,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = await run_db(
            lambda: supabase.table("investments")
            .update(payload)
            .eq("id", inv_id)
            .eq("user_id", user_uuid)
            .execute()
        )
        if not res.data:
            return web.json_response({"error": "Актив не знайдено"}, status=404)
        return web.json_response({"status": "updated", "investment": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_delete_investment(request: web.Request):
    inv_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    credit_account_id = request.query.get("credit_account_id", "").strip()
    try:
        inv_res = await run_db(
            lambda: supabase.table("investments")
            .select("*")
            .eq("id", inv_id)
            .eq("user_id", user_uuid)
            .limit(1)
            .execute()
            .data
        )
        if not inv_res:
            return web.json_response({"error": "Актив не знайдено"}, status=404)
        inv = inv_res[0]

        if credit_account_id:
            acc_res = await run_db(
                lambda: supabase.table("accounts")
                .select("id, currency")
                .eq("id", credit_account_id)
                .eq("user_id", user_uuid)
                .limit(1)
                .execute()
                .data
            )
            if not acc_res:
                return web.json_response({"error": "Рахунок зарахування не знайдено"}, status=404)
            acc = acc_res[0]
            rates = await get_exchange_rates()
            sale_val = float(inv.get("quantity") or 1) * float(inv.get("current_price") or inv.get("buy_price_avg") or 0)
            rate_inv = rates.get(str(inv.get("currency") or "USD").upper(), 1.0)
            rate_acc = rates.get(str(acc.get("currency") or "UAH").upper(), 1.0)
            credit_amt = round(sale_val * (rate_inv / rate_acc), 2)
            if credit_amt > 0:
                await create_transaction_atomic(
                    user_uuid=user_uuid,
                    account_id=credit_account_id,
                    to_account_id=None,
                    category_id=None,
                    amount=credit_amt,
                    target_amount=None,
                    tx_type="income",
                    note=f"💰 Продаж/погашення активу: {inv.get('name') or inv.get('ticker')}",
                )

        await run_db(
            lambda: supabase.table("investments").delete().eq("id", inv_id).eq("user_id", user_uuid).execute()
        )
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_refresh_investment_prices(request: web.Request):
    denied = await require_plan(request, "vip")
    if denied:
        return denied
    user_uuid: str = request["user_uuid"]
    try:
        invs = await run_db(
            lambda: supabase.table("investments")
            .select("id, ticker, asset_class")
            .eq("user_id", user_uuid)
            .in_("asset_class", ["stock", "crypto"])
            .execute()
            .data
            or []
        )
        prices = await fetch_prices_parallel(invs)
        now_iso = datetime.now(timezone.utc).isoformat()
        updated_count = 0
        for inv_id, p in prices.items():
            await run_db(
                lambda i_id=inv_id, price=p: supabase.table("investments")
                .update({"current_price": price, "updated_at": now_iso})
                .eq("id", i_id)
                .eq("user_id", user_uuid)
                .execute()
            )
            updated_count += 1

        return web.json_response({"status": "success", "updated": updated_count})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
