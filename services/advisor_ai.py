from datetime import datetime, timedelta, timezone
from db import supabase, run_db, get_exchange_rates
from services.gemini_client import call_gemini_parts


async def generate_financial_advice(user_uuid: str) -> str:
    now = datetime.now(timezone.utc)
    d30 = (now - timedelta(days=30)).isoformat()
    d60 = (now - timedelta(days=60)).isoformat()

    def _load():
        txs = (
            supabase.table("transactions")
            .select("amount, type, category_id, account_id, transaction_date")
            .eq("user_id", user_uuid)
            .gte("transaction_date", d60)
            .execute()
            .data
            or []
        )
        accs = supabase.table("accounts").select("id, name, balance, currency").eq("user_id", user_uuid).execute().data or []
        cats = supabase.table("categories").select("id, name").execute().data or []
        goals = supabase.table("goals").select("title, target_amount, current_amount, currency, deadline").eq("user_id", user_uuid).execute().data or []
        return txs, accs, cats, goals

    txs, accs, cats, goals = await run_db(_load)
    rates = await get_exchange_rates()
    acc_curr = {a["id"]: str(a.get("currency") or "UAH").upper() for a in accs}
    cat_map = {c["id"]: c["name"] for c in cats}

    cur_inc = 0.0
    cur_exp = 0.0
    prev_exp = 0.0
    cat_totals: dict[str, float] = {}

    for t in txs:
        uah = float(t.get("amount") or 0) * rates.get(acc_curr.get(t.get("account_id"), "UAH"), 1.0)
        is_cur = (t.get("transaction_date") or "") >= d30
        if t.get("type") == "income" and is_cur:
            cur_inc += uah
        elif t.get("type") == "expense":
            if is_cur:
                cur_exp += uah
                cname = cat_map.get(t.get("category_id"), "Інше")
                cat_totals[cname] = cat_totals.get(cname, 0.0) + uah
            else:
                prev_exp += uah

    liq_uah = sum(float(a.get("balance") or 0) * rates.get(str(a.get("currency") or "UAH").upper(), 1.0) for a in accs)
    top_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:5]
    top_cats_str = ", ".join(f"{k}: {v:,.0f} UAH" for k, v in top_cats) or "немає даних"
    goals_str = ", ".join(
        f"{g['title']} ({float(g['current_amount']):,.0f}/{float(g['target_amount']):,.0f} {g['currency']})"
        for g in goals[:4]
    ) or "немає"

    prompt = f"""Ти — персональний фінансовий радник у FinTrack Hub. Проаналізуй показники користувача за останні 30 днів (у UAH) та дай 3-4 конкретні, практичні поради українською мовою (до 900 символів, з емодзі, без Markdown ** — використовуй лише простий текст або переноси рядків).

Дані користувача:
- Ліквідність на рахунках: {liq_uah:,.0f} UAH
- Доходи за 30 дн.: {cur_inc:,.0f} UAH
- Витрати за 30 дн.: {cur_exp:,.0f} UAH (попередні 30 дн.: {prev_exp:,.0f} UAH)
- Топ категорій витрат: {top_cats_str}
- Цілі накопичення: {goals_str}"""

    return await call_gemini_parts([{"text": prompt}], response_mime_type="text/plain", temperature=0.5)
