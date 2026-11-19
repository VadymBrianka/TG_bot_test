import asyncio
import base64
import json
import logging
from typing import Any
import aiohttp

from config import GEMINI_API_KEY
from db import supabase, run_db

MODELS_FALLBACK = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]


PROMPT = """Ти — фінансовий асистент. Проаналізуй фото касового чеку (або квитанції) та поверни ТІЛЬКИ JSON-об'єкт з такими полями:
- "merchant": назва магазину або закладу (рядок, наприклад "Сільпо", "АТБ", "OKKO", "McDonald's")
- "total_amount": підсумкова сума до сплати (число float, більше 0)
- "currency": 3-літерний код валюти ("UAH", "USD", "EUR", "PLN", тощо; за замовчуванням "UAH" для українських чеків)
- "date": дата чеку у форматі "YYYY-MM-DD" або null, якщо не видно
- "category_hint": одна з категорій: "Продукти", "Кафе", "Транспорт", "Здоров'я", "Одяг", "Комунальні", "Розваги", "Інше"
- "items_summary": короткий опис 2-4 основних товарів/послуг через кому (до 120 символів)

Якщо це не чек або суму неможливо розпізнати, поверни {"error": "Не вдалося розпізнати суму на чеку"}."""


async def parse_receipt_image(image_b64: str, mime_type: str = "image/jpeg") -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise ValueError("ШІ-розпізнавання чеків не налаштовано: додайте GEMINI_API_KEY у змінні середовища сервера.")

    clean_b64 = image_b64.strip()
    if "," in clean_b64 and clean_b64.startswith("data:"):
        header, clean_b64 = clean_b64.split(",", 1)
        if ";" in header and ":" in header:
            mime_type = header.split(":", 1)[1].split(";", 1)[0] or mime_type

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime_type, "data": clean_b64}},
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    data = None
    last_err = "ШІ-сервіс тимчасово недоступний, спробуйте за хвилину."
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        for model_name in MODELS_FALLBACK:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    async with session.post(url, json=payload) as resp:
                        body_text = await resp.text()
                        if resp.status == 200:
                            data = json.loads(body_text)
                            break
                        logging.warning(f"Gemini {model_name} HTTP {resp.status}: {body_text[:250]}")
                        if resp.status in (503, 429) and attempt == 0:
                            await asyncio.sleep(1.5)
                            continue
                        if resp.status in (404, 503, 429):
                            last_err = f"ШІ-модель тимчасово перевантажена (HTTP {resp.status}), спробуйте ще раз."
                            break
                        raise RuntimeError(f"Помилка ШІ-сервісу (HTTP {resp.status})")
                except asyncio.TimeoutError:
                    logging.warning(f"Gemini {model_name} timeout (attempt {attempt + 1})")
                    last_err = "ШІ-сервіс не відповів вчасно (таймаут). Спробуйте ще раз."
                    if attempt == 0:
                        continue
                    break
            if data is not None:
                break

    if data is None:
        raise RuntimeError(last_err)

    try:
        text_out = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text_out)
    except Exception as e:
        logging.error(f"Gemini parse error: {e} | raw={data}")
        raise RuntimeError("Не вдалося обробити відповідь ШІ")

    if parsed.get("error"):
        raise ValueError(str(parsed["error"]))

    total = float(parsed.get("total_amount") or 0)
    if total <= 0:
        raise ValueError("Не вдалося визначити суму чеку")

    return {
        "merchant": str(parsed.get("merchant") or "Чек").strip(),
        "total_amount": round(total, 2),
        "currency": str(parsed.get("currency") or "UAH").strip().upper(),
        "date": parsed.get("date"),
        "category_hint": str(parsed.get("category_hint") or "Інше").strip(),
        "items_summary": str(parsed.get("items_summary") or "").strip(),
    }


async def parse_receipt_bytes(raw_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return await parse_receipt_image(b64, mime_type=mime_type)


async def match_category_id(category_hint: str) -> tuple[str | None, str]:
    cats = await run_db(
        lambda: supabase.table("categories").select("id, name, icon, type").eq("type", "expense").execute().data or []
    )
    hint = category_hint.lower()
    for c in cats:
        cname = str(c.get("name") or "").lower()
        if cname and (cname in hint or hint in cname):
            return c["id"], f"{c.get('icon', '🏷️')} {c['name']}"
    return None, "⚪ Інше"
