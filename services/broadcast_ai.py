import asyncio
import json
import logging
from typing import Any
import aiohttp

from config import GEMINI_API_KEY

MODELS_FALLBACK = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]


async def generate_broadcast_draft(topic: str, tone: str = "дружній") -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise ValueError("ШІ-генератор не налаштовано: додайте GEMINI_API_KEY у змінні середовища сервера.")

    prompt = f"""Ти — копірайтер і маркетинг-агент фінансового Telegram-бота FinTrack Hub (облік витрат, рахунки, цілі, інвестиції, борги, ШІ-сканер чеків).
Напиши коротку, яскраву та корисну статтю/оголошення для розсилки користувачам у Telegram українською мовою.

Тема / ідея від адміністратора: "{topic}"
Тон тексту: {tone}

Вимоги:
1. Використовуй валідні Telegram HTML-теги (<b>, <i>, <code>) — БЕЗ Markdown (** або ##) і без <br>/<p>.
2. Додай доречні емодзі, чітку структуру (заголовок, 2-3 коротких абзаци або буліти, заклик до дії).
3. Сформуй текст для inline-кнопки (до 28 символів) та визнач, чи варто прикріпити кнопку відкриття Mini App ("attach_miniapp_btn": true/false), або зовнішній URL ("button_url", якщо в темі вказане посилання, інакше "").

Поверни ТІЛЬКИ JSON-об'єкт з полями:
- "text": рядок з HTML-текстом розсилки
- "button_text": текст кнопки (рядок)
- "button_url": URL або ""
- "attach_miniapp_btn": boolean"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
    }

    data = None
    last_err = "ШІ-сервіс тимчасово недоступний, спробуйте ще раз."
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
                        logging.warning(f"Broadcast AI {model_name} HTTP {resp.status}: {body_text[:200]}")
                        if resp.status in (503, 429) and attempt == 0:
                            await asyncio.sleep(1.5)
                            continue
                        if resp.status in (404, 503, 429):
                            last_err = f"ШІ-модель тимчасово недоступна (HTTP {resp.status})"
                            break
                        raise RuntimeError(f"Помилка ШІ-сервісу (HTTP {resp.status})")
                except asyncio.TimeoutError:
                    last_err = "ШІ-сервіс не відповів вчасно (таймаут)"
                    if attempt == 0:
                        continue
                    break
            if data is not None:
                break

    if data is None:
        raise RuntimeError(last_err)

    text_out = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text_out)
    return {
        "text": str(parsed.get("text") or "").strip(),
        "button_text": str(parsed.get("button_text") or "").strip(),
        "button_url": str(parsed.get("button_url") or "").strip(),
        "attach_miniapp_btn": bool(parsed.get("attach_miniapp_btn", True)),
    }
