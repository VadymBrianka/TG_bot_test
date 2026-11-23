import logging
from aiohttp import web
from aiogram.types import LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton

from config import ADMIN_ID
from db import supabase, run_db
from services.subscriptions import (
    get_effective_plans,
    get_user_subscription,
    get_crypto_wallets,
    activate_subscription,
    get_user_active_promo,
    redeem_promo_code,
)
from services.blockchain_verify import verify_crypto_payment


async def handle_get_plans(request: web.Request):
    user_uuid: str = request["user_uuid"]
    tg_id: int = request["tg_id"]
    sub = await get_user_subscription(tg_id)
    wallets = await get_crypto_wallets()
    plans = await get_effective_plans()
    active_promo = await get_user_active_promo(user_uuid)

    if active_promo and active_promo.get("discount_percent"):
        pct = min(90, max(1, int(active_promo["discount_percent"])))
        promo_plan = active_promo.get("plan")
        for p_key in ("pro", "vip"):
            if not promo_plan or promo_plan == p_key:
                plans[p_key]["stars_month"] = max(1, round(plans[p_key]["stars_month"] * (100 - pct) / 100))
                plans[p_key]["stars_year"] = max(1, round(plans[p_key]["stars_year"] * (100 - pct) / 100))
                plans[p_key]["usd_month"] = round(max(0.1, plans[p_key]["usd_month"] * (100 - pct) / 100), 2)
                plans[p_key]["usd_year"] = round(max(0.1, plans[p_key]["usd_year"] * (100 - pct) / 100), 2)

    try:
        payments = await run_db(
            lambda: supabase.table("subscription_payments")
            .select("*")
            .eq("user_id", user_uuid)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
    except Exception:
        payments = []

    return web.json_response(
        {
            "plans": plans,
            "subscription": sub,
            "wallets": wallets,
            "payments": payments,
            "active_promo": (
                {"code": active_promo["code"], "discount_percent": active_promo["discount_percent"], "plan": active_promo.get("plan")}
                if active_promo
                else None
            ),
        }
    )


async def handle_create_stars_invoice(request: web.Request):
    user_uuid: str = request["user_uuid"]
    bot = request.app.get("bot")
    if not bot:
        return web.json_response({"error": "Bot unavailable"}, status=500)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    plan = str(body.get("plan", "")).lower()
    period = str(body.get("period", "month")).lower()
    if plan not in ("pro", "vip") or period not in ("month", "year"):
        return web.json_response({"error": "Некоректний тариф або період"}, status=400)

    plans = await get_effective_plans()
    stars_amount = plans[plan][f"stars_{period}"]
    active_promo = await get_user_active_promo(user_uuid)
    if active_promo and active_promo.get("discount_percent") and (not active_promo.get("plan") or active_promo["plan"] == plan):
        pct = min(90, max(1, int(active_promo["discount_percent"])))
        stars_amount = max(1, round(stars_amount * (100 - pct) / 100))

    period_label = "1 рік (-33%)" if period == "year" else "1 місяць"
    title = f"FinTrack Hub {plans[plan]['name']} ({period_label})"
    description = ", ".join(plans[plan]["features"][:3])
    payload = f"sub:{user_uuid}:{plan}:{period}"

    try:
        invoice_url = await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=int(stars_amount))],
        )
        return web.json_response({"invoice_url": invoice_url, "stars": stars_amount})
    except Exception as e:
        logging.error(f"Stars invoice error: {e}")
        return web.json_response({"error": f"Не вдалося створити рахунок Telegram Stars: {e}"}, status=502)


async def handle_create_crypto_request(request: web.Request):
    user_uuid: str = request["user_uuid"]
    tg_id: int = request["tg_id"]
    bot = request.app.get("bot")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    plan = str(body.get("plan", "")).lower()
    period = str(body.get("period", "month")).lower()
    network_key = str(body.get("network_key", "USDT_TRC20")).strip()
    currency = str(body.get("currency", "USDT")).strip().upper()
    tx_hash = str(body.get("tx_hash", "")).strip()

    if plan not in ("pro", "vip") or period not in ("month", "year"):
        return web.json_response({"error": "Некоректний тариф або період"}, status=400)
    if not tx_hash:
        return web.json_response({"error": "Вкажіть TX Hash або деталі переказу"}, status=400)

    # Prevent TX Hash replay
    try:
        dup = await run_db(
            lambda: supabase.table("subscription_payments")
            .select("id")
            .eq("tx_hash", tx_hash[:250])
            .in_("status", ["approved", "paid"])
            .limit(1)
            .execute()
            .data
        )
        if dup:
            return web.json_response({"error": "Цей TX Hash уже було використано раніше."}, status=400)
    except Exception:
        pass

    plans = await get_effective_plans()
    amount = float(plans[plan][f"usd_{period}"])
    active_promo = await get_user_active_promo(user_uuid)
    if active_promo and active_promo.get("discount_percent") and (not active_promo.get("plan") or active_promo["plan"] == plan):
        pct = min(90, max(1, int(active_promo["discount_percent"])))
        amount = round(max(0.1, amount * (100 - pct) / 100), 2)
    wallets = await get_crypto_wallets()
    expected_wallet = wallets.get(network_key, "")

    verified, verify_msg = await verify_crypto_payment(
        network_key=network_key,
        tx_hash=tx_hash,
        expected_wallet=expected_wallet,
        expected_usd=amount,
    )

    status_val = "approved" if verified else "pending"

    try:
        res = await run_db(
            lambda: supabase.table("subscription_payments")
            .insert(
                {
                    "user_id": user_uuid,
                    "telegram_id": tg_id,
                    "plan": plan,
                    "period": period,
                    "payment_method": "crypto",
                    "currency": currency,
                    "amount": amount,
                    "tx_hash": tx_hash[:250],
                    "status": status_val,
                }
            )
            .execute()
        )
        pay_row = res.data[0]
    except Exception as e:
        return web.json_response({"error": f"Помилка збереження заявки (перевірте migrations.sql): {e}"}, status=400)

    if verified:
        new_exp = await activate_subscription(user_uuid, plan, period)
        if bot:
            try:
                await bot.send_message(
                    tg_id,
                    f"🎉 <b>Оплату автоматично підтверджено в блокчейні!</b>\n"
                    f"Активовано тариф <b>{plan.upper()}</b> до <b>{new_exp[:10]}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return web.json_response(
            {
                "status": "approved",
                "expires_at": new_exp,
                "message": f"Оплату підтверджено автоматично ({verify_msg})!",
                "payment": pay_row,
            }
        )

    if bot:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"sub_ok:{pay_row['id']}"),
                    InlineKeyboardButton(text="❌ Відхилити", callback_data=f"sub_rej:{pay_row['id']}"),
                ]
            ]
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🪙 <b>Нова крипто-оплата підписки (очікує перевірки)</b>\n\n"
                f"👤 Користувач: <code>{tg_id}</code>\n"
                f"💎 Тариф: <b>{plan.upper()} ({period})</b>\n"
                f"💵 Сума: <b>{amount} {currency} ({network_key})</b>\n"
                f"🔗 TX: <code>{tx_hash}</code>\n"
                f"ℹ️ Статус автоперевірки: <i>{verify_msg}</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            pass

    return web.json_response({"status": "pending", "message": verify_msg, "payment": pay_row})


async def handle_redeem_promo(request: web.Request):
    user_uuid: str = request["user_uuid"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    code = str(body.get("code") or "").strip()
    try:
        res = await redeem_promo_code(user_uuid, code)
        return web.json_response(res)
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Помилка активації промокоду: {e}"}, status=500)
