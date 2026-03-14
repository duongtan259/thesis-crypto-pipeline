"""
CoinGecko REST polling fallback.
Polls the /coins/markets endpoint on an interval and yields PriceEvent objects.
Use this when WebSocket is unavailable or for additional pairs.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import aiohttp

from models.price_event import PriceEvent

BASE_URL = "https://api.coingecko.com/api/v3"
logger = logging.getLogger(__name__)

# Map Coinbase symbol to CoinGecko coin id
SYMBOL_TO_ID = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "BNB-USD": "binancecoin",
    "XRP-USD": "ripple",
    "ADA-USD": "cardano",
    "DOGE-USD": "dogecoin",
    "AVAX-USD": "avalanche-2",
    "DOT-USD": "polkadot",
    "MATIC-USD": "matic-network",
}

_sequence = 0


async def stream_prices(symbols: list[str], poll_interval: float = 10.0) -> AsyncIterator[PriceEvent]:
    global _sequence

    coin_ids = [SYMBOL_TO_ID.get(s, s.split("-")[0].lower()) for s in symbols]
    symbol_map = {SYMBOL_TO_ID.get(s, s.split("-")[0].lower()): s for s in symbols}

    params = {
        "vs_currency": "usd",
        "ids": ",".join(coin_ids),
        "order": "market_cap_desc",
        "per_page": len(coin_ids),
        "page": 1,
        "sparkline": "false",
    }

    backoff = 60  # seconds; doubles on rate limit, resets on success, capped at 300s
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{BASE_URL}/coins/markets",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        logger.warning("CoinGecko rate limited — backing off %ds", backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 300)
                        continue

                    resp.raise_for_status()
                    data = await resp.json()
                    backoff = 60  # reset on success

                    for coin in data:
                        _sequence += 1
                        symbol = symbol_map.get(coin["id"], f"{coin['symbol'].upper()}-USD")
                        yield PriceEvent(
                            symbol=symbol,
                            price=float(coin.get("current_price") or 0),
                            volume_24h=float(coin.get("total_volume") or 0),
                            market_cap=float(coin.get("market_cap") or 0),
                            timestamp_utc=datetime.now(timezone.utc),
                            source="coingecko_rest",
                            sequence=_sequence,
                        )

                    logger.info("Polled CoinGecko: %d coins", len(data))

            except aiohttp.ClientError as e:
                logger.error("CoinGecko request failed: %s", e)

            await asyncio.sleep(poll_interval)
