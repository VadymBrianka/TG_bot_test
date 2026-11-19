import logging
import aiohttp
from db import supabase, run_db


async def update_rates_from_nbu() -> None:
    try:
        url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    records = [{"currency": "UAH", "rate_to_base": 1.0}]
                    for item in data:
                        cc = str(item.get("cc")).strip().upper()
                        if cc and cc != "USDT":
                            rate = float(item.get("rate", 1.0))
                            records.append({"currency": cc, "rate_to_base": rate})
                    await run_db(
                        lambda: supabase.table("exchange_rates").upsert(records, on_conflict="currency").execute()
                    )
                    logging.info(f"Оновлено {len(records)} курсів валют НБУ.")
    except Exception as e:
        logging.error(f"Помилка оновлення курсів НБУ: {e}")
