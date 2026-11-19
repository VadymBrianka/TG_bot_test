from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from db import supabase, run_db, get_user_uuid_by_tg_id, is_admin
from services.subscriptions import PLANS, get_user_subscription, activate_subscription

router = Router()


@router.message(Command("subscribe", "premium"))
async def cmd_subscribe(message: Message):
    if not message.from_user:
        return
    sub = await get_user_subscription(message.from_user.id)
    exp_text = f" (до {str(sub['expires_at'])[:10]})" if sub.get("expires_at") else ""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭐ Pro 1 міс (149 ⭐)", callback_data="buy_stars:pro:month"),
                InlineKeyboardButton(text="⭐ Pro 1 рік (1190 ⭐)", callback_data="buy_stars:pro:year"),
            ],
            [
                InlineKeyboardButton(text="👑 VIP 1 міс (299 ⭐)", callback_data="buy_stars:vip:month"),
                InlineKeyboardButton(text="👑 VIP 1 рік (2390 ⭐)", callback_data="buy_stars:vip:year"),
            ],
        ]
    )
    await message.answer(
        f"💎 <b>Підписка FinTrack Hub</b>\n"
        f"Ваш поточний тариф: <b>{sub['plan'].upper()}</b>{exp_text}\n\n"
        f"• <b>Pro</b> — ШІ-чеки, прогноз, бюджети, регулярні платежі, борги, CSV\n"
        f"• <b>VIP Investor</b> — усе з Pro + інвестиції/депозити/ОВДП, алерти цін, симулятор, дайджести\n\n"
        f"Оберіть тариф для миттєвої оплати в <b>Telegram Stars ⭐</b> або оплатіть криптою (USDT/USDC/TON) у вкладці <b>💎 Підписка</b> в Mini App:",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(callback: CallbackQuery):
    if not callback.from_user:
        return
    _, plan, period = callback.data.split(":", 2)
    if plan not in ("pro", "vip") or period not in ("month", "year"):
        await callback.answer("Некоректний тариф")
        return

    user_uuid = await get_user_uuid_by_tg_id(callback.from_user.id)
    if not user_uuid:
        await callback.answer("Спочатку натисніть /start", show_alert=True)
        return

    stars = PLANS[plan][f"stars_{period}"]
    period_label = "1 рік (-33%)" if period == "year" else "1 місяць"
    title = f"FinTrack {PLANS[plan]['name']} ({period_label})"
    await callback.message.answer_invoice(
        title=title,
        description=", ".join(PLANS[plan]["features"][:3]),
        payload=f"sub:{user_uuid}:{plan}:{period}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=int(stars))],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    if not message.from_user or not message.successful_payment:
        return
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith("sub:"):
        return

    _, user_uuid, plan, period = payload.split(":", 3)
    new_exp = await activate_subscription(user_uuid, plan, period)
    try:
        await run_db(
            lambda: supabase.table("subscription_payments")
            .insert(
                {
                    "user_id": user_uuid,
                    "telegram_id": message.from_user.id,
                    "plan": plan,
                    "period": period,
                    "payment_method": "stars",
                    "currency": "XTR",
                    "amount": sp.total_amount,
                    "tx_hash": sp.telegram_payment_charge_id or "",
                    "status": "paid",
                }
            )
            .execute()
        )
    except Exception:
        pass

    await message.answer(
        f"🎉 <b>Оплата успішна!</b>\n"
        f"Активовано тариф <b>{PLANS[plan]['name']}</b> до <b>{new_exp[:10]}</b>.\n"
        f"Перевідкрийте Mini App, щоб користуватися всіма функціями!",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("sub_ok:") | F.data.startswith("sub_rej:"))
async def cb_review_crypto_sub(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("⛔ Потрібна роль admin", show_alert=True)
        return

    action, pay_id = callback.data.split(":", 1)
    rows = await run_db(
        lambda: supabase.table("subscription_payments").select("*").eq("id", pay_id).limit(1).execute().data
    )
    if not rows:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return
    pay = rows[0]
    if pay.get("status") != "pending":
        await callback.answer(f"Вже оброблено ({pay.get('status')})", show_alert=True)
        return

    if action == "sub_ok":
        new_exp = await activate_subscription(pay["user_id"], pay["plan"], pay["period"])
        await run_db(
            lambda: supabase.table("subscription_payments").update({"status": "approved"}).eq("id", pay_id).execute()
        )
        await callback.message.edit_text(
            f"✅ <b>Крипто-оплату підтверджено!</b>\n"
            f"Користувач: <code>{pay['telegram_id']}</code> | Тариф: <b>{pay['plan'].upper()}</b> до {new_exp[:10]}",
            parse_mode="HTML",
        )
        try:
            await callback.bot.send_message(
                pay["telegram_id"],
                f"🎉 <b>Вашу крипто-оплату підтверджено!</b>\n"
                f"Активовано тариф <b>{pay['plan'].upper()}</b> до <b>{new_exp[:10]}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await callback.answer("Підтверджено!")
    else:
        await run_db(
            lambda: supabase.table("subscription_payments").update({"status": "rejected"}).eq("id", pay_id).execute()
        )
        await callback.message.edit_text(
            f"❌ <b>Крипто-оплату відхилено</b> (користувач <code>{pay['telegram_id']}</code>).",
            parse_mode="HTML",
        )
        try:
            await callback.bot.send_message(
                pay["telegram_id"],
                "❌ На жаль, вашу заявку на крипто-оплату підписки відхилено. Зверніться до адміністратора.",
            )
        except Exception:
            pass
        await callback.answer("Відхилено")
