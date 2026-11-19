from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery

from config import ADMIN_ID
from db import get_user_by_tg_id


class BanCheckMiddleware(BaseMiddleware):
    """Block banned users from interacting with the Telegram bot chat."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id and user_id != ADMIN_ID:
            user = await get_user_by_tg_id(user_id)
            if user and user.get("is_banned"):
                if isinstance(event, Message):
                    await event.answer("⛔ Ваш акаунт заблоковано адміністратором.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("⛔ Ваш акаунт заблоковано адміністратором.", show_alert=True)
                return None

        return await handler(event, data)
