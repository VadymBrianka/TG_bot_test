import logging
import aiohttp
from aiohttp import web
from services.market_quotes import DEFAULT_HEADERS, get_realtime_price


async def handle_market_search(request: web.Request):
    query = request.query.get("q", "").strip().lower()
    asset_class = request.query.get("type", "stock").strip().lower()
    if not query:
        return web.json_response({"results": []})

    results = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
            if asset_class == "crypto":
                url = f"https://api.coingecko.com/api/v3/search?query={query}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        cdata = await resp.json()
                        for c in cdata.get("coins", [])[:8]:
                            results.append(
                                {
                                    "ticker": c.get("symbol", "").upper(),
                                    "name": c.get("name", ""),
                                    "currency": "USD",
                                    "price": 0.0,
                                }
                            )
                        if results:
                            return web.json_response({"results": results})

                common = [
                    {"ticker": "BTC", "name": "Bitcoin"},
                    {"ticker": "ETH", "name": "Ethereum"},
                    {"ticker": "SOL", "name": "Solana"},
                    {"ticker": "TON", "name": "Toncoin"},
                    {"ticker": "XRP", "name": "Ripple"},
                    {"ticker": "BNB", "name": "BNB"},
                    {"ticker": "DOGE", "name": "Dogecoin"},
                    {"ticker": "ADA", "name": "Cardano"},
                ]
                results = [c for c in common if query in c["ticker"].lower() or query in c["name"].lower()]
                return web.json_response({"results": results})

            elif asset_class == "stock":
                url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=8&newsCount=0"
                async with session.get(url, headers=DEFAULT_HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for q in data.get("quotes", []):
                            if q.get("quoteType") in ["EQUITY", "ETF"]:
                                results.append(
                                    {
                                        "ticker": q.get("symbol"),
                                        "name": q.get("shortname") or q.get("longname") or q.get("symbol"),
                                        "currency": "USD",
                                        "price": 0.0,
                                    }
                                )
                        return web.json_response({"results": results})
    except Exception as e:
        logging.warning(f"Market search warning: {e}")

    return web.json_response({"results": results})


async def handle_market_quote(request: web.Request):
    symbol = request.query.get("symbol", "").strip().upper()
    asset_class = request.query.get("type", "stock").strip().lower()
    if not symbol:
        return web.json_response({"error": "Symbol is required"}, status=400)

    price = await get_realtime_price(symbol, asset_class, use_cache=True)
    if price and price > 0:
        return web.json_response({"symbol": symbol, "price": price, "currency": "USD"})
    return web.json_response({"error": "Ціну не знайдено на біржах. Введіть вручну."}, status=404)
