from aiohttp import web
from db import supabase, run_db, get_exchange_rates, create_transaction_atomic
from schemas import DebtCreate, parse_json_body


async def handle_list_debts(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        items = await run_db(
            lambda: supabase.table("debts")
            .select("*")
            .eq("user_id", user_uuid)
            .order("is_settled")
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        return web.json_response({"debts": items})
    except Exception:
        return web.json_response({"debts": [], "migration_required": True})


async def handle_create_debt(request: web.Request):
    body, err_resp = await parse_json_body(request, DebtCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("debts")
            .insert(
                {
                    "user_id": user_uuid,
                    "direction": body.direction,
                    "person_name": body.person_name.strip(),
                    "amount": body.amount,
                    "currency": body.currency,
                    "note": body.note.strip(),
                    "due_date": body.due_date or None,
                    "is_settled": False,
                }
            )
            .execute()
        )
        return web.json_response({"status": "success", "debt": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося зберегти борг (перевірте міграцію БД): {e}"}, status=400)


async def handle_settle_debt(request: web.Request):
    debt_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    account_id = ""
    try:
        body = await request.json()
        account_id = str(body.get("account_id") or "").strip()
    except Exception:
        pass

    try:
        rows = await run_db(
            lambda: supabase.table("debts").select("*").eq("id", debt_id).eq("user_id", user_uuid).limit(1).execute().data
        )
        if not rows:
            return web.json_response({"error": "Запис не знайдено"}, status=404)
        debt = rows[0]

        if not debt.get("is_settled") and account_id:
            acc_res = await run_db(
                lambda: supabase.table("accounts")
                .select("id, currency")
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
            d_curr = str(debt.get("currency") or "UAH").upper()
            a_curr = str(acc.get("currency") or "UAH").upper()
            amt = round(float(debt["amount"]) * (rates.get(d_curr, 1.0) / rates.get(a_curr, 1.0)), 2)
            tx_type = "income" if debt["direction"] == "owed_to_me" else "expense"
            prefix = "🤝 Повернення боргу від" if tx_type == "income" else "🤝 Погашення боргу"
            await create_transaction_atomic(
                user_uuid=user_uuid,
                account_id=account_id,
                to_account_id=None,
                category_id=None,
                amount=amt,
                target_amount=None,
                tx_type=tx_type,
                note=f"{prefix} {debt['person_name']}",
            )

        await run_db(
            lambda: supabase.table("debts").update({"is_settled": True}).eq("id", debt_id).eq("user_id", user_uuid).execute()
        )
        return web.json_response({"status": "settled"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_delete_debt(request: web.Request):
    debt_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("debts").delete().eq("id", debt_id).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Запис не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
