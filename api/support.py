import base64
import io
import logging
import mimetypes
from aiohttp import web
from aiogram.types import BufferedInputFile

from config import ADMIN_ID
from db import supabase, run_db, is_admin
from services.faq_data import FAQ_ITEMS


async def handle_get_faq(request: web.Request):
    return web.json_response({"faq": FAQ_ITEMS})


def _decode_attachment(body: dict) -> tuple[bytes | None, str, str, str]:
    """Return (raw_bytes, att_type, mime_type, file_name)."""
    b64 = str(body.get("attachment_base64") or "").strip()
    if not b64:
        return None, "", "", ""
    mime_type = str(body.get("mime_type") or "application/octet-stream").strip()
    if b64.startswith("data:") and "," in b64:
        header, b64 = b64.split(",", 1)
        if ";" in header:
            mime_type = header[5:].split(";", 1)[0] or mime_type
    raw = base64.b64decode(b64)
    file_name = str(body.get("file_name") or "file").strip()[:120] or "file"
    if mime_type.startswith("image/"):
        att_type = "photo"
    elif mime_type.startswith("video/"):
        att_type = "video"
    else:
        att_type = "document"
    return raw, att_type, mime_type, file_name


async def _send_tg_with_attachment(
    bot,
    chat_id: int,
    caption: str,
    raw: bytes | None,
    att_type: str,
    file_name: str,
) -> str | None:
    """Send message/media via Telegram Bot and return Telegram file_id if media was sent."""
    if not bot:
        return None
    if raw:
        inp = BufferedInputFile(raw, filename=file_name or "attachment")
        if att_type == "photo":
            sent = await bot.send_photo(chat_id, photo=inp, caption=caption[:1024], parse_mode="HTML")
            return sent.photo[-1].file_id if sent.photo else None
        if att_type == "video":
            sent = await bot.send_video(chat_id, video=inp, caption=caption[:1024], parse_mode="HTML")
            return sent.video.file_id if sent.video else None
        sent = await bot.send_document(chat_id, document=inp, caption=caption[:1024], parse_mode="HTML")
        return sent.document.file_id if sent.document else None
    await bot.send_message(chat_id, caption, parse_mode="HTML")
    return None


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

    text = str(body.get("text") or "").strip()[:2000]
    try:
        raw_bytes, att_type, _mime, file_name = _decode_attachment(body)
    except Exception:
        return web.json_response({"error": "Некоректний формат вкладення"}, status=400)

    if not text and not raw_bytes:
        return web.json_response({"error": "Введіть текст або прикріпіть файл"}, status=400)

    file_id: str | None = None
    if bot:
        uname = f"@{user_row.get('username')}" if user_row.get("username") else "без username"
        fname = user_row.get("first_name") or "Користувач"
        caption = (
            f"📩 <b>Нове звернення в підтримку!</b>\n\n"
            f"👤 {fname} ({uname}) • ID: <code>{tg_id}</code>\n"
            f"💬 <i>{text or '[Вкладення]'}</i>\n\n"
            f"Відповісти: <code>/reply {tg_id} Ваша відповідь</code>"
        )
        try:
            file_id = await _send_tg_with_attachment(bot, ADMIN_ID, caption, raw_bytes, att_type, file_name)
        except Exception as e:
            logging.warning(f"Failed to notify admin about support msg: {e}")

    try:
        payload = {
            "user_id": user_uuid,
            "telegram_id": tg_id,
            "sender_role": "user",
            "message_text": text,
            "is_read": False,
        }
        if att_type:
            payload["attachment_type"] = att_type
            payload["attachment_name"] = file_name
            payload["attachment_file_id"] = file_id
        res = await run_db(lambda: supabase.table("support_messages").insert(payload).execute())
        msg_row = res.data[0]
    except Exception as e:
        return web.json_response({"error": f"Не вдалося надіслати повідомлення (перевірте migrations.sql): {e}"}, status=400)

    return web.json_response({"status": "sent", "message": msg_row})


async def handle_get_support_media(request: web.Request):
    msg_id = request.match_info.get("msg_id")
    user_uuid: str = request["user_uuid"]
    tg_id: int = request["tg_id"]
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot unavailable"}, status=500)

    rows = await run_db(
        lambda: supabase.table("support_messages").select("*").eq("id", msg_id).limit(1).execute().data or []
    )
    if not rows:
        return web.json_response({"error": "Not found"}, status=404)
    msg = rows[0]
    if msg["user_id"] != user_uuid and not await is_admin(tg_id):
        return web.json_response({"error": "Forbidden"}, status=403)

    file_id = msg.get("attachment_file_id")
    if not file_id:
        return web.json_response({"error": "No media attached"}, status=404)

    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        data_bytes = buf.getvalue()
        fname = msg.get("attachment_name") or "file"
        guessed, _ = mimetypes.guess_type(fname)
        att_type = msg.get("attachment_type")
        ctype = guessed or (
            "image/jpeg" if att_type == "photo" else ("video/mp4" if att_type == "video" else "application/octet-stream")
        )
        return web.Response(
            body=data_bytes,
            content_type=ctype,
            headers={"Content-Disposition": f'inline; filename="{fname}"'},
        )
    except Exception as e:
        return web.json_response({"error": f"Media load error: {e}"}, status=502)


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
                th["last_message"] = m["message_text"] or (f"[{m.get('attachment_name') or 'Файл'}]" if m.get("attachment_type") else "")
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

    text = str(body.get("text") or "").strip()[:2000]
    try:
        raw_bytes, att_type, _mime, file_name = _decode_attachment(body)
    except Exception:
        return web.json_response({"error": "Некоректний формат вкладення"}, status=400)

    if not text and not raw_bytes:
        return web.json_response({"error": "Введіть текст або прикріпіть файл"}, status=400)

    u_rows = await run_db(
        lambda: supabase.table("users").select("id, telegram_id").eq("id", target_user_id).limit(1).execute().data or []
    )
    if not u_rows:
        return web.json_response({"error": "Користувача не знайдено"}, status=404)

    target_tg_id = int(u_rows[0]["telegram_id"])
    file_id: str | None = None
    if bot and target_tg_id:
        caption = f"💬 <b>Відповідь підтримки FinTrack Hub:</b>\n\n{text}" if text else "💬 <b>Відповідь підтримки FinTrack Hub</b>"
        try:
            file_id = await _send_tg_with_attachment(bot, target_tg_id, caption, raw_bytes, att_type, file_name)
        except Exception as e:
            logging.warning(f"Support reply DM failed: {e}")

    payload = {
        "user_id": target_user_id,
        "telegram_id": target_tg_id,
        "sender_role": "admin",
        "message_text": text,
        "is_read": True,
    }
    if att_type:
        payload["attachment_type"] = att_type
        payload["attachment_name"] = file_name
        payload["attachment_file_id"] = file_id

    res = await run_db(lambda: supabase.table("support_messages").insert(payload).execute())
    await run_db(
        lambda: supabase.table("support_messages")
        .update({"is_read": True})
        .eq("user_id", target_user_id)
        .eq("sender_role", "user")
        .execute()
    )

    return web.json_response({"status": "replied", "message": res.data[0]})
