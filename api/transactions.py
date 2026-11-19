import logging
from aiohttp import web
from db import supabase, run_db, create_transaction_atomic, apply_tx_balance_effect_sync
from schemas import TransactionCreate, TransactionUpdate, parse_json_body
from services.budget_alerts import check_budget_threshold


async def handle_list_transactions(request: web.Request):
    try:
        user_uuid: str = request["user_uuid"]
        q = request.query
        limit = min(max(int(q.get("limit", 50)), 1), 200)
        offset = max(int(q.get("offset", 0)), 0)
        tx_type = q.get("type", "").strip()
        category_id = q.get("category_id", "").strip()
        account_id = q.get("account_id", "").strip()
        search = q.get("search", "").strip()
        date_from = q.get("date_from", "").strip()
        date_to = q.get("date_to", "").strip()
        sort_by = q.get("sort_by", "date_desc").strip()

        def _query():
            accs = supabase.table("accounts").select("id, name, currency").eq("user_id", user_uuid).execute().data or []
            acc_map = {a["id"]: {"name": a["name"], "currency": a["currency"]} for a in accs}

            stmt = (
                supabase.table("transactions")
                .select("*, categories(name, icon)", count="exact")
                .eq("user_id", user_uuid)
            )
            if tx_type in ("expense", "income", "transfer"):
                stmt = stmt.eq("type", tx_type)
            if category_id == "none":
                stmt = stmt.is_("category_id", "null")
            elif category_id and category_id != "all":
                stmt = stmt.eq("category_id", category_id)
            if account_id:
                stmt = stmt.or_(f"account_id.eq.{account_id},to_account_id.eq.{account_id}")
            if search:
                stmt = stmt.ilike("note", f"%{search}%")
            if date_from:
                stmt = stmt.gte("transaction_date", date_from)
            if date_to:
                stmt = stmt.lte("transaction_date", date_to)

            if sort_by == "date_asc":
                stmt = stmt.order("transaction_date", desc=False)
            elif sort_by == "amount_desc":
                stmt = stmt.order("amount", desc=True)
            elif sort_by == "amount_asc":
                stmt = stmt.order("amount", desc=False)
            else:
                stmt = stmt.order("transaction_date", desc=True)

            res = stmt.range(offset, offset + limit - 1).execute()
            rows = res.data or []
            for t in rows:
                t["from_acc"] = acc_map.get(t.get("account_id"))
                t["to_acc"] = acc_map.get(t.get("to_account_id"))
            return rows, (res.count if res.count is not None else len(rows))

        rows, total = await run_db(_query)
        return web.json_response({"transactions": rows, "total": total, "limit": limit, "offset": offset})
    except Exception as e:
        logging.error(f"List transactions error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_create_transaction(request: web.Request):
    body, err_resp = await parse_json_body(request, TransactionCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    tg_id: int = request["tg_id"]
    category_id = body.category_id if body.category_id and not body.category_id.startswith("other_") else None

    if body.type == "transfer" and (not body.to_account_id or body.to_account_id == body.account_id):
        return web.json_response({"error": "Оберіть два різних рахунки для переказу"}, status=400)

    try:
        tx = await create_transaction_atomic(
            user_uuid=user_uuid,
            account_id=body.account_id,
            to_account_id=body.to_account_id,
            category_id=category_id,
            amount=body.amount,
            target_amount=body.target_amount if body.type == "transfer" else None,
            tx_type=body.type,
            note=body.note,
            transaction_date=body.transaction_date,
        )
        if body.type == "expense" and category_id:
            bot = request.app.get("bot")
            if bot:
                await check_budget_threshold(bot, user_uuid, tg_id, category_id)
        return web.json_response({"status": "success", "transaction": tx})
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        logging.error(f"Create transaction error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_update_transaction(request: web.Request):
    tx_id = request.match_info.get("id")
    body, err_resp = await parse_json_body(request, TransactionUpdate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    category_id = body.category_id if body.category_id and not body.category_id.startswith("other_") else None

    try:
        def _update():
            existing = (
                supabase.table("transactions")
                .select("*")
                .eq("id", tx_id)
                .eq("user_id", user_uuid)
                .limit(1)
                .execute()
                .data
            )
            if not existing:
                return None
            old_tx = existing[0]

            # Verify new account ownership if changed
            new_acc_id = body.account_id or old_tx.get("account_id")
            if new_acc_id:
                acc_ok = supabase.table("accounts").select("id").eq("id", new_acc_id).eq("user_id", user_uuid).execute().data
                if not acc_ok:
                    raise ValueError("Рахунок не знайдено")

            new_to_acc_id = body.to_account_id if body.type == "transfer" else None
            if body.type == "transfer":
                if not new_to_acc_id or new_to_acc_id == new_acc_id:
                    raise ValueError("Оберіть два різних рахунки для переказу")
                to_ok = supabase.table("accounts").select("id").eq("id", new_to_acc_id).eq("user_id", user_uuid).execute().data
                if not to_ok:
                    raise ValueError("Рахунок отримувача не знайдено")

            # 1. Reverse old balance effect
            apply_tx_balance_effect_sync(old_tx, user_uuid, reverse=True)

            # 2. Build updated tx
            update_payload = {
                "account_id": new_acc_id,
                "to_account_id": new_to_acc_id,
                "category_id": category_id if body.type != "transfer" else None,
                "amount": body.amount,
                "target_amount": (body.target_amount or body.amount) if body.type == "transfer" else None,
                "type": body.type,
                "note": body.note,
            }
            if body.transaction_date:
                update_payload["transaction_date"] = body.transaction_date

            res = (
                supabase.table("transactions")
                .update(update_payload)
                .eq("id", tx_id)
                .eq("user_id", user_uuid)
                .execute()
            )
            updated_tx = res.data[0] if res.data else {**old_tx, **update_payload}

            # 3. Apply new balance effect
            apply_tx_balance_effect_sync(updated_tx, user_uuid, reverse=False)
            return updated_tx

        updated = await run_db(_update)
        if not updated:
            return web.json_response({"error": "Транзакцію не знайдено"}, status=404)
        return web.json_response({"status": "updated", "transaction": updated})
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        logging.error(f"Update transaction error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_delete_transaction(request: web.Request):
    tx_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        def _delete():
            existing = (
                supabase.table("transactions")
                .select("*")
                .eq("id", tx_id)
                .eq("user_id", user_uuid)
                .limit(1)
                .execute()
                .data
            )
            if not existing:
                return False
            old_tx = existing[0]
            apply_tx_balance_effect_sync(old_tx, user_uuid, reverse=True)
            supabase.table("transactions").delete().eq("id", tx_id).eq("user_id", user_uuid).execute()
            return True

        deleted = await run_db(_delete)
        if not deleted:
            return web.json_response({"error": "Транзакцію не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        logging.error(f"Delete transaction error: {e}")
        return web.json_response({"error": str(e)}, status=500)
