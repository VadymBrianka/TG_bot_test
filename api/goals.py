import logging
from aiohttp import web
from db import supabase, run_db, get_exchange_rates
from schemas import GoalCreate, GoalUpdate, GoalFundsModify, parse_json_body
from services.subscriptions import PLANS, get_user_subscription


async def handle_create_goal(request: web.Request):
    body, err_resp = await parse_json_body(request, GoalCreate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    sub = await get_user_subscription(request["tg_id"])
    max_goals = PLANS[sub["plan"]]["max_goals"]
    cur_cnt = await run_db(
        lambda: supabase.table("goals").select("id", count="exact").eq("user_id", user_uuid).execute().count or 0
    )
    if cur_cnt >= max_goals:
        return web.json_response(
            {
                "error": f"На тарифі Free можна мати до {max_goals} цілей. Оновіть підписку до Pro у вкладці «💎 Підписка».",
                "required_plan": "pro",
            },
            status=402,
        )
    try:
        res = await run_db(
            lambda: supabase.table("goals")
            .insert(
                {
                    "user_id": user_uuid,
                    "title": body.title,
                    "target_amount": body.target_amount,
                    "current_amount": body.current_amount,
                    "currency": body.currency,
                    "deadline": body.deadline or None,
                }
            )
            .execute()
        )
        return web.json_response({"status": "success", "goal": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_update_goal(request: web.Request):
    goal_id = request.match_info.get("id")
    body, err_resp = await parse_json_body(request, GoalUpdate)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("goals")
            .update(
                {
                    "title": body.title,
                    "target_amount": body.target_amount,
                    "currency": body.currency,
                    "deadline": body.deadline or None,
                }
            )
            .eq("id", goal_id)
            .eq("user_id", user_uuid)
            .execute()
        )
        if not res.data:
            return web.json_response({"error": "Ціль не знайдено"}, status=404)
        return web.json_response({"status": "updated", "goal": res.data[0]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_delete_goal(request: web.Request):
    goal_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        res = await run_db(
            lambda: supabase.table("goals").delete().eq("id", goal_id).eq("user_id", user_uuid).execute()
        )
        if not res.data:
            return web.json_response({"error": "Ціль не знайдено"}, status=404)
        return web.json_response({"status": "deleted"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_modify_goal_funds(request: web.Request):
    body, err_resp = await parse_json_body(request, GoalFundsModify)
    if err_resp:
        return err_resp

    user_uuid: str = request["user_uuid"]
    try:
        def _load():
            g_res = supabase.table("goals").select("*").eq("id", body.goal_id).eq("user_id", user_uuid).limit(1).execute().data
            a_res = supabase.table("accounts").select("*").eq("id", body.account_id).eq("user_id", user_uuid).limit(1).execute().data
            return (g_res[0] if g_res else None), (a_res[0] if a_res else None)

        goal, acc = await run_db(_load)
        if not goal:
            return web.json_response({"error": "Ціль не знайдено"}, status=404)
        if not acc:
            return web.json_response({"error": "Рахунок не знайдено"}, status=404)

        rates = await get_exchange_rates()
        goal_curr = str(goal.get("currency", "UAH")).upper()
        acc_curr = str(acc.get("currency", "UAH")).upper()
        rate_goal = rates.get(goal_curr, 1.0)
        rate_acc = rates.get(acc_curr, 1.0)

        amount_in_acc = body.amount * (rate_goal / rate_acc)
        goal_title = goal.get("title", "Ціль")
        note = (
            f"🎯 Поповнення цілі: {goal_title} ({body.amount:,.2f} {goal_curr})"
            if body.action == "deposit"
            else f"🎯 Зняття з цілі: {goal_title} ({body.amount:,.2f} {goal_curr})"
        )

        # Try atomic RPC first
        rpc_ok = False
        try:
            await run_db(
                lambda: supabase.rpc(
                    "modify_goal_funds_atomic",
                    {
                        "p_user_id": user_uuid,
                        "p_goal_id": body.goal_id,
                        "p_account_id": body.account_id,
                        "p_amount_goal": body.amount,
                        "p_amount_acc": amount_in_acc,
                        "p_action": body.action,
                        "p_note": note,
                    },
                ).execute()
            )
            rpc_ok = True
        except Exception as rpc_err:
            err_msg = str(rpc_err)
            if "Insufficient" in err_msg:
                return web.json_response({"error": "Недостатньо коштів"}, status=400)
            logging.debug(f"RPC modify_goal_funds_atomic unavailable, using fallback: {rpc_err}")

        if not rpc_ok:
            goal_balance = float(goal["current_amount"])
            acc_balance = float(acc["balance"])
            if body.action == "deposit":
                if acc_balance < amount_in_acc:
                    return web.json_response(
                        {"error": f"Недостатньо коштів на рахунку ({amount_in_acc:.2f} {acc_curr})"},
                        status=400,
                    )
                def _dep():
                    supabase.table("accounts").update({"balance": acc_balance - amount_in_acc}).eq("id", body.account_id).eq("user_id", user_uuid).execute()
                    supabase.table("goals").update({"current_amount": goal_balance + body.amount}).eq("id", body.goal_id).eq("user_id", user_uuid).execute()
                    supabase.table("transactions").insert(
                        {
                            "user_id": user_uuid,
                            "account_id": body.account_id,
                            "amount": round(amount_in_acc, 2),
                            "target_amount": round(body.amount, 2),
                            "type": "transfer",
                            "note": note,
                        }
                    ).execute()
                await run_db(_dep)
            else:
                if goal_balance < body.amount:
                    return web.json_response({"error": "У цілі недостатньо коштів"}, status=400)
                def _wd():
                    supabase.table("goals").update({"current_amount": goal_balance - body.amount}).eq("id", body.goal_id).eq("user_id", user_uuid).execute()
                    supabase.table("accounts").update({"balance": acc_balance + amount_in_acc}).eq("id", body.account_id).eq("user_id", user_uuid).execute()
                    supabase.table("transactions").insert(
                        {
                            "user_id": user_uuid,
                            "account_id": body.account_id,
                            "amount": round(amount_in_acc, 2),
                            "target_amount": round(body.amount, 2),
                            "type": "transfer",
                            "note": note,
                        }
                    ).execute()
                await run_db(_wd)

        return web.json_response(
            {
                "status": "success",
                "converted_amount": round(amount_in_acc, 2),
                "account_currency": acc_curr,
            }
        )
    except Exception as e:
        logging.error(f"Goal modify error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_close_goal(request: web.Request):
    goal_id = request.match_info.get("id")
    user_uuid: str = request["user_uuid"]
    try:
        def _close():
            goal_res = supabase.table("goals").select("*").eq("id", goal_id).eq("user_id", user_uuid).limit(1).execute().data
            if not goal_res:
                return None
            goal = goal_res[0]
            current_amount = float(goal.get("current_amount", 0))
            title = goal.get("title", "Ціль")
            goal_curr = goal.get("currency", "UAH")

            accs = supabase.table("accounts").select("id").eq("user_id", user_uuid).order("created_at").limit(1).execute().data
            acc_id = accs[0]["id"] if accs else None

            # Record as 'transfer' (not 'expense') so it doesn't double-count in expense analytics
            if current_amount > 0 and acc_id:
                supabase.table("transactions").insert(
                    {
                        "user_id": user_uuid,
                        "account_id": acc_id,
                        "to_account_id": None,
                        "amount": current_amount,
                        "type": "transfer",
                        "note": f"🎯 Досягнення цілі: {title} ({current_amount:,.2f} {goal_curr})",
                    }
                ).execute()

            supabase.table("goals").delete().eq("id", goal_id).eq("user_id", user_uuid).execute()
            return title

        title = await run_db(_close)
        if title is None:
            return web.json_response({"error": "Ціль не знайдено"}, status=404)
        return web.json_response({"status": "success", "message": f"Ціль «{title}» закрито!"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
