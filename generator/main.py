"""
Crypto price data generator.
Streams real-time prices from Coinbase WebSocket (or CoinGecko REST fallback)
and publishes events to Azure Event Hub or a local Kafka broker.

Usage:
    python main.py                         # reads config from .env
    USE_LOCAL_KAFKA=true python main.py    # use local Kafka (no Azure needed)
"""
import asyncio
import logging
import sys
import time

import structlog

from config import Settings

# Structured logging setup
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger()


async def run(settings: Settings) -> None:
    # Pick publisher
    if settings.use_local_kafka:
        from publisher.kafka import KafkaPublisher
        publisher_ctx = KafkaPublisher(settings.kafka_bootstrap, settings.kafka_topic)
        log.info("mode=local_kafka", bootstrap=settings.kafka_bootstrap)
    else:
        from publisher.eventhub import EventHubPublisher
        if settings.eventhub_connection_string:
            # Connection string from Key Vault (thesis mode)
            publisher_ctx = EventHubPublisher(
                eventhub_name=settings.eventhub_name,
                connection_string=settings.eventhub_connection_string,
            )
            log.info("mode=azure_eventhub (connection string)", hub=settings.eventhub_name)
        elif settings.eventhub_namespace:
            # Managed Identity / OIDC — no secrets (production mode)
            publisher_ctx = EventHubPublisher(
                eventhub_name=settings.eventhub_name,
                fully_qualified_namespace=settings.eventhub_namespace,
            )
            log.info("mode=azure_eventhub (managed identity)", hub=settings.eventhub_name)
        else:
            log.error("Set EVENTHUB_CONNECTION_STRING (Key Vault) or EVENTHUB_NAMESPACE (Managed Identity), or USE_LOCAL_KAFKA=true")
            sys.exit(1)

    # Pick source
    if settings.data_source == "coingecko_rest":
        from sources.coingecko_rest import stream_prices
        source = stream_prices(settings.symbol_list, settings.poll_interval)
    elif settings.data_source == "merged":
        from sources.merged import stream_prices
        source = stream_prices(settings.symbol_list, settings.poll_interval)
    else:
        from sources.coinbase_ws import stream_prices
        source = stream_prices(settings.symbol_list)

    log.info("starting", symbols=settings.symbol_list, source=settings.data_source)

    buffer = []
    sent_total = 0
    start_time = time.monotonic()

    async with publisher_ctx as publisher:
        async for event in source:
            buffer.append(event)

            if len(buffer) >= settings.batch_size:
                n = await publisher.send_batch(buffer)
                sent_total += n
                buffer.clear()

                elapsed = time.monotonic() - start_time
                eps = sent_total / elapsed if elapsed > 0 else 0
                log.info("batch_sent", total=sent_total, eps=round(eps, 1))

            # Optional rate limiting
            if settings.max_events_per_second > 0:
                await asyncio.sleep(1.0 / settings.max_events_per_second)

        # Flush remaining events on clean shutdown
        if buffer:
            await publisher.send_batch(buffer)
            sent_total += len(buffer)
            log.info("flushed_remaining", count=len(buffer), total=sent_total)


def main():
    settings = Settings()
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
