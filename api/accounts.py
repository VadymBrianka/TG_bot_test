from aiohttp import web
from db import supabase, run_db
from schemas import AccountCreate, AccountUpdate, parse_json_body
from services.subscriptions import PLANS, get_user_subscription


async def handle_create_account(request: web.Request):
    body, err_resp = await parse_json_body(request, AccountCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    sub = await get_user_subscription(request["tg_id"])
    max_acc = PLANS[sub["plan"]]["max_accounts"]
    cur_cnt = await run_db(
        lambda: supabase.table("accounts").select("id", count="exact").eq("user_id", user_uuid).execute().count or 0
    )
    if cur_cnt >= max_acc:
        return web.json_response(
            {
                "error": f"На тарифі Free можна мати до {max_acc} рахунків. Оновіть підписку до Pro у вкладці «💎 Підписка».",
                "required_plan": "pro",
            },
            status=402,
        )
    try:
        res = await run_db(
            lambda: supabase.table("accounts")
            .insert(
                {
                    "user_id": user_uuid,
                    "name": body.name,
                    "account_type": body.account_type,
                    "currency": body.currency,
                    "balance": body.balance,
                }
            )
            .execute()
        )
        return web.json_response({"status": "success", "account": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_update_account(request: web.Request):
    acc_id = request.match_info.get("id")
    body, err_resp = await parse_json_body(request, AccountUpdate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("accounts")
            .update(
                {
                    "name": body.name,
                    "account_type": body.account_type,
                    "currency": body.currency,
                    "balance": body.balance,
                }
            )
            .eq("id", acc_id)
            .eq("user_id", user_uuid)
            .execute()
        )
        if not res.data:
            return web.json_response({"error": "Рахунок не знайдено"}, status=404)
        return web.json_response({"status": "updated", "account": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_delete_account(request: web.Request):
    acc_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("accounts").delete().eq("id", acc_id).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Рахунок не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
