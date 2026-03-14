"""
Coinbase Advanced Trade WebSocket source.
Subscribes to the 'ticker' channel and yields PriceEvent objects.
Reconnects automatically on disconnection.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import websockets

from models.price_event import PriceEvent

WS_URL = "wss://advanced-trade-ws.coinbase.com"
logger = logging.getLogger(__name__)

_sequence = 0


async def stream_prices(symbols: list[str]) -> AsyncIterator[PriceEvent]:
    global _sequence

    subscribe_msg = json.dumps({
        "type": "subscribe",
        "product_ids": symbols,
        "channel": "ticker",
    })

    backoff = 5  # seconds; doubles on each failure, capped at 60s
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(subscribe_msg)
                logger.info("Subscribed to Coinbase ticker | symbols=%s", symbols)
                backoff = 5  # reset on successful connection

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("channel") != "ticker":
                        continue

                    for event in msg.get("events", []):
                        for ticker in event.get("tickers", []):
                            try:
                                _sequence += 1
                                yield PriceEvent(
                                    symbol=ticker["product_id"],
                                    price=float(ticker["price"]),
                                    volume_24h=float(ticker.get("volume_24_h", 0)),
                                    market_cap=0.0,
                                    timestamp_utc=datetime.now(timezone.utc),
                                    source="coinbase_ws",
                                    sequence=_sequence,
                                )
                            except (KeyError, ValueError, TypeError) as e:
                                logger.warning("Skipping malformed ticker: %s | raw=%s", e, ticker)

        except (websockets.WebSocketException, OSError, asyncio.TimeoutError) as e:
            logger.error("WebSocket error: %s — reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            logger.exception("Unexpected error in WebSocket stream: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
