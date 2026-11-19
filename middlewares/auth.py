import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl
from aiohttp import web

from config import BOT_TOKEN, ALLOWED_ORIGINS, PRIMARY_ORIGIN, ALLOW_INSECURE_TG_ID
from db import get_user_uuid_by_tg_id

PUBLIC_PATHS = {
    "/",
    "/health",
    "/api/v1/currencies",
    "/api/v1/market/search",
    "/api/v1/market/quote",
}


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> int | None:
    """Verify Telegram WebApp initData HMAC-SHA256 signature and return telegram user_id."""
    if not init_data or not bot_token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        auth_date = int(parsed.get("auth_date", "0") or 0)
        if auth_date <= 0 or (time.time() - auth_date) > max_age_seconds:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user_raw = parsed.get("user")
        if not user_raw:
            return None
        user_obj = json.loads(user_raw)
        tg_id = user_obj.get("id")
        return int(tg_id) if tg_id else None
    except Exception as e:
        logging.debug(f"initData verification error: {e}")
        return None


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin", "").rstrip("/")
    allowed_origin = origin if origin in ALLOWED_ORIGINS else PRIMARY_ORIGIN

    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)

    if allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = allowed_origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Telegram-Init-Data, ngrok-skip-browser-warning"
    )
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.method == "OPTIONS" or request.path in PUBLIC_PATHS or not request.path.startswith("/api/"):
        return await handler(request)

    auth_header = request.headers.get("Authorization", "").strip()
    init_data = ""
    if auth_header.lower().startswith("tma "):
        init_data = auth_header[4:].strip()
    elif auth_header.lower().startswith("bearer "):
        init_data = auth_header[7:].strip()
    elif auth_header:
        init_data = auth_header
    else:
        init_data = request.headers.get("X-Telegram-Init-Data", "").strip()

    tg_id = verify_telegram_init_data(init_data, BOT_TOKEN)

    if tg_id is None and ALLOW_INSECURE_TG_ID:
        raw_id = request.query.get("telegram_id")
        if raw_id and raw_id.isdigit():
            tg_id = int(raw_id)

    if tg_id is None:
        return web.json_response(
            {"error": "Unauthorized: invalid or missing Telegram initData"},
            status=401,
        )

    user_uuid = await get_user_uuid_by_tg_id(tg_id)
    if not user_uuid:
        return web.json_response({"error": "User not found. Press /start in the bot first."}, status=404)

    request["tg_id"] = tg_id
    request["user_uuid"] = user_uuid
    return await handler(request)
