"""
Coinbase Advanced Trade WebSocket source.
Subscribes to the 'ticker' channel and yields PriceEvent objects.
Reconnects automatically on disconnection.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

import websockets
from models.price_event import PriceEvent

WS_URL = "wss://advanced-trade-ws.coinbase.com"
logger = logging.getLogger(__name__)

_sequence = 0


def _parse_coinbase_time(value: str) -> datetime:
    """Parse Coinbase server timestamp (ISO 8601, may carry 'Z' and nanoseconds).

    datetime.fromisoformat on Python < 3.11 rejects 'Z' and >6 fractional
    digits, so normalise to microsecond precision with an explicit UTC offset.
    """
    s = value.strip().replace("Z", "+00:00")
    # Truncate fractional seconds to 6 digits (microseconds) if longer.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return datetime.fromisoformat(s)


def _event_time_or_now(
    value: str | None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> datetime:
    """Return a parsed exchange timestamp or an explicit UTC fallback."""
    if value:
        try:
            return _parse_coinbase_time(value)
        except (TypeError, ValueError):
            pass
    return now()


async def stream_prices(symbols: list[str]) -> AsyncIterator[PriceEvent]:
    global _sequence

    subscribe_msg = json.dumps(
        {
            "type": "subscribe",
            "product_ids": symbols,
            "channel": "ticker",
        }
    )

    backoff = 5  # seconds; doubles on each failure, capped at 60s
    while True:
        try:
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=10
            ) as ws:
                await ws.send(subscribe_msg)
                logger.info("Subscribed to Coinbase ticker | symbols=%s", symbols)
                backoff = 5  # reset on successful connection

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("channel") != "ticker":
                        continue

                    # Coinbase stamps each message envelope with its server send
                    # time. Use it as the event time so latency_ms measures real
                    # exchange-to-generator transit, not just generator buffering.
                    event_time = _event_time_or_now(msg.get("timestamp"))

                    for event in msg.get("events", []):
                        for ticker in event.get("tickers", []):
                            try:
                                _sequence += 1
                                yield PriceEvent(
                                    symbol=ticker["product_id"],
                                    price=float(ticker["price"]),
                                    volume_24h=float(ticker.get("volume_24_h", 0)),
                                    market_cap=0.0,
                                    timestamp_utc=event_time,
                                    source="coinbase_ws",
                                    sequence=_sequence,
                                )
                            except (KeyError, ValueError, TypeError) as e:
                                logger.warning(
                                    "Skipping malformed ticker: %s | raw=%s", e, ticker
                                )

        except (websockets.WebSocketException, OSError, asyncio.TimeoutError) as e:
            logger.error("WebSocket error: %s — reconnecting in %ds", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception:
            logger.exception("Unexpected error in WebSocket stream")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
