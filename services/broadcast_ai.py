import asyncio
import json
import logging
from typing import Any
import aiohttp

from config import GEMINI_API_KEY
from db import supabase, run_db
from services.subscriptions import PLANS

MODELS_FALLBACK = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash"]

RECENT_UPDATES = [
    "Великий редизайн UI/UX Mini App: сучасна фінтех-палітра, нижня панель навігації (Bottom Tab Bar) із центральною кнопкою «+» для швидкого запису з будь-якого екрана, меню «Ще», акордеони в Налаштуваннях, автоматичне приховування нижнього меню при відкритій клавіатурі та плавне Skeleton-завантаження.",
    "Безпечне та зручне редагування записів: закриття вікна редагування натисканням поза ним або кнопкою ✕ зверху, розділені кнопки «Зберегти зміни» та 2-крокове підтвердження видалення.",
    "Режим приватності (👁️) на головному екрані: приховує баланси рахунків, Net Worth, суми в структурі капіталу та транзакціях одним дотиком.",
    "Головний екран: горизонтальна карусель рахунків та групування транзакцій по днях («Сьогодні», «Вчора») із підсумком витрат за день.",
    "Кастомні категорії та правила автокатегоризації (Pro+): створюйте власні категорії та правила (напр. Glovo -> Доставка їжі), щоб бот визначав категорію автоматично.",
    "Пошук, фільтрація транзакцій та #теги (Free/Pro): фільтр за рахунком, нотаткою або #тегами (напр. #відпустка) з миттєвим підрахунком суми.",
    "ШІ-чат із фінансами (Pro+): ставте будь-які питання про свої витрати й доходи через /ask, знаком «?» у чаті або прямо у вкладці Аналітика.",
    "Платні сервіси та підписки під контролем (Pro+): трекер Netflix, Spotify, iCloud тощо з автоматичним нагадуванням за 2 дні до списання.",
    "Промокоди на знижку або безкоштовний тріал: активація через /promo або у вкладці Підписка.",
    "Голосовий швидкий ввід (Pro+): надсилай голосове повідомлення в чат бота, а ШІ автоматично розпізнає суму, валюту та категорію.",
    "ШІ-фінансовий радник (Pro+): команда /advice та персональний аналіз витрат у вкладці Аналітика Mini App.",
    "Календар грошових потоків (Pro+): візуалізація доходів і витрат по днях у Mini App.",
    "Оновлення VIP Демо-трейдинг бота: вибір профілю ризику (Conservative / Balanced / Aggressive), графік кривої капіталу (Equity Curve) та VIP Лідерборд.",
    "Сімейний / спільний капітал (VIP створює, Pro+ приєднується): об'єднання рахунків сім'ї з копіюванням коду запрошення.",
    "Оновлений ШІ-сканер чеків (Pro+): надійні кнопки Камера, Галерея та Документ/PDF для iPhone і Android.",
    "Підписки Pro та VIP: оплата в Telegram Stars або криптовалютою (USDT/USDC/TON) з автоперевіркою в блокчейні та миттєвим копіюванням адреси гаманця.",
]


async def _get_sent_broadcasts_summary() -> str:
    try:
        rows = await run_db(
            lambda: supabase.table("broadcast_logs")
            .select("message_text, target_filter, created_at")
            .order("created_at", desc=True)
            .limit(8)
            .execute()
            .data
            or []
        )
        if not rows:
            return "Раніше розсилки ще не надсилалися."
        lines = []
        for r in rows:
            dt = str(r.get("created_at") or "")[:10]
            txt = str(r.get("message_text") or "").replace("\n", " ")[:180]
            lines.append(f"- [{dt}] ({r.get('target_filter', 'all')}): {txt}")
        return "\n".join(lines)
    except Exception:
        return "Історія розсилок недоступна."


async def _build_product_context() -> str:
    plans_lines = []
    for key in ("free", "pro", "vip"):
        p = PLANS[key]
        feats = "; ".join(p["features"])
        plans_lines.append(f"- {p['name']} (${p['usd_month']}/міс або {p['stars_month']} Stars): {feats}")
    updates_lines = "\n".join(f"- {u}" for u in RECENT_UPDATES)
    sent_history = await _get_sent_broadcasts_summary()
    return (
        "Актуальні тарифи та функції FinTrack Hub:\n"
        + "\n".join(plans_lines)
        + "\n\nУсі останні нововведення в боті, бекенді та UI/UX дизайні:\n"
        + updates_lines
        + "\n\nОстанні вже надіслані розсилки (якщо адміністратор просить розказати про нові/ще не анонсовані зміни — НЕ повторюй те, що вже було в цих розсилках, а обери те, про що ще не писали):\n"
        + sent_history
    )


async def generate_broadcast_draft(topic: str, tone: str = "дружній") -> dict[str, Any]:
    if not GEMINI_API_KEY:
        raise ValueError("ШІ-генератор не налаштовано: додайте GEMINI_API_KEY у змінні середовища сервера.")

    product_context = await _build_product_context()
    prompt = f"""Ти — копірайтер і маркетинг-агент фінансового Telegram-бота FinTrack Hub.
Напиши коротку, яскраву та корисну статтю/оголошення для розсилки користувачам у Telegram українською мовою.

{product_context}

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
