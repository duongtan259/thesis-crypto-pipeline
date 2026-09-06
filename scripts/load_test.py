"""Broker-acknowledged synthetic load test for Kafka or Azure Event Hubs.

The scheduler follows absolute monotonic deadlines. Producing an event and waiting
for broker acknowledgement therefore do not get added to every pacing interval.
The result distinguishes scheduled, generated, and broker-acknowledged events;
acknowledgement is not presented as proof of downstream losslessness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "generator"))

from models.price_event import PriceEvent

SYMBOLS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "BNB-USD",
    "XRP-USD",
    "ADA-USD",
    "DOGE-USD",
    "AVAX-USD",
    "DOT-USD",
    "MATIC-USD",
]

BASE_PRICES = {
    "BTC-USD": 70_000,
    "ETH-USD": 2_100,
    "SOL-USD": 90,
    "BNB-USD": 650,
    "XRP-USD": 1.4,
    "ADA-USD": 0.45,
    "DOGE-USD": 0.12,
    "AVAX-USD": 35,
    "DOT-USD": 7.5,
    "MATIC-USD": 0.9,
}

_STOP = object()


def make_event(sequence: int) -> PriceEvent:
    """Create one schema-valid synthetic ticker event."""
    symbol = random.choice(SYMBOLS)
    base = BASE_PRICES[symbol]
    price = base * (1 + random.gauss(0, 0.001))
    return PriceEvent(
        symbol=symbol,
        price=round(price, 6),
        volume_24h=round(random.uniform(1_000, 1_000_000), 2),
        market_cap=round(price * random.uniform(1e9, 1e12), 2),
        timestamp_utc=datetime.now(timezone.utc),
        source="load_test",
        sequence=sequence,
    )


def scheduled_deadline(start: float, sequence: int, eps: float) -> float:
    """Return the absolute deadline for a zero-based event sequence."""
    if eps <= 0:
        raise ValueError("eps must be greater than zero")
    return start + sequence / eps


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise_latencies(values_ms: list[float]) -> dict[str, float | int | None]:
    """Summarise per-batch acknowledgement durations."""
    if not values_ms:
        return {
            "count": 0,
            "mean_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(values_ms),
        "mean_ms": round(sum(values_ms) / len(values_ms), 3),
        "p50_ms": round(_nearest_rank(values_ms, 50) or 0.0, 3),
        "p95_ms": round(_nearest_rank(values_ms, 95) or 0.0, 3),
        "p99_ms": round(_nearest_rank(values_ms, 99) or 0.0, 3),
        "max_ms": round(max(values_ms), 3),
    }


def build_result(
    *,
    eps: float,
    requested_duration_s: float,
    target: str,
    batch_size: int,
    queue_size: int,
    scheduled: int,
    generated: int,
    acknowledged: int,
    queue_overflows: int,
    errors: int,
    batches: int,
    elapsed_s: float,
    ack_latencies_ms: list[float],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Build a result whose field names encode the supported claims."""
    divisor = elapsed_s if elapsed_s > 0 else 1.0
    attempted = acknowledged + errors
    return {
        "schema_version": 2,
        "test_config": {
            "target_eps": eps,
            "requested_duration_s": requested_duration_s,
            "target": target,
            "batch_size": batch_size,
            "queue_size": queue_size,
            "pacing": "absolute_monotonic_deadlines",
        },
        "counts": {
            "scheduled": scheduled,
            "generated": generated,
            "broker_acknowledged": acknowledged,
            "queue_overflows": queue_overflows,
            "errors": errors,
        },
        "rates_eps": {
            "scheduled": round(scheduled / divisor, 3),
            "generated": round(generated / divisor, 3),
            "broker_acknowledged": round(acknowledged / divisor, 3),
        },
        "batches": batches,
        "batch_acknowledgement_latency": summarise_latencies(ack_latencies_ms),
        "elapsed_s": round(elapsed_s, 6),
        "broker_acceptance_rate_pct": (
            round(acknowledged / attempted * 100, 3) if attempted else None
        ),
        "started_at": started_at,
        "finished_at": finished_at,
        "claims": {
            "broker_acknowledgement_measured": True,
            "proves_downstream_losslessness": False,
        },
    }


def _publisher(target: str):
    if target == "kafka":
        from publisher.kafka import KafkaPublisher

        return KafkaPublisher(
            os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"),
            os.getenv("KAFKA_TOPIC", "crypto-prices"),
        )

    from publisher.eventhub import EventHubPublisher

    connection_string = os.getenv("EVENTHUB_CONNECTION_STRING", "")
    namespace = os.getenv("EVENTHUB_NAMESPACE", "")
    name = os.getenv("EVENTHUB_NAME", "crypto-prices")
    if connection_string:
        return EventHubPublisher(
            eventhub_name=name,
            connection_string=connection_string,
        )
    if namespace:
        return EventHubPublisher(
            eventhub_name=name,
            fully_qualified_namespace=namespace,
        )
    raise RuntimeError(
        "EVENTHUB_CONNECTION_STRING or EVENTHUB_NAMESPACE must be set for eventhub"
    )


async def run_load_test(
    eps: float,
    duration: float,
    target: str,
    batch_size: int = 100,
    queue_size: int = 10_000,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Schedule events independently of broker sending and return measured counts."""
    from dotenv import load_dotenv

    if eps <= 0 or duration <= 0 or batch_size <= 0 or queue_size <= 0:
        raise ValueError("eps, duration, batch_size, and queue_size must be positive")

    load_dotenv(Path(__file__).parent.parent / ".env")
    publisher_context = _publisher(target)
    queue: asyncio.Queue[PriceEvent | object] = asyncio.Queue(maxsize=queue_size)
    counts = {
        "scheduled": 0,
        "generated": 0,
        "acknowledged": 0,
        "queue_overflows": 0,
        "errors": 0,
        "batches": 0,
    }
    ack_latencies_ms: list[float] = []
    total_events = int(eps * duration)
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()

    async def schedule_events() -> None:
        for index in range(total_events):
            wait_s = scheduled_deadline(started, index, eps) - time.monotonic()
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            counts["scheduled"] += 1
            event = make_event(index + 1)
            counts["generated"] += 1
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                counts["queue_overflows"] += 1
        await queue.put(_STOP)

    async def send_events() -> None:
        async with publisher_context as publisher:
            batch: list[PriceEvent] = []

            async def flush() -> None:
                if not batch:
                    return
                size = len(batch)
                before = time.monotonic()
                try:
                    accepted = await publisher.send_batch(batch)
                    counts["acknowledged"] += accepted
                    counts["errors"] += size - accepted
                except Exception as exc:  # noqa: BLE001 - SDK failures become evidence
                    counts["errors"] += size
                    print(f"send error for batch of {size}: {exc}", file=sys.stderr)
                finally:
                    counts["batches"] += 1
                    ack_latencies_ms.append((time.monotonic() - before) * 1000)
                    batch.clear()

            while True:
                item = await queue.get()
                if item is _STOP:
                    await flush()
                    return
                if not isinstance(item, PriceEvent):
                    raise TypeError(f"unexpected queue item: {type(item)!r}")
                batch.append(item)
                if len(batch) >= batch_size:
                    await flush()

    await asyncio.gather(schedule_events(), send_events())
    elapsed = time.monotonic() - started
    finished_wall = datetime.now(timezone.utc)
    result = build_result(
        eps=eps,
        requested_duration_s=duration,
        target=target,
        batch_size=batch_size,
        queue_size=queue_size,
        scheduled=counts["scheduled"],
        generated=counts["generated"],
        acknowledged=counts["acknowledged"],
        queue_overflows=counts["queue_overflows"],
        errors=counts["errors"],
        batches=counts["batches"],
        elapsed_s=elapsed,
        ack_latencies_ms=ack_latencies_ms,
        started_at=started_wall.isoformat(),
        finished_at=finished_wall.isoformat(),
    )

    out_dir = output_dir or Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = finished_wall.strftime("%Y%m%d_%H%M%S_%f")
    out = out_dir / f"load_test_v2_{eps:g}eps_{stamp}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    print(f"Results saved to: {out}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eps", type=float, default=100, help="scheduled events per second"
    )
    parser.add_argument(
        "--duration", type=float, default=60, help="scheduling duration in seconds"
    )
    parser.add_argument("--target", default="kafka", choices=["kafka", "eventhub"])
    parser.add_argument(
        "--batch",
        "--batch-size",
        dest="batch_size",
        type=int,
        default=100,
        help="events per broker send",
    )
    parser.add_argument("--queue-size", type=int, default=10_000)
    args = parser.parse_args()
    asyncio.run(
        run_load_test(
            args.eps,
            args.duration,
            args.target,
            args.batch_size,
            args.queue_size,
        )
    )


if __name__ == "__main__":
    main()
