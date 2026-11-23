import asyncio
import csv
import io
import logging
import random
from datetime import datetime, timedelta, timezone
from aiohttp import web
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BufferedInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import MINIAPP_URL, GEMINI_API_KEY
from db import supabase, run_db, is_admin
from services.nbu import update_rates_from_nbu
from services.broadcast_ai import generate_broadcast_draft
from services.subscriptions import (
    activate_subscription,
    get_crypto_wallets,
    save_crypto_wallets,
    get_effective_plans,
    save_subscription_prices,
)


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

        try:
            pending_payments = (
                supabase.table("subscription_payments")
                .select("*")
                .eq("status", "pending")
                .order("created_at", desc=True)
                .limit(20)
                .execute()
                .data
                or []
            )
        except Exception:
            pending_payments = []

        try:
            giveaways = (
                supabase.table("giveaways")
                .select("*")
                .order("created_at", desc=True)
                .limit(10)
                .execute()
                .data
                or []
            )
        except Exception:
            giveaways = []

        try:
            promo_codes = (
                supabase.table("promo_codes")
                .select("*")
                .order("created_at", desc=True)
                .limit(30)
                .execute()
                .data
                or []
            )
        except Exception:
            promo_codes = []

        # Funnel & top features
        all_users_list = users.data or []
        total_u = max(len(all_users_list), 1)
        free_cnt = sum(1 for u in all_users_list if (u.get("subscription_plan") or "free") == "free")
        pro_cnt = sum(1 for u in all_users_list if u.get("subscription_plan") == "pro")
        vip_cnt = sum(1 for u in all_users_list if u.get("subscription_plan") == "vip")
        paid_cnt = pro_cnt + vip_cnt

        top_features: list[dict] = []
        try:
            ev_rows = (
                supabase.table("analytics_events")
                .select("event_name")
                .gte("created_at", d30)
                .limit(1000)
                .execute()
                .data
                or []
            )
            counts: dict[str, int] = {}
            for er in ev_rows:
                en = er.get("event_name") or "unknown"
                counts[en] = counts.get(en, 0) + 1
            top_features = [
                {"name": k, "count": v}
                for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:8]
            ]
        except Exception:
            pass

        revenue = {"stars_total": 0, "usd_total": 0.0, "payments_count": 0, "stars_30d": 0, "usd_30d": 0.0}
        try:
            paid_rows = (
                supabase.table("subscription_payments")
                .select("payment_method, amount, status, created_at")
                .in_("status", ["approved", "paid"])
                .execute()
                .data
                or []
            )
            revenue["payments_count"] = len(paid_rows)
            for pr in paid_rows:
                amt = float(pr.get("amount") or 0)
                is_30d = str(pr.get("created_at") or "") >= d30
                if pr.get("payment_method") == "stars":
                    revenue["stars_total"] += int(amt)
                    if is_30d:
                        revenue["stars_30d"] += int(amt)
                else:
                    revenue["usd_total"] += amt
                    if is_30d:
                        revenue["usd_30d"] += amt
            revenue["usd_total"] = round(revenue["usd_total"], 2)
            revenue["usd_30d"] = round(revenue["usd_30d"], 2)
        except Exception:
            pass

        return {
            "stats": {
                "users_count": users.count if users.count is not None else len(all_users_list),
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
            "revenue": revenue,
            "funnel": {
                "free_count": free_cnt,
                "pro_count": pro_cnt,
                "vip_count": vip_cnt,
                "paid_conversion_pct": round(paid_cnt / total_u * 100, 1),
                "vip_share_pct": round(vip_cnt / total_u * 100, 1),
                "top_features": top_features,
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
                    "subscription_plan": u.get("subscription_plan", "free"),
                    "subscription_expires_at": u.get("subscription_expires_at"),
                    "last_active_at": u.get("last_active_at"),
                }
                for u in all_users_list
            ],
            "broadcast_logs": b_logs,
            "pending_payments": pending_payments,
            "giveaways": giveaways,
            "promo_codes": promo_codes,
        }

    data = await run_db(_load)
    data["wallets"] = await get_crypto_wallets()
    data["subscription_prices"] = await get_effective_plans()
    return web.json_response(data)
    data["wallets"] = await get_crypto_wallets()
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
            inline_keyboard=[[InlineKeyboardButton(text="💎 Відкрити FinTrack Hub", web_app=WebAppInfo(url=MINIAPP_URL))]]
        )

    if preview_self or target == "self":
        tg_ids = [request["tg_id"]]
    elif target == "custom":
        tg_ids = [int(x.strip()) for x in custom_ids_raw.split(",") if x.strip().isdigit()]
    else:
        def _load_users():
            rows = (
                supabase.table("users")
                .select("id, telegram_id, role, is_banned, subscription_plan, subscription_expires_at, last_active_at")
                .execute()
                .data
                or []
            )
            now_dt = datetime.now(timezone.utc)
            cutoff_7d = (now_dt - timedelta(days=7)).isoformat()
            active_7d_ids: set[str] = set()
            if target == "inactive_7d":
                try:
                    txs_7d = (
                        supabase.table("transactions")
                        .select("user_id")
                        .gte("transaction_date", cutoff_7d)
                        .execute()
                        .data
                        or []
                    )
                    active_7d_ids = {t["user_id"] for t in txs_7d}
                except Exception:
                    pass

            out = []
            for u in rows:
                tid = u.get("telegram_id")
                if not tid:
                    continue
                if target == "everybody":
                    out.append(tid)
                    continue
                if target == "admins":
                    if u.get("role") == "admin":
                        out.append(tid)
                    continue
                if u.get("is_banned"):
                    continue
                plan = str(u.get("subscription_plan") or "free").lower()
                exp = u.get("subscription_expires_at")
                if target == "free":
                    if plan == "free":
                        out.append(tid)
                elif target == "pro":
                    if plan == "pro":
                        out.append(tid)
                elif target == "vip":
                    if plan == "vip":
                        out.append(tid)
                elif target == "expired":
                    if exp and str(exp) < now_dt.isoformat():
                        out.append(tid)
                elif target == "inactive_7d":
                    last_act = str(u.get("last_active_at") or "")
                    if u.get("id") not in active_7d_ids and (not last_act or last_act < cutoff_7d):
                        out.append(tid)
                else:
                    out.append(tid)
            return out
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


async def handle_admin_set_subscription(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    target_user_id = request.match_info.get("id")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    plan = str(body.get("plan", "free")).lower()
    period = str(body.get("period", "month")).lower()
    days_raw = body.get("days")
    custom_days = int(days_raw) if days_raw and int(days_raw) > 0 else None
    if plan not in ("free", "pro", "vip"):
        return web.json_response({"error": "Тариф має бути free, pro або vip"}, status=400)

    try:
        if plan == "free":
            await run_db(
                lambda: supabase.table("users")
                .update({"subscription_plan": "free", "subscription_expires_at": None})
                .eq("id", target_user_id)
                .execute()
            )
            return web.json_response({"status": "updated", "plan": "free"})
        new_exp = await activate_subscription(target_user_id, plan, period, custom_days=custom_days)
        return web.json_response({"status": "updated", "plan": plan, "expires_at": new_exp})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_admin_review_payment(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    pay_id = request.match_info.get("id")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    decision = str(body.get("decision", "")).lower()  # approve | reject
    if decision not in ("approve", "reject"):
        return web.json_response({"error": "decision має бути approve або reject"}, status=400)

    rows = await run_db(
        lambda: supabase.table("subscription_payments").select("*").eq("id", pay_id).limit(1).execute().data
    )
    if not rows:
        return web.json_response({"error": "Заявку не знайдено"}, status=404)
    pay = rows[0]

    bot = request.app.get("bot")
    if decision == "approve":
        new_exp = await activate_subscription(pay["user_id"], pay["plan"], pay["period"])
        await run_db(
            lambda: supabase.table("subscription_payments").update({"status": "approved"}).eq("id", pay_id).execute()
        )
        if bot and pay.get("telegram_id"):
            try:
                await bot.send_message(
                    pay["telegram_id"],
                    f"🎉 <b>Вашу крипто-оплату підтверджено!</b>\nАктивовано тариф <b>{pay['plan'].upper()}</b> до <b>{new_exp[:10]}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return web.json_response({"status": "approved", "expires_at": new_exp})
    else:
        await run_db(
            lambda: supabase.table("subscription_payments").update({"status": "rejected"}).eq("id", pay_id).execute()
        )
        if bot and pay.get("telegram_id"):
            try:
                await bot.send_message(
                    pay["telegram_id"],
                    "❌ Вашу заявку на крипто-оплату підписки відхилено. Зверніться до адміністратора.",
                )
            except Exception:
                pass
        return web.json_response({"status": "rejected"})


async def handle_admin_save_wallets(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    wallets = body.get("wallets") or {}
    if not isinstance(wallets, dict):
        return web.json_response({"error": "wallets має бути об'єктом"}, status=400)

    try:
        await save_crypto_wallets(wallets)
        return web.json_response({"status": "saved"})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося зберегти гаманці (перевірте migrations.sql): {e}"}, status=400)


async def handle_admin_create_giveaway(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    title = str(body.get("title", "")).strip() or "Розіграш підписок FinTrack Hub"
    plan = str(body.get("plan", "pro")).lower()
    duration_days = max(int(body.get("duration_days", 30)), 1)
    winners_count = max(int(body.get("winners_count", 1)), 1)
    broadcast_to_users = bool(body.get("broadcast_to_users", True))

    if plan not in ("pro", "vip"):
        return web.json_response({"error": "Тариф розіграшу має бути pro або vip"}, status=400)

    try:
        res = await run_db(
            lambda: supabase.table("giveaways")
            .insert(
                {
                    "title": title,
                    "plan": plan,
                    "duration_days": duration_days,
                    "winners_count": winners_count,
                    "participants": [],
                    "winners": [],
                    "status": "active",
                    "created_by": request["tg_id"],
                }
            )
            .execute()
        )
        gw = res.data[0]
    except Exception as e:
        return web.json_response({"error": f"Помилка створення розіграшу (перевірте migrations.sql): {e}"}, status=400)

    sent = 0
    bot = request.app.get("bot")
    if bot and broadcast_to_users:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎁 Взяти участь", callback_data=f"giveaway_join:{gw['id']}")]
            ]
        )
        users = await run_db(
            lambda: supabase.table("users").select("telegram_id, is_banned").execute().data or []
        )
        msg_text = (
            f"🎉 <b>{title}</b>\n\n"
            f"🎁 Приз: підписка <b>{plan.upper()}</b> на <b>{duration_days} дн.</b>\n"
            f"🏆 Переможців: <b>{winners_count}</b>\n\n"
            f"Натисніть кнопку нижче, щоб взяти участь!"
        )
        for u in users:
            if u.get("telegram_id") and not u.get("is_banned"):
                try:
                    await bot.send_message(u["telegram_id"], msg_text, reply_markup=kb, parse_mode="HTML")
                    sent += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    pass

    return web.json_response({"status": "created", "giveaway": gw, "notified": sent})


async def handle_admin_draw_giveaway(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied

    gw_id = request.match_info.get("id")
    rows = await run_db(
        lambda: supabase.table("giveaways").select("*").eq("id", gw_id).limit(1).execute().data
    )
    if not rows:
        return web.json_response({"error": "Розіграш не знайдено"}, status=404)
    gw = rows[0]
    if gw.get("status") == "finished":
        return web.json_response({"error": "Розіграш уже завершено"}, status=400)

    participants: list[int] = list({int(x) for x in (gw.get("participants") or [])})
    if not participants:
        return web.json_response({"error": "Ще немає жодного учасника"}, status=400)

    k = min(int(gw.get("winners_count") or 1), len(participants))
    winners = random.sample(participants, k)
    plan = gw["plan"]
    days = int(gw["duration_days"])

    bot = request.app.get("bot")
    for w_tg_id in winners:
        u_rows = await run_db(
            lambda tid=w_tg_id: supabase.table("users").select("id").eq("telegram_id", tid).limit(1).execute().data
        )
        if u_rows:
            new_exp = await activate_subscription(u_rows[0]["id"], plan, custom_days=days)
            if bot:
                try:
                    await bot.send_message(
                        w_tg_id,
                        f"🏆 <b>Вітаємо! Ви перемогли в розіграші «{gw['title']}»!</b>\n\n"
                        f"Вам нараховано підписку <b>{plan.upper()}</b> на <b>{days} дн.</b> (до {new_exp[:10]})! 🎉",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    await run_db(
        lambda: supabase.table("giveaways")
        .update({"winners": winners, "status": "finished"})
        .eq("id", gw_id)
        .execute()
    )
    return web.json_response({"status": "finished", "winners": winners})


async def handle_admin_create_promo(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    code = str(body.get("code") or "").strip().upper()
    promo_type = str(body.get("promo_type") or "trial").lower()
    plan = str(body.get("plan") or "pro").lower()
    duration_days = max(1, min(365, int(body.get("duration_days") or 14)))
    discount_percent = max(0, min(90, int(body.get("discount_percent") or 0)))
    max_uses = max(1, int(body.get("max_uses") or 100))

    if not code or len(code) < 3:
        return web.json_response({"error": "Код має містити щонайменше 3 символи"}, status=400)
    if promo_type not in ("trial", "discount") or plan not in ("pro", "vip"):
        return web.json_response({"error": "Некоректний тип або тариф"}, status=400)
    if promo_type == "discount" and discount_percent <= 0:
        return web.json_response({"error": "Вкажіть відсоток знижки (1-90%)"}, status=400)

    try:
        res = await run_db(
            lambda: supabase.table("promo_codes")
            .insert(
                {
                    "code": code[:32],
                    "promo_type": promo_type,
                    "plan": plan,
                    "duration_days": duration_days,
                    "discount_percent": discount_percent,
                    "max_uses": max_uses,
                    "is_active": True,
                }
            )
            .execute()
        )
        return web.json_response({"status": "created", "promo": res.data[0]})
    except Exception as e:
        return web.json_response({"error": f"Не вдалося створити промокод: {e}"}, status=400)


async def handle_admin_toggle_promo(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    promo_id = request.match_info.get("id")
    try:
        rows = await run_db(
            lambda: supabase.table("promo_codes").select("is_active").eq("id", promo_id).limit(1).execute().data or []
        )
        if not rows:
            return web.json_response({"error": "Промокод не знайдено"}, status=404)
        new_val = not bool(rows[0].get("is_active", True))
        await run_db(lambda: supabase.table("promo_codes").update({"is_active": new_val}).eq("id", promo_id).execute())
        return web.json_response({"status": "updated", "is_active": new_val})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_admin_save_prices(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)
    try:
        updated = await save_subscription_prices(body.get("prices") or body)
        return web.json_response({"status": "saved", "subscription_prices": updated})
    except Exception as e:
        return web.json_response({"error": f"Помилка збереження цін: {e}"}, status=400)


async def handle_admin_gift_segment(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    segment = str(body.get("segment") or "free").lower()
    plan = str(body.get("plan") or "pro").lower()
    days = max(1, min(365, int(body.get("days") or 7)))
    if plan not in ("pro", "vip"):
        return web.json_response({"error": "Некоректний тариф"}, status=400)

    users = await run_db(
        lambda: supabase.table("users")
        .select("id, telegram_id, is_banned, subscription_plan, subscription_expires_at")
        .execute()
        .data
        or []
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    targets = []
    for u in users:
        if u.get("is_banned"):
            continue
        u_plan = str(u.get("subscription_plan") or "free").lower()
        exp = u.get("subscription_expires_at")
        if segment == "all":
            targets.append(u)
        elif segment == "free" and u_plan == "free":
            targets.append(u)
        elif segment == "pro" and u_plan == "pro":
            targets.append(u)
        elif segment == "vip" and u_plan == "vip":
            targets.append(u)
        elif segment == "expired" and exp and str(exp) < now_iso:
            targets.append(u)

    bot = request.app.get("bot")
    gifted = 0
    for u in targets:
        try:
            new_exp = await activate_subscription(u["id"], plan=plan, custom_days=days)
            gifted += 1
            if bot and u.get("telegram_id"):
                try:
                    await bot.send_message(
                        u["telegram_id"],
                        f"🎁 <b>Подарунок від FinTrack Hub!</b>\n\n"
                        f"Вам нараховано тариф <b>{plan.upper()}</b> на <b>{days} дн.</b> (до {new_exp[:10]})! 🚀",
                        parse_mode="HTML",
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
        except Exception:
            pass

    return web.json_response({"status": "gifted", "count": gifted, "plan": plan, "days": days})


async def handle_admin_export_users(request: web.Request):
    denied = await _require_admin(request)
    if denied:
        return denied
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot unavailable"}, status=500)

    rows = await run_db(
        lambda: supabase.table("users")
        .select("telegram_id, username, first_name, role, is_banned, base_currency, subscription_plan, subscription_expires_at, last_active_at")
        .order("telegram_id")
        .execute()
        .data
        or []
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["telegram_id", "username", "first_name", "role", "is_banned", "base_currency", "plan", "expires_at", "last_active_at"])
    for r in rows:
        writer.writerow([
            r.get("telegram_id", ""),
            r.get("username", ""),
            r.get("first_name", ""),
            r.get("role", "user"),
            r.get("is_banned", False),
            r.get("base_currency", "UAH"),
            r.get("subscription_plan", "free"),
            str(r.get("subscription_expires_at") or "")[:10],
            str(r.get("last_active_at") or "")[:19],
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    doc = BufferedInputFile(csv_bytes, filename=f"fintrack_users_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv")
    await bot.send_document(
        chat_id=request["tg_id"],
        document=doc,
        caption=f"📊 Експорт користувачів FinTrack Hub ({len(rows)} записів)",
    )
    return web.json_response({"status": "sent", "count": len(rows)})
