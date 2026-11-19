import asyncio
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from db import supabase, run_db, is_admin
from services.nbu import update_rates_from_nbu

router = Router()


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
                InlineKeyboardButton(text="👥 Користувачі", callback_data="adm:users"),
            ],
            [
                InlineKeyboardButton(text="🔄 Оновити курси НБУ", callback_data="adm:rates"),
                InlineKeyboardButton(text="📢 Розсилка", callback_data="adm:bcast"),
            ],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("⛔ Доступ заборонено. Потрібна роль <b>admin</b>.", parse_mode="HTML")
        return
    await message.answer(
        "🛡️ <b>Панель адміністратора FinTrack Hub</b>\n\n"
        "Оберіть дію нижче або використовуйте команди:\n"
        "• <code>/broadcast текст</code> — розсилка всім користувачам\n"
        "• <code>/setrole &lt;telegram_id&gt; &lt;admin|user&gt;</code> — змінити роль",
        reply_markup=_admin_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_panel(callback: CallbackQuery):
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("⛔ Потрібна роль admin", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    if action == "stats":
        def _stats():
            u = supabase.table("users").select("id", count="exact").limit(1).execute().count or 0
            t = supabase.table("transactions").select("id", count="exact").limit(1).execute().count or 0
            a = supabase.table("accounts").select("id", count="exact").limit(1).execute().count or 0
            g = supabase.table("goals").select("id", count="exact").limit(1).execute().count or 0
            i = supabase.table("investments").select("id", count="exact").limit(1).execute().count or 0
            return u, t, a, g, i

        u, t, a, g, i = await run_db(_stats)
        await callback.message.edit_text(
            f"📊 <b>Статистика платформи:</b>\n\n"
            f"👤 Користувачів: <b>{u}</b>\n"
            f"📝 Транзакцій: <b>{t}</b>\n"
            f"💳 Рахунків: <b>{a}</b>\n"
            f"🎯 Цілей: <b>{g}</b>\n"
            f"📈 Активів: <b>{i}</b>",
            reply_markup=_admin_keyboard(),
            parse_mode="HTML",
        )
        await callback.answer()

    elif action == "users":
        users = await run_db(
            lambda: supabase.table("users").select("telegram_id, username, first_name, role").limit(20).execute().data or []
        )
        lines = ["👥 <b>Користувачі (до 20):</b>\n"]
        for u in users:
            uname = f"@{u['username']}" if u.get("username") else (u.get("first_name") or "—")
            role_badge = "🛡️" if u.get("role") == "admin" else "👤"
            lines.append(f"{role_badge} <code>{u.get('telegram_id')}</code> — {uname} ({u.get('role', 'user')})")
        await callback.message.edit_text("\n".join(lines), reply_markup=_admin_keyboard(), parse_mode="HTML")
        await callback.answer()

    elif action == "rates":
        await update_rates_from_nbu()
        await callback.answer("✅ Курси НБУ оновлено!", show_alert=True)

    elif action == "bcast":
        await callback.message.answer(
            "📢 Для розсилки надішліть команду:\n<code>/broadcast Ваш текст повідомлення</code>",
            parse_mode="HTML",
        )
        await callback.answer()


@router.message(Command("setrole"))
async def cmd_setrole(message: Message):
    if not message.from_user or not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or parts[2].lower() not in ("admin", "user") or not parts[1].isdigit():
        await message.reply("Формат: <code>/setrole 123456789 admin</code> або <code>user</code>", parse_mode="HTML")
        return

    target_tg_id = int(parts[1])
    new_role = parts[2].lower()
    try:
        res = await run_db(
            lambda: supabase.table("users").update({"role": new_role}).eq("telegram_id", target_tg_id).execute()
        )
        if not res.data:
            await message.reply("Користувача з таким Telegram ID не знайдено.")
            return
        await message.reply(f"✅ Користувачу <code>{target_tg_id}</code> призначено роль <b>{new_role}</b>.", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ Помилка (перевірте міграцію колонки role): {e}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not message.from_user or not await is_admin(message.from_user.id):
        return

    text_to_send = (message.text or "").partition(" ")[2].strip()
    if not text_to_send:
        await message.reply(
            "Вкажіть текст для розсилки!\nПриклад: <code>/broadcast Увага! Оновлено курси валют.</code>",
            parse_mode="HTML",
        )
        return

    users = await run_db(lambda: supabase.table("users").select("telegram_id").execute().data or [])
    status_msg = await message.answer(f"⏳ Розпочато розсилку для {len(users)} користувачів...")

    success_count = 0
    blocked_count = 0
    bot = message.bot

    for user in users:
        user_tg_id = user.get("telegram_id")
        if not user_tg_id:
            continue
        try:
            await bot.send_message(chat_id=user_tg_id, text=text_to_send, parse_mode="HTML")
            success_count += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked_count += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.send_message(chat_id=user_tg_id, text=text_to_send, parse_mode="HTML")
                success_count += 1
            except Exception:
                pass
        except Exception as e:
            logging.warning(f"Не вдалося надіслати {user_tg_id}: {e}")

    await status_msg.edit_text(
        f"✅ <b>Розсилку завершено!</b>\n\n"
        f"• Успішно надіслано: <b>{success_count}</b>\n"
        f"• Заблокували бота: <b>{blocked_count}</b>",
        parse_mode="HTML",
    )
