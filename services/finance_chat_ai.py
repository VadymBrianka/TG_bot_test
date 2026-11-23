import json
from datetime import datetime, timedelta, timezone
from typing import Any
from db import supabase, run_db, get_exchange_rates
from services.gemini_client import call_gemini_parts


async def answer_finance_question(user_uuid: str, question: str) -> str:
    q_text = (question or "").strip()
    if not q_text:
        raise ValueError("Поставте питання про ваші фінанси.")

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

    ctx = await run_db(_gather)

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
