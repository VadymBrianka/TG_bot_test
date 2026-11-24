import json
from datetime import datetime, timedelta, timezone
from typing import Any
from db import supabase, run_db, get_exchange_rates
from services.gemini_client import call_gemini_parts, gemini_tts_wav_b64


async def _gather_user_finance_context(user_uuid: str) -> dict[str, Any]:

    rates = await get_exchange_rates()

    def _gather() -> dict[str, Any]:
        accs = (
            supabase.table("accounts")
            .select("id, name, account_type, currency, balance")
            .eq("user_id", user_uuid)
            .execute()
            .data
            or []
        )
        since_180 = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        txs = (
            supabase.table("transactions")
            .select("amount, type, note, transaction_date, account_id, categories(name)")
            .eq("user_id", user_uuid)
            .gte("transaction_date", since_180)
            .order("transaction_date", desc=True)
            .limit(250)
            .execute()
            .data
            or []
        )
        goals = (
            supabase.table("goals")
            .select("title, current_amount, target_amount, currency, deadline")
            .eq("user_id", user_uuid)
            .execute()
            .data
            or []
        )
        budgets = (
            supabase.table("budgets")
            .select("monthly_limit, currency, categories(name)")
            .eq("user_id", user_uuid)
            .execute()
            .data
            or []
        )
        try:
            debts = (
                supabase.table("debts")
                .select("direction, person_name, amount, currency, is_settled")
                .eq("user_id", user_uuid)
                .eq("is_settled", False)
                .execute()
                .data
                or []
            )
        except Exception:
            debts = []
        try:
            services = (
                supabase.table("service_subscriptions")
                .select("name, amount, currency, billing_cycle, next_billing_date")
                .eq("user_id", user_uuid)
                .eq("is_active", True)
                .execute()
                .data
                or []
            )
        except Exception:
            services = []

        acc_map = {a["id"]: a for a in accs}
        tx_lines = []
        for t in txs:
            acc = acc_map.get(t.get("account_id"), {})
            curr = str(acc.get("currency") or "UAH").upper()
            amt = float(t.get("amount") or 0)
            uah = round(amt * rates.get(curr, 1.0), 2)
            cat_name = (t.get("categories") or {}).get("name") or "Без категорії"
            dt = str(t.get("transaction_date") or "")[:10]
            note = str(t.get("note") or "")[:80]
            tx_lines.append(f"{dt} | {t.get('type')} | {amt:g} {curr} (~{uah:g} UAH) | {cat_name} | {note}")

        return {
            "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "accounts": [
                {
                    "name": a["name"],
                    "balance": float(a.get("balance") or 0),
                    "currency": a.get("currency", "UAH"),
                    "uah_equiv": round(float(a.get("balance") or 0) * rates.get(str(a.get("currency") or "UAH").upper(), 1.0), 2),
                }
                for a in accs
            ],
            "goals": goals,
            "budgets": budgets,
            "debts": debts,
            "paid_services": services,
            "recent_transactions_last_180d": tx_lines,
        }

    return await run_db(_gather)


async def answer_finance_question(user_uuid: str, question: str) -> str:
    q_text = (question or "").strip()
    if not q_text:
        raise ValueError("Поставте питання про ваші фінанси.")

    ctx = await _gather_user_finance_context(user_uuid)

    prompt = (
        "Ти — персональний ШІ-фінансовий асистент у додатку FinTrack Hub.\n"
        "Відповідай на питання користувача українською мовою, спираючись ТІЛЬКИ на його реальні дані нижче.\n"
        "Якщо рахуєш суму витрат чи доходів — наведи точну цифру та поясни, що враховано (в UAH та за потреби у валюті).\n"
        "Якщо оцінюєш можливість накопичити на ціль — порахуй середньомісячний залишок (доходи мінус витрати) і дай реалістичний прогноз.\n"
        "Відповідай лаконічно (4-10 речень), структуровано, без вигадок.\n\n"
        f"Питання користувача: {q_text}\n\n"
        f"Фінансові дані користувача (JSON):\n{ctx}\n\n"
        'Поверни JSON: {"answer": "текст відповіді"}'
    )

    raw = await call_gemini_parts([{"text": prompt}], response_mime_type="application/json", temperature=0.35)
    res = json.loads(raw)
    ans = str(res.get("answer") or "").strip()
    if not ans:
        raise RuntimeError("ШІ не зміг сформувати відповідь, спробуйте переформулювати питання.")
    return ans


async def answer_finance_voice_dialogue(
    user_uuid: str,
    audio_b64: str | None = None,
    mime_type: str = "audio/webm",
    text_question: str | None = None,
    history: list[dict[str, str]] | None = None,
    speak: bool = True,
) -> dict[str, Any]:
    """Listen to user voice (or read text), answer using live financial context, and synthesize speech."""
    if not audio_b64 and not (text_question or "").strip():
        raise ValueError("Надішліть голосове повідомлення або текстове питання.")

    ctx = await _gather_user_finance_context(user_uuid)

    hist_lines = []
    for h in (history or [])[-6:]:
        role = "Користувач" if h.get("role") == "user" else "Радник"
        msg = str(h.get("text") or "").strip()
        if msg:
            hist_lines.append(f"{role}: {msg}")
    hist_block = ("\nПопередній діалог:\n" + "\n".join(hist_lines) + "\n") if hist_lines else ""

    instruction = (
        "Ти — живий персональний ШІ-фінансовий радник у додатку FinTrack Hub. Ти ведеш голосовий діалог із користувачем.\n"
        "1. Якщо є аудіо — уважно прослухай його й точно розпізнай, що сказав користувач (поле transcript).\n"
        "2. Дай точну, теплу, природну відповідь українською мовою, спираючись ТІЛЬКИ на реальні фінансові дані користувача нижче.\n"
        "3. Відповідь буде озвучена вголос, тому пиши чистою розмовною мовою БЕЗ символів Markdown (** зірочок, # решіток, бектіків) і лаконічно (2–5 речень).\n"
        f"{hist_block}\n"
        f"Фінансові дані користувача (JSON):\n{ctx}\n\n"
        'Поверни ТІЛЬКИ JSON: {"transcript": "що запитав користувач", "answer": "твоя розмовна відповідь"}'
    )

    parts: list[dict[str, Any]] = [{"text": instruction}]
    if audio_b64:
        clean_b64 = audio_b64.split(",", 1)[1] if "," in audio_b64 else audio_b64
        parts.append({"inlineData": {"mimeType": mime_type or "audio/webm", "data": clean_b64}})
    elif text_question:
        parts.append({"text": f"Питання користувача: {text_question.strip()}"})

    raw = await call_gemini_parts(parts, response_mime_type="application/json", temperature=0.35)
    parsed = json.loads(raw)
    transcript = str(parsed.get("transcript") or text_question or "").strip()
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        raise RuntimeError("ШІ не зміг сформувати відповідь, спробуйте ще раз.")

    audio_out_b64 = await gemini_tts_wav_b64(answer) if speak else None
    return {
        "transcript": transcript,
        "answer": answer,
        "audio_base64": audio_out_b64,
        "audio_mime": "audio/wav" if audio_out_b64 else None,
    }
