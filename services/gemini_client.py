import asyncio
import json
import logging
from typing import Any
import aiohttp

from config import GEMINI_API_KEY

MODELS_FALLBACK = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]


async def call_gemini_parts(
    parts: list[dict[str, Any]],
    response_mime_type: str = "application/json",
    temperature: float = 0.2,
) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY не налаштовано на сервері.")

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": response_mime_type, "temperature": temperature},
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
                        logging.warning(f"Gemini {model_name} HTTP {resp.status}: {body_text[:200]}")
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

    return data["candidates"][0]["content"]["parts"][0]["text"]
