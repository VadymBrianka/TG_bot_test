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


TTS_MODELS_FALLBACK = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]


async def gemini_tts_wav_b64(
    text: str,
    voice: str = "Kore",
    style: str = "warm, clear, friendly Ukrainian financial advisor",
) -> str | None:
    """Synthesize spoken WAV audio (base64) via Gemini Interactions TTS API; returns None on fallback."""
    clean_text = (text or "").strip()
    if not GEMINI_API_KEY or not clean_text:
        return None
    # Keep TTS concise for fast mobile playback
    if len(clean_text) > 1200:
        clean_text = clean_text[:1200]

    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        for model_name in TTS_MODELS_FALLBACK:
            payload = {
                "model": model_name,
                "input": [
                    {
                        "type": "user_input",
                        "content": [
                            {
                                "type": "text",
                                "text": clean_text,
                                "annotations": [{"type": "speech_metadata", "style": style}],
                            }
                        ],
                    }
                ],
                "response_format": {"type": "audio"},
                "generation_config": {"speech_config": [{"voice": voice}]},
            }
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body_preview = (await resp.text())[:200]
                        logging.warning(f"Gemini TTS {model_name} HTTP {resp.status}: {body_preview}")
                        continue
                    data = await resp.json()
                    for step in data.get("steps") or []:
                        for item in step.get("content") or []:
                            if item.get("type") == "audio" and item.get("data"):
                                return str(item["data"])
                    for out in data.get("outputs") or []:
                        if out.get("type") == "audio" and out.get("data"):
                            return str(out["data"])
            except Exception as e:
                logging.warning(f"Gemini TTS {model_name} err: {e}")
    return None
