import os
import json
import hmac
import hashlib
import asyncio
import logging
from urllib.parse import parse_qsl
from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    WebAppInfo,
    MenuButtonWebApp
)
from aiogram.filters import CommandStart, Command

# --- 1. ЗАВАНТАЖЕННЯ ЗМІННИХ ОТОЧЕННЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL")
SERVER_PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not MINIAPP_URL:
    raise ValueError("Перевірте наявність BOT_TOKEN та MINIAPP_URL у файлі .env або Environment Variables!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- 2. КЛАВІАТУРИ ---

def get_inline_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопка в повідомленні чату"""
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

# --- 3. БЕЗПЕЧНА ВАЛІДАЦІЯ TELEGRAM INITDATA ---

def validate_init_data(init_data: str, token: str) -> dict | None:
    """
    Перевіряє автентичність сесії за допомогою алгоритму HMAC-SHA256.
    """
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
        logging.error(f"Помилка перевірки initData: {e}")
        return None

# --- 4. CORS MIDDLEWARE ---

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """
    Забезпечує підтримку CORS, щоб запити з домену Vercel не блокувалися браузером/WebView.
    """
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, ngrok-skip-browser-warning"
    return response

# --- 5. HTTP ОБРОБНИКИ (ЕНДПОІНТИ) ---

async def handle_health(request: web.Request):
    """
    Health-check ендпоінт для зовнішніх пінгувальників (UptimeRobot, Cron-job.org)
    і Render, щоб сервер не засинав і відповідав статусом 200 OK.
    """
    return web.Response(text="Bot & Server are healthy and running! 🚀", status=200)

async def handle_calculation(request: web.Request):
    """
    Обробник POST-запиту з калькулятора Mini App.
    Приймає вираз та результат і надсилає їх у чат користувачеві.
    """
    try:
        data = await request.json()
        init_data = data.get("initData")
        user_id = data.get("userId")
        equation = data.get("equation", "")
        result = data.get("result", "")

        # Якщо є initData — безпечно витягуємо user_id з нього
        if init_data:
            validated = validate_init_data(init_data, BOT_TOKEN)
            if validated:
                user_info = json.loads(validated.get("user", "{}"))
                user_id = user_info.get("id")

        if not user_id:
            return web.json_response({"error": "User ID not identified"}, status=400)

        # Надсилаємо акуратне повідомлення з розрахунком прямо в чат Telegram
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
        logging.error(f"Помилка при збереженні обчислення: {e}")
        return web.json_response({"error": str(e)}, status=500)

# --- 6. ОБРОБНИКИ КОМАНД TELEGRAM-БОТА ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    user_name = message.from_user.first_name
    chat_id = message.chat.id

    # Встановлюємо нативну кнопку Menu Button внизу ліворуч (біля скріпки)
    # Ця кнопка завжди коректно передає initData та не закриває вікно примусово
    try:
        await bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(text="🧮 Калькулятор", web_app=WebAppInfo(url=MINIAPP_URL))
        )
    except Exception as e:
        logging.warning(f"Не вдалося встановити Menu Button: {e}")

    await message.answer(
        f"Привіт, {user_name}! 👋\n\n"
        "Я підтримую розрахунки прямо у вбудованому Telegram Mini App.\n"
        "Відкрити калькулятор можна кнопкою під цим повідомленням або через меню зліва від поля вводу 🧮",
        reply_markup=get_inline_keyboard()
    )

@dp.message(Command("help"))
async def handle_help(message: Message):
    await message.answer(
        "📖 <b>Довідка:</b>\n"
        "• Відкрийте калькулятор через кнопку під повідомленням або кнопку «Меню» внизу.\n"
        "• Рахуйте вирази та тисніть кнопку «Надіслати результат в чат».\n"
        "• Застосунок не закривається автоматично — закрити його можна хрестиком у кутку.",
        parse_mode="HTML"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    await message.reply(
        "Натисніть кнопку нижче, щоб відкрити калькулятор:", 
        reply_markup=get_inline_keyboard()
    )

# --- 7. ТОЧКА ВХОДУ (ЗАПУСК HTTP-СЕРВЕРА ТА POLLING) ---

async def main():
    # Налаштування HTTP-сервера aiohttp
    app = web.Application(middlewares=[cors_middleware])
    
    # Реєстрація ендпоінтів
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/send-calculation", handle_calculation)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    logging.info(f"API сервер успішно запущено на порті {SERVER_PORT}...")

    # Запуск опитування подій Telegram
    print("Бот запущений і слухає оновлення...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())