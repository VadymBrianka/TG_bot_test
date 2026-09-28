import os
import json
import hmac
import hashlib
import asyncio
import logging
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from aiogram.types import MenuButtonWebApp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    WebAppInfo,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.filters import CommandStart, Command

# 1. Завантаження змінних оточення
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL")
SERVER_PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not MINIAPP_URL:
    raise ValueError("Перевірте наявність BOT_TOKEN та MINIAPP_URL у файлі .env!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- КЛАВІАТУРИ ---

def get_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📱 Відкрити Калькулятор", 
                web_app=WebAppInfo(url=MINIAPP_URL)
            )
        ],
        [
            InlineKeyboardButton(text="ℹ️ Допомога", callback_data="btn_help")
        ]
    ])

def get_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    # Додаємо user_id до URL, щоб додаток знав, кому надсилати результат
    user_app_url = f"{MINIAPP_URL}?user_id={user_id}"
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="🧮 Відкрити Калькулятор", 
                    web_app=WebAppInfo(url=user_app_url)
                )
            ]
        ],
        resize_keyboard=True
    )

@dp.message(CommandStart())
async def handle_start(message: Message):
    user_id = message.from_user.id
    await message.answer(
        f"Привіт, {message.from_user.first_name}! 👋",
        reply_markup=get_menu_keyboard(user_id)
    )
    await message.answer("Швидкий запуск:", reply_markup=get_inline_keyboard())

# --- ВАЛІДАЦІЯ ТЕЛЕГРАМ INITDATA ---

def validate_init_data(init_data: str, token: str) -> dict | None:
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None
        
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if hmac.compare_digest(calculated_hash, received_hash):
            return parsed_data
        return None
    except Exception as e:
        logging.error(f"Помилка валідації: {e}")
        return None

# --- HTTP ЕНДПОІНТ ДЛЯ КАЛЬКУЛЯТОРА ---

async def handle_calculation(request: web.Request):
    try:
        data = await request.json()
        init_data = data.get("initData")
        user_id = data.get("userId")
        equation = data.get("equation", "")
        result = data.get("result", "")

        # Якщо є initData — беремо user_id з нього (безпечно)
        if init_data:
            validated = validate_init_data(init_data, BOT_TOKEN)
            if validated:
                user_info = json.loads(validated.get("user", "{}"))
                user_id = user_info.get("id")

        # Якщо initData був порожнім (нижня кнопка), але передався userId з URL
        if not user_id:
            return web.json_response({"error": "User ID not identified"}, status=400)

        # Відправляємо повідомлення в чат користувачеві
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🧮 <b>Розрахунок з Mini App:</b>\n\n"
                f"📝 Вираз: <code>{equation}</code>\n"
                f"🎯 Результат: <b>{result}</b>"
            ),
            parse_mode="HTML"
        )

        return web.json_response({"status": "success", "result": result})

    except Exception as e:
        logging.error(f"Помилка API розрахунку: {e}")
        return web.json_response({"error": str(e)}, status=500)
    
# --- CORS MIDDLEWARE ---

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, ngrok-skip-browser-warning"
    return response

# --- ОБРОБНИКИ КОМАНД БОТА ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    user_name = message.from_user.first_name
    await message.answer(
        f"Привіт, {user_name}! 👋\n\n"
        "Скористайтеся кнопками нижче для запуску калькулятора.",
        reply_markup=get_menu_keyboard()
    )
    await message.answer("Швидкий запуск додатку:", reply_markup=get_inline_keyboard())

@dp.message(Command("help"))
async def handle_help(message: Message):
    await message.answer("Відкрийте калькулятор через меню, рахуйте значення та надсилайте результат сюди в чат.")

@dp.message(CommandStart())
async def handle_start(message: Message):
    user_name = message.from_user.first_name
    
    # Встановлюємо нативну кнопку Menu Button внизу зліва (біля скріпки)
    # Ця кнопка ВСІ ДАНІ initData передає на 100%!
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(text="🧮 Калькулятор", web_app=WebAppInfo(url=MINIAPP_URL))
    )

    await message.answer(
        f"Привіт, {user_name}! 👋\n\n"
        "Калькулятор доступний за кнопкою нижче або в меню чату (зліва біля скріпки):",
        reply_markup=get_inline_keyboard()
    )

# --- ТОЧКА ВХОДУ ---

async def main():
    # Налаштовуємо локальний сервер aiohttp
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/api/send-calculation", handle_calculation)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    logging.info(f"API сервер запущено на порті {SERVER_PORT}...")

    # Запускаємо бота
    print("Бот запущений і слухає повідомлення...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())