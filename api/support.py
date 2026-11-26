import logging
from aiohttp import web

from config import ADMIN_ID
from db import supabase, run_db, is_admin
from services.faq_data import FAQ_ITEMS


async def handle_get_faq(request: web.Request):
    return web.json_response({"faq": FAQ_ITEMS})


async def handle_get_support_messages(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        rows = await run_db(
            lambda: supabase.table("support_messages")
            .select("*")
            .eq("user_id", user_uuid)
            .order("created_at", desc=False)
            .limit(100)
            .execute()
            .data
            or []
        )
        return web.json_response({"messages": rows})
    except Exception as e:
        return web.json_response({"messages": [], "error": str(e)})


async def handle_send_support_message(request: web.Request):
    user_uuid: str = request["user_uuid"]
    tg_id: int = request["tg_id"]
    user_row = request.get("user_row") or {}
    bot = request.app.get("bot")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "Введіть текст повідомлення"}, status=400)
    if len(text) > 2000:
        text = text[:2000]

    try:
        res = await run_db(
            lambda: supabase.table("support_messages")
            .insert(
                {
                    "user_id": user_uuid,
                    "telegram_id": tg_id,
                    "sender_role": "user",
                    "message_text": text,
                    "is_read": False,
                }
            )
            .execute()
        )
        msg_row = res.data[0]
    except Exception as e:
        return web.json_response({"error": f"Не вдалося надіслати повідомлення (перевірте migrations.sql): {e}"}, status=400)

    if bot:
        uname = f"@{user_row.get('username')}" if user_row.get("username") else "без username"
        fname = user_row.get("first_name") or "Користувач"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"📩 <b>Нове звернення в підтримку!</b>\n\n"
                f"👤 {fname} ({uname}) • ID: <code>{tg_id}</code>\n"
                f"💬 <i>{text}</i>\n\n"
                f"Відповісти в чаті: <code>/reply {tg_id} Ваша відповідь</code>\n"
                f"Або через Адмін-панель у Mini App.",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.warning(f"Failed to notify admin about support msg: {e}")

    return web.json_response({"status": "sent", "message": msg_row})


async def handle_admin_support_threads(request: web.Request):
    if not await is_admin(request["tg_id"]):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        def _load():
            msgs = (
                supabase.table("support_messages")
                .select("*")
                .order("created_at", desc=False)
                .limit(500)
                .execute()
                .data
                or []
            )
            users = (
                supabase.table("users")
                .select("id, telegram_id, username, first_name, subscription_plan")
                .execute()
                .data
                or []
            )
            u_map = {u["id"]: u for u in users}
            threads: dict[str, dict] = {}
            for m in msgs:
                uid = m["user_id"]
                if uid not in threads:
                    u = u_map.get(uid, {})
                    threads[uid] = {
                        "user_id": uid,
                        "telegram_id": m.get("telegram_id") or u.get("telegram_id"),
                        "first_name": u.get("first_name") or "Користувач",
                        "username": u.get("username") or "",
                        "plan": u.get("subscription_plan") or "free",
                        "unread_count": 0,
                        "last_message": "",
                        "last_at": "",
                        "messages": [],
                    }
                th = threads[uid]
                th["messages"].append(m)
                th["last_message"] = m["message_text"]
                th["last_at"] = m["created_at"]
                if m.get("sender_role") == "user" and not m.get("is_read"):
                    th["unread_count"] += 1
            out = sorted(threads.values(), key=lambda x: x["last_at"], reverse=True)
            return out

        threads = await run_db(_load)
        return web.json_response({"threads": threads})
    except Exception as e:
        return web.json_response({"threads": [], "error": str(e)})


async def handle_admin_support_reply(request: web.Request):
    if not await is_admin(request["tg_id"]):
        return web.json_response({"error": "Forbidden"}, status=403)

    target_user_id = request.match_info.get("user_id")
    bot = request.app.get("bot")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "Введіть текст відповіді"}, status=400)

    u_rows = await run_db(
        lambda: supabase.table("users").select("id, telegram_id").eq("id", target_user_id).limit(1).execute().data or []
    )
    if not u_rows:
        return web.json_response({"error": "Користувача не знайдено"}, status=404)

    target_tg_id = int(u_rows[0]["telegram_id"])
    res = await run_db(
        lambda: supabase.table("support_messages")
        .insert(
            {
                "user_id": target_user_id,
                "telegram_id": target_tg_id,
                "sender_role": "admin",
                "message_text": text[:2000],
                "is_read": True,
            }
        )
        .execute()
    )
    await run_db(
        lambda: supabase.table("support_messages")
        .update({"is_read": True})
        .eq("user_id", target_user_id)
        .eq("sender_role", "user")
        .execute()
    )

    if bot and target_tg_id:
        try:
            await bot.send_message(
                target_tg_id,
                f"💬 <b>Відповідь підтримки FinTrack Hub:</b>\n\n{text}",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.warning(f"Support reply DM failed: {e}")

    return web.json_response({"status": "replied", "message": res.data[0]})
