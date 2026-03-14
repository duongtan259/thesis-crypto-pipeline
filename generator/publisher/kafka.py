"""
Local Kafka publisher — used for offline development before Azure is set up.
Mirrors the EventHubPublisher interface so main.py can swap between them.
"""
import logging
from typing import TYPE_CHECKING

from aiokafka import AIOKafkaProducer

if TYPE_CHECKING:
    from models.price_event import PriceEvent

logger = logging.getLogger(__name__)


class KafkaPublisher:
    def __init__(self, bootstrap_servers: str, topic: str):
        self._bootstrap = bootstrap_servers
        self._topic = topic
        self._producer: AIOKafkaProducer | None = None
        self._sent_total = 0

    async def __aenter__(self):
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            value_serializer=lambda v: v,
        )
        await self._producer.start()
        logger.info("Kafka publisher connected | bootstrap=%s topic=%s", self._bootstrap, self._topic)
        return self

    async def __aexit__(self, *_):
        if self._producer:
            await self._producer.stop()
            logger.info("Kafka publisher closed | total_sent=%d", self._sent_total)

    async def send_batch(self, events: list["PriceEvent"]) -> int:
        if not self._producer:
            raise RuntimeError("Publisher not started — use async context manager")

        for event in events:
            await self._producer.send(self._topic, event.to_json_bytes())

        self._sent_total += len(events)
        return len(events)
