import asyncio
import logging
import time
import aiohttp

_PRICE_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def get_cached_price(symbol: str, asset_class: str) -> float | None:
    key = (symbol.strip().upper(), asset_class.strip().lower())
    entry = _PRICE_CACHE.get(key)
    if not entry:
        return None
    price, ts = entry
    if time.monotonic() - ts < CACHE_TTL_SECONDS:
        return price
    _PRICE_CACHE.pop(key, None)
    return None


def set_cached_price(symbol: str, asset_class: str, price: float) -> None:
    key = (symbol.strip().upper(), asset_class.strip().lower())
    _PRICE_CACHE[key] = (price, time.monotonic())


async def get_realtime_price(
    symbol: str,
    asset_class: str,
    session: aiohttp.ClientSession | None = None,
    use_cache: bool = True,
) -> float | None:
    symbol = symbol.strip().upper()
    asset_class = asset_class.strip().lower()
    if not symbol:
        return None

    if use_cache:
        cached = get_cached_price(symbol, asset_class)
        if cached is not None:
            return cached

    async def _fetch(sess: aiohttp.ClientSession) -> float | None:
        if asset_class == "crypto":
            pair = f"{symbol}USDT"
            try:
                async with sess.get(f"https://api.binance.com/api/v3/ticker/price?symbol={pair}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        p = round(float(data["price"]), 4)
                        if p > 0:
                            return p
            except Exception:
                pass
            try:
                async with sess.get(f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("data") and data["data"].get("price"):
                            p = round(float(data["data"]["price"]), 4)
                            if p > 0:
                                return p
            except Exception:
                pass
            try:
                async with sess.get(f"https://api.coincap.io/v2/assets?search={symbol}&limit=1") as resp_cc:
                    if resp_cc.status == 200:
                        data = await resp_cc.json()
                        if data.get("data"):
                            p = round(float(data["data"][0]["priceUsd"]), 4)
                            if p > 0:
                                return p
            except Exception:
                pass
        else:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
                async with sess.get(url, headers=DEFAULT_HEADERS) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("chart", {}).get("result", [])
                        if result:
                            meta = result[0].get("meta", {})
                            price = float(meta.get("regularMarketPrice", 0))
                            if price > 0:
                                return round(price, 4)
            except Exception as e:
                logging.warning(f"Yahoo quote failed for {symbol}: {e}")
        return None

    try:
        if session is not None:
            price = await _fetch(session)
        else:
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as new_sess:
                price = await _fetch(new_sess)
        if price and price > 0:
            set_cached_price(symbol, asset_class, price)
        return price
    except Exception as e:
        logging.warning(f"Price fetch failed for {symbol}: {e}")
        return None


async def fetch_prices_parallel(items: list[dict]) -> dict[str, float]:
    """Fetch prices for multiple investment items concurrently using a shared ClientSession."""
    if not items:
        return {}

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS, timeout=aiohttp.ClientTimeout(total=6)) as session:
        async def _one(item: dict):
            p = await get_realtime_price(item["ticker"], item["asset_class"], session=session, use_cache=True)
            return item["id"], p

        results = await asyncio.gather(*(_one(it) for it in items), return_exceptions=True)

    prices: dict[str, float] = {}
    for res in results:
        if isinstance(res, tuple):
            item_id, p = res
            if p and p > 0:
                prices[item_id] = p
    return prices
