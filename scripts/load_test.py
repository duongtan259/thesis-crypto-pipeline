"""
Load test script — pumps synthetic crypto events at configurable rates
to stress-test the pipeline throughput and measure Fabric's limits.

Usage:
    # Test at 500 events/sec for 2 minutes, publishing to local Kafka
    python scripts/load_test.py --eps 500 --duration 120 --target kafka

    # Test at 1000 events/sec, publishing to Azure Event Hub
    python scripts/load_test.py --eps 1000 --duration 60 --target eventhub

Results are written to: scripts/results/load_test_<timestamp>.json
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent.parent / "generator"))

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
           "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "MATIC-USD"]

BASE_PRICES = {
    "BTC-USD": 70000, "ETH-USD": 2100, "SOL-USD": 90,
    "BNB-USD": 650,   "XRP-USD": 1.4,  "ADA-USD": 0.45,
    "DOGE-USD": 0.12, "AVAX-USD": 35,  "DOT-USD": 7.5, "MATIC-USD": 0.9,
}


def make_event(sequence: int) -> bytes:
    symbol = random.choice(SYMBOLS)
    base = BASE_PRICES[symbol]
    price = base * (1 + random.gauss(0, 0.001))  # ±0.1% noise
    event = {
        "event_id":      str(uuid4()),
        "symbol":        symbol,
        "price":         round(price, 6),
        "volume_24h":    round(random.uniform(1000, 1_000_000), 2),
        "market_cap":    round(price * random.uniform(1e9, 1e12), 2),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "source":        "load_test",
        "sequence":      sequence,
        "ingestion_time": datetime.now(timezone.utc).isoformat(),
        "raw_payload":   "{}",
    }
    return json.dumps(event).encode("utf-8")


async def run_load_test(eps: int, duration: int, target: str, batch_size: int = 100):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")

    print(f"\n{'='*55}")
    print(f"  Load Test: {eps} events/sec for {duration}s → {target}")
    print(f"{'='*55}\n")

    if target == "kafka":
        from publisher.kafka import KafkaPublisher
        bootstrap = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")
        topic     = os.getenv("KAFKA_TOPIC", "crypto-prices")
        publisher_ctx = KafkaPublisher(bootstrap, topic)
    else:
        from publisher.eventhub import EventHubPublisher
        conn = os.getenv("EVENTHUB_CONNECTION_STRING", "")
        name = os.getenv("EVENTHUB_NAME", "crypto-prices")
        if not conn:
            print("ERROR: EVENTHUB_CONNECTION_STRING not set in .env")
            sys.exit(1)
        publisher_ctx = EventHubPublisher(conn, name)

    interval   = 1.0 / eps
    sequence   = 0
    sent_total = 0
    errors     = 0
    checkpoints = []  # (elapsed, sent, eps_actual)
    start = time.monotonic()

    async with publisher_ctx as publisher:
        buffer = []
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break

            sequence += 1
            buffer.append(make_event(sequence))

            if len(buffer) >= batch_size:
                t0 = time.monotonic()
                try:
                    if target == "kafka":
                        for b in buffer:
                            await publisher._producer.send(publisher._topic, b)
                        publisher._sent_total += len(buffer)
                    else:
                        from azure.eventhub import EventData
                        batch = await publisher._client.create_batch()
                        for b in buffer:
                            try:
                                batch.add(EventData(b))
                            except ValueError:
                                await publisher._client.send_batch(batch)
                                batch = await publisher._client.create_batch()
                                batch.add(EventData(b))
                        if len(batch):
                            await publisher._client.send_batch(batch)
                    sent_total += len(buffer)
                except Exception as e:
                    errors += len(buffer)
                    print(f"  Send error: {e}")
                buffer.clear()

                send_ms  = (time.monotonic() - t0) * 1000
                eps_real = sent_total / elapsed if elapsed > 0 else 0
                checkpoints.append({
                    "elapsed_s":  round(elapsed, 1),
                    "sent":       sent_total,
                    "eps_actual": round(eps_real, 1),
                    "send_ms":    round(send_ms, 1),
                    "errors":     errors,
                })
                print(f"  t={elapsed:5.1f}s | sent={sent_total:>7,} | "
                      f"eps={eps_real:6.1f} | send={send_ms:5.1f}ms | errors={errors}")

            await asyncio.sleep(interval)

    elapsed_total = time.monotonic() - start
    eps_final     = sent_total / elapsed_total

    result = {
        "test_config": {"target_eps": eps, "duration_s": duration, "target": target, "batch_size": batch_size},
        "summary": {
            "sent_total":    sent_total,
            "errors":        errors,
            "elapsed_s":     round(elapsed_total, 2),
            "eps_achieved":  round(eps_final, 1),
            "success_rate":  round((sent_total / (sent_total + errors)) * 100, 2) if (sent_total + errors) > 0 else 0,
        },
        "checkpoints": checkpoints,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    print(f"\n{'='*55}")
    print(f"  RESULT")
    print(f"  Target:    {eps} eps")
    print(f"  Achieved:  {eps_final:.1f} eps")
    print(f"  Sent:      {sent_total:,} events")
    print(f"  Errors:    {errors}")
    print(f"  Duration:  {elapsed_total:.1f}s")
    print(f"{'='*55}\n")

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"load_test_{eps}eps_{ts}.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"Results saved to: {out}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Pipeline load tester")
    parser.add_argument("--eps",      type=int,   default=100,     help="Target events per second")
    parser.add_argument("--duration", type=int,   default=60,      help="Test duration in seconds")
    parser.add_argument("--target",   type=str,   default="kafka", choices=["kafka", "eventhub"])
    parser.add_argument("--batch",    type=int,   default=100,     help="Batch size")
    args = parser.parse_args()
    asyncio.run(run_load_test(args.eps, args.duration, args.target, args.batch))


if __name__ == "__main__":
    main()
