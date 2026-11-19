import asyncio
import logging
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import MINIAPP_URL, GEMINI_API_KEY
from db import supabase, run_db, is_admin
from services.nbu import update_rates_from_nbu
from services.broadcast_ai import generate_broadcast_draft


async def _require_admin(request: web.Request) -> web.Response | None:
    if not await is_admin(request["tg_id"]):
        return web.json_response({"error": "Forbidden: admin role required"}, status=403)
    return None


async def handle_admin_overview(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    def _load():
        users = supabase.table("users").select("*", count="exact").order("telegram_id").limit(200).execute()
        tx_cnt = supabase.table("transactions").select("id", count="exact").limit(1).execute().count or 0
        acc_cnt = supabase.table("accounts").select("id", count="exact").limit(1).execute().count or 0
        goal_cnt = supabase.table("goals").select("id", count="exact").limit(1).execute().count or 0
        inv_cnt = supabase.table("investments").select("id", count="exact").limit(1).execute().count or 0
        rates_cnt = supabase.table("exchange_rates").select("currency", count="exact").limit(1).execute().count or 0

        now = datetime.now(timezone.utc)
        d1 = (now - timedelta(days=1)).isoformat()
        d7 = (now - timedelta(days=7)).isoformat()
        d30 = (now - timedelta(days=30)).isoformat()
        recent_txs = (
            supabase.table("transactions")
            .select("user_id, transaction_date")
            .gte("transaction_date", d30)
            .execute()
            .data
            or []
        )
        dau = len({t["user_id"] for t in recent_txs if (t.get("transaction_date") or "") >= d1})
        wau = len({t["user_id"] for t in recent_txs if (t.get("transaction_date") or "") >= d7})
        mau = len({t["user_id"] for t in recent_txs})
        tx_7d = sum(1 for t in recent_txs if (t.get("transaction_date") or "") >= d7)

        try:
            b_logs = (
                supabase.table("broadcast_logs")
                .select("*")
                .order("created_at", desc=True)
                .limit(10)
                .execute()
                .data
                or []
            )
        except Exception:
            b_logs = []

        return {
            "stats": {
                "users_count": users.count if users.count is not None else len(users.data or []),
                "transactions_count": tx_cnt,
                "accounts_count": acc_cnt,
                "goals_count": goal_cnt,
                "investments_count": inv_cnt,
                "rates_count": rates_cnt,
                "dau": dau,
                "wau": wau,
                "mau": mau,
                "tx_7d": tx_7d,
                "gemini_configured": bool(GEMINI_API_KEY),
            },
            "users": [
                {
                    "id": u["id"],
                    "telegram_id": u.get("telegram_id"),
                    "username": u.get("username"),
                    "first_name": u.get("first_name"),
                    "role": u.get("role", "user"),
                    "is_banned": bool(u.get("is_banned", False)),
                    "base_currency": u.get("base_currency", "UAH"),
                }
                for u in (users.data or [])
            ],
            "broadcast_logs": b_logs,
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


async def handle_admin_ban_user(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    target_user_id = request.match_info.get("id")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    is_banned = bool(body.get("is_banned", False))
    try:
        res = await run_db(
            lambda: supabase.table("users").update({"is_banned": is_banned}).eq("id", target_user_id).execute()
        )
        if not res.data:
            return web.json_response({"error": "Користувача не знайдено"}, status=404)
        return web.json_response({"status": "updated", "is_banned": is_banned})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося змінити статус блокування (перевірте міграцію БД): {e}"}, status=400)


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

    target = str(body.get("target", "all")).lower()  # all | admins | custom | self
    custom_ids_raw = str(body.get("custom_ids", ""))
    button_text = str(body.get("button_text", "")).strip()
    button_url = str(body.get("button_url", "")).strip()
    attach_miniapp_btn = bool(body.get("attach_miniapp_btn", False))
    preview_self = bool(body.get("preview_self", False))

    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot unavailable"}, status=500)

    reply_markup = None
    if button_text and button_url:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
        )
    elif attach_miniapp_btn and MINIAPP_URL:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📊 Відкрити FinTrack Hub", web_app=WebAppInfo(url=MINIAPP_URL))]]
        )

    if preview_self or target == "self":
        tg_ids = [request["tg_id"]]
    elif target == "custom":
        tg_ids = [int(x.strip()) for x in custom_ids_raw.split(",") if x.strip().isdigit()]
    else:
        def _load_users():
            rows = supabase.table("users").select("telegram_id, role, is_banned").execute().data or []
            if target == "admins":
                return [u["telegram_id"] for u in rows if u.get("telegram_id") and u.get("role") == "admin"]
            return [u["telegram_id"] for u in rows if u.get("telegram_id") and not u.get("is_banned")]
        tg_ids = await run_db(_load_users)

    sent = 0
    blocked = 0
    for tg_id in tg_ids:
        try:
            await bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
                sent += 1
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"Admin broadcast err for {tg_id}: {e}")

    if not preview_self and target != "self":
        try:
            await run_db(
                lambda: supabase.table("broadcast_logs")
                .insert(
                    {
                        "admin_tg_id": request["tg_id"],
                        "target_filter": target,
                        "message_text": text[:500],
                        "sent_count": sent,
                        "blocked_count": blocked,
                    }
                )
                .execute()
            )
        except Exception:
            pass

    return web.json_response({"status": "done", "sent": sent, "blocked": blocked, "preview": preview_self})


async def handle_admin_refresh_rates(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    await update_rates_from_nbu()
    return web.json_response({"status": "refreshed"})


async def handle_admin_send_dm(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    target_user_id = request.match_info.get("id")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "Повідомлення порожнє"}, status=400)

    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot unavailable"}, status=500)

    rows = await run_db(
        lambda: supabase.table("users").select("telegram_id").eq("id", target_user_id).limit(1).execute().data
    )
    if not rows or not rows[0].get("telegram_id"):
        return web.json_response({"error": "Користувача не знайдено"}, status=404)

    try:
        await bot.send_message(chat_id=rows[0]["telegram_id"], text=text, parse_mode="HTML")
        return web.json_response({"status": "sent"})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося надіслати: {e}"}, status=502)


async def handle_admin_generate_broadcast(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    topic = str(body.get("topic", "")).strip()
    tone = str(body.get("tone", "дружній")).strip()
    if not topic:
        return web.json_response({"error": "Вкажіть тему або ідею для статті"}, status=400)

    try:
        draft = await generate_broadcast_draft(topic=topic, tone=tone)
        return web.json_response(draft)
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e) or "Не вдалося згенерувати текст"}, status=502)
