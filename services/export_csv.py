import csv
import io
from datetime import datetime, timedelta, timezone
from db import supabase, run_db


async def build_transactions_csv(user_uuid: str, days: int = 365) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _load():
        txs = (
            supabase.table("transactions")
            .select("transaction_date, type, amount, note, account_id, category_id")
            .eq("user_id", user_uuid)
            .gte("transaction_date", since)
            .order("transaction_date", desc=True)
            .execute()
            .data
            or []
        )
        accs = supabase.table("accounts").select("id, name, currency").eq("user_id", user_uuid).execute().data or []
        cats = supabase.table("categories").select("id, name").execute().data or []
        return txs, {a["id"]: a for a in accs}, {c["id"]: c["name"] for c in cats}

    txs, acc_map, cat_map = await run_db(_load)

    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM for Excel
    writer = csv.writer(buf)
    writer.writerow(["Дата", "Тип", "Сума", "Валюта", "Рахунок", "Категорія", "Нотатка"])
    for t in txs:
        acc = acc_map.get(t.get("account_id"), {})
        writer.writerow(
            [
                t.get("transaction_date", ""),
                t.get("type", ""),
                t.get("amount", 0),
                acc.get("currency", "UAH"),
                acc.get("name", ""),
                cat_map.get(t.get("category_id"), "Інше"),
                t.get("note", ""),
            ]
        )
    return buf.getvalue()
