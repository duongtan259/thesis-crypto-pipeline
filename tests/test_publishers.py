import asyncio

import pytest
from models.price_event import PriceEvent
from publisher.kafka import KafkaPublisher


class FakeProducer:
    def __init__(self, fail_on: int | None = None) -> None:
        self.fail_on = fail_on
        self.acknowledged: list[tuple[str, bytes]] = []

    async def send_and_wait(self, topic: str, payload: bytes) -> None:
        if self.fail_on == len(self.acknowledged):
            raise RuntimeError("broker rejected event")
        self.acknowledged.append((topic, payload))


def event(sequence: int) -> PriceEvent:
    from datetime import datetime, timezone

    return PriceEvent(
        symbol="BTC-USD",
        price=70_000.0,
        volume_24h=12_345.0,
        timestamp_utc=datetime.now(timezone.utc),
        source="test",
        sequence=sequence,
    )


def test_kafka_batch_count_increases_only_after_broker_acknowledgements() -> None:
    publisher = KafkaPublisher("unused:9092", "prices")
    fake = FakeProducer()
    publisher._producer = fake

    sent = asyncio.run(publisher.send_batch([event(1), event(2)]))

    assert sent == 2
    assert publisher._sent_total == 2
    assert len(fake.acknowledged) == 2


def test_kafka_batch_does_not_count_rejected_event() -> None:
    publisher = KafkaPublisher("unused:9092", "prices")
    fake = FakeProducer(fail_on=1)
    publisher._producer = fake

    with pytest.raises(RuntimeError, match="broker rejected"):
        asyncio.run(publisher.send_batch([event(1), event(2)]))

    assert publisher._sent_total == 0
    assert len(fake.acknowledged) == 1
