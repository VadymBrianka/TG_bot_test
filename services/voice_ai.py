import base64
import json
from typing import Any

from services.gemini_client import call_gemini_parts

VOICE_PROMPT = """Ти — фінансовий асистент. Прослухай голосове повідомлення користувача українською/російською/англійською і витягни всі згадані фінансові операції (витрати або доходи).
Поверни ТІЛЬКИ JSON-масив об'єктів, де кожен об'єкт має поля:
- "type": "expense" або "income"
- "amount": число float > 0
- "currency": 3-літерний код ("UAH", "USD", "EUR", "USDT", "PLN"; за замовчуванням "UAH")
- "note": короткий опис (до 80 символів)
- "category_hint": одна з категорій ("Продукти", "Кафе", "Транспорт", "Здоров'я", "Одяг", "Комунальні", "Розваги", "Зарплата", "Інше")

Якщо транзакцій не знайдено, поверни []."""


async def parse_voice_transactions(audio_bytes: bytes, mime_type: str = "audio/ogg") -> list[dict[str, Any]]:
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    raw = await call_gemini_parts(
        [
            {"text": VOICE_PROMPT},
            {"inline_data": {"mime_type": mime_type, "data": b64}},
        ],
        response_mime_type="application/json",
        temperature=0.1,
    )
    items = json.loads(raw)
    if not isinstance(items, list):
        return []
    result = []
    for it in items:
        amt = float(it.get("amount") or 0)
        if amt <= 0:
            continue
        tx_type = "income" if it.get("type") == "income" else "expense"
        result.append(
            {
                "type": tx_type,
                "amount": round(amt, 2),
                "currency": str(it.get("currency") or "UAH").strip().upper(),
                "note": str(it.get("note") or ("Дохід" if tx_type == "income" else "Витрата")).strip(),
                "category_hint": str(it.get("category_hint") or "Інше").strip(),
            }
        )
    return result
