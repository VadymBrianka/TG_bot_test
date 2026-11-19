import logging
from aiohttp import web
from services.receipt_ai import parse_receipt_image, match_category_id
from services.subscriptions import require_plan


async def handle_parse_receipt(request: web.Request):
    denied = await require_plan(request, "pro")
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Некоректний JSON"}, status=400)

    image_b64 = str(body.get("image_base64") or "").strip()
    mime_type = str(body.get("mime_type") or "image/jpeg").strip()
    if not image_b64:
        return web.json_response({"error": "Фото чеку не передано"}, status=400)

    try:
        parsed = await parse_receipt_image(image_b64, mime_type=mime_type)
        cat_id, cat_label = await match_category_id(parsed.get("category_hint", ""))
        return web.json_response(
            {
                **parsed,
                "category_id": cat_id,
                "category_label": cat_label,
            }
        )
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        logging.error(f"Receipt parse error: {e!r}")
        return web.json_response({"error": str(e) or "Помилка розпізнавання чеку"}, status=502)
