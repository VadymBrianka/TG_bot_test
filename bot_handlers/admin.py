import asyncio
import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import ADMIN_ID
from db import supabase, run_db

router = Router()


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
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
