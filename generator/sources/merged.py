"""
Merged source: Coinbase WebSocket (real-time ticks) enriched with
CoinGecko REST market cap data (polled every 30s).

This demonstrates a multi-source pipeline pattern — a common
enterprise use case where a high-frequency stream is enriched
with lower-frequency reference data.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import aiohttp

from models.price_event import PriceEvent
from sources.coinbase_ws import stream_prices as coinbase_stream

logger = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

SYMBOL_TO_ID = {
    "BTC-USD": "bitcoin",     "ETH-USD": "ethereum",
    "SOL-USD": "solana",      "BNB-USD": "binancecoin",
    "XRP-USD": "ripple",      "ADA-USD": "cardano",
    "DOGE-USD": "dogecoin",   "AVAX-USD": "avalanche-2",
    "DOT-USD": "polkadot",    "MATIC-USD": "matic-network",
}


async def _poll_market_caps(symbols: list[str], cache: dict, poll_interval: float = 30.0):
    """Background task: polls CoinGecko every 30s and updates the shared cache."""
    coin_ids = [SYMBOL_TO_ID.get(s, s.split("-")[0].lower()) for s in symbols]
    id_to_symbol = {v: k for k, v in SYMBOL_TO_ID.items() if k in symbols}

    params = {
        "vs_currency": "usd",
        "ids": ",".join(coin_ids),
        "order": "market_cap_desc",
        "per_page": len(coin_ids),
        "page": 1,
        "sparkline": "false",
    }

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    COINGECKO_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 429:
                        logger.warning("CoinGecko rate limited — backing off 60s")
                        await asyncio.sleep(60)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    for coin in data:
                        sym = id_to_symbol.get(coin["id"])
                        if sym:
                            cache[sym] = {
                                "market_cap": float(coin.get("market_cap") or 0),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }
                    logger.info("CoinGecko market cap updated for %d symbols", len(data))
            except Exception as e:
                logger.warning("CoinGecko poll failed: %s", e)

            await asyncio.sleep(poll_interval)


async def stream_prices(symbols: list[str], poll_interval: float = 30.0) -> AsyncIterator[PriceEvent]:
    """
    Yields PriceEvent from Coinbase WebSocket, enriched with market cap from CoinGecko.
    CoinGecko is polled in the background every poll_interval seconds.
    """
    market_cap_cache: dict = {}

    # Start CoinGecko polling in background
    poller = asyncio.create_task(
        _poll_market_caps(symbols, market_cap_cache, poll_interval)
    )

    try:
        async for event in coinbase_stream(symbols):
            cap_data = market_cap_cache.get(event.symbol, {})
            event.market_cap = cap_data.get("market_cap", 0.0)
            yield event
    finally:
        poller.cancel()
