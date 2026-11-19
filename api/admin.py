import asyncio
import logging
from aiohttp import web
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from db import supabase, run_db, is_admin
from services.nbu import update_rates_from_nbu


async def _require_admin(request: web.Request) -> web.Response | None:
    if not await is_admin(request["tg_id"]):
        return web.json_response({"error": "Forbidden: admin role required"}, status=403)
    return None


async def handle_admin_overview(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    def _load():
        users = supabase.table("users").select("*", count="exact").order("telegram_id").limit(100).execute()
        tx_cnt = supabase.table("transactions").select("id", count="exact").limit(1).execute().count or 0
        acc_cnt = supabase.table("accounts").select("id", count="exact").limit(1).execute().count or 0
        goal_cnt = supabase.table("goals").select("id", count="exact").limit(1).execute().count or 0
        inv_cnt = supabase.table("investments").select("id", count="exact").limit(1).execute().count or 0
        return {
            "stats": {
                "users_count": users.count if users.count is not None else len(users.data or []),
                "transactions_count": tx_cnt,
                "accounts_count": acc_cnt,
                "goals_count": goal_cnt,
                "investments_count": inv_cnt,
            },
            "users": [
                {
                    "id": u["id"],
                    "telegram_id": u.get("telegram_id"),
                    "username": u.get("username"),
                    "first_name": u.get("first_name"),
                    "role": u.get("role", "user"),
                }
                for u in (users.data or [])
            ],
        }

    data = await run_db(_load)
    return web.json_response(data)


async def handle_admin_set_role(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    target_user_id = request.match_info.get("id")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    new_role = str(body.get("role", "user")).lower()
    if new_role not in ("admin", "user"):
        return web.json_response({"error": "Роль має бути 'admin' або 'user'"}, status=400)

    try:
        res = await run_db(
            lambda: supabase.table("users").update({"role": new_role}).eq("id", target_user_id).execute()
        )
        if not res.data:
            return web.json_response({"error": "Користувача не знайдено"}, status=404)
        return web.json_response({"status": "updated", "role": new_role})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося оновити роль (перевірте міграцію БД): {e}"}, status=400)


async def handle_admin_broadcast(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "Текст розсилки порожній"}, status=400)

    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot unavailable"}, status=500)

    users = await run_db(lambda: supabase.table("users").select("telegram_id").execute().data or [])
    sent = 0
    blocked = 0
    for u in users:
        tg_id = u.get("telegram_id")
        if not tg_id:
            continue
        try:
            await bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML")
                sent += 1
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"Admin broadcast err for {tg_id}: {e}")

    return web.json_response({"status": "done", "sent": sent, "blocked": blocked})


async def handle_admin_refresh_rates(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    await update_rates_from_nbu()
    return web.json_response({"status": "refreshed"})
