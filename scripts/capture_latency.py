"""
Latency capture — records the generator-side `latency_ms` distribution, and
optionally runs a controlled batch-size experiment on a single event stream.

The generator stamps `ingestion_time` inside PriceEvent.to_json_bytes(), which the
publisher calls when a batch is flushed. This script reproduces that path exactly,
writing the serialised batch to a file instead of to a broker: `latency_ms` is
computed before the sink, so the value is the one a Kafka or Event Hub sink records
(thesis Section 5.1.3).

Passing several --batch-size values runs them against the SAME WebSocket stream, so
every buffer sees identical events and identical arrival timing. Batch size is then
the only variable that differs between the outputs.

Usage:
    python scripts/capture_latency.py --duration 600 --batch-size 50
    python scripts/capture_latency.py --duration 1800 --batch-size 1 --batch-size 10 --batch-size 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "generator"))


class BatchSink:
    """One buffer with its own batch size, writing flushed batches to its own file."""

    def __init__(self, batch_size: int, path: Path):
        self.batch_size = batch_size
        self.path = path
        self.fh = path.open("w")
        self.buffer: list = []
        self.written = 0
        self.batches = 0
        self.final_partial = 0

    def add(self, event) -> None:
        self.buffer.append(event)
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        # to_json_bytes() stamps ingestion_time — this is the measured boundary.
        for event in self.buffer:
            self.fh.write(event.to_json_bytes().decode("utf-8") + "\n")
        self.written += len(self.buffer)
        self.batches += 1
        self.buffer.clear()

    def close(self) -> None:
        # The final buffer is flushed at shutdown rather than when full, so its
        # events carry an unrepresentative wait. Recorded so analysis can drop them.
        self.final_partial = len(self.buffer)
        self.flush()
        self.fh.close()


def rotated_sinks(sinks: list, event_index: int) -> list:
    """Rotate dispatch order so no sink is systematically serialized first."""
    if not sinks:
        return []
    offset = event_index % len(sinks)
    return sinks[offset:] + sinks[:offset]


def arrival_rate(events: int, elapsed_s: float) -> float:
    """Calculate rate from measured wall-clock duration, not requested duration."""
    if elapsed_s <= 0:
        raise ValueError("elapsed_s must be greater than zero")
    return events / elapsed_s


async def capture(
    duration: int, batch_sizes: list[int], symbols: list[str], out_dir: Path
) -> None:
    from sources.coinbase_ws import stream_prices

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    sinks = [
        BatchSink(b, out_dir / f"capture_batch{b}_{stamp}.jsonl") for b in batch_sizes
    ]

    print(
        f"capturing {duration}s from Coinbase | symbols={symbols} | batch sizes={batch_sizes}"
    )
    started = time.monotonic()
    deadline = started + duration
    received = 0

    try:
        async for event in stream_prices(symbols):
            received += 1
            for sink in rotated_sinks(sinks, received - 1):
                sink.add(event)
            if received % 500 == 0:
                left = max(0, deadline - time.monotonic())
                print(
                    f"  received={received}  elapsed={duration - left:.0f}s  remaining={left:.0f}s",
                    flush=True,
                )
            if time.monotonic() >= deadline:
                break
    finally:
        for sink in sinks:
            sink.close()

    elapsed = time.monotonic() - started
    rate = arrival_rate(received, elapsed)
    print(f"\ndone: received={received} events in {elapsed:.3f}s ({rate:.2f} eps)")
    for sink in sinks:
        print(
            f"  batch_size={sink.batch_size:<4} flushed={sink.written:<7} batches={sink.batches:<6} -> {sink.path.name}"
        )

    # A run manifest, so a reader can tie every figure back to the run that made it.
    manifest = {
        "schema_version": 2,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "requested_duration_s": duration,
        "duration_s": round(elapsed, 6),
        "elapsed_s": round(elapsed, 6),
        "symbols": symbols,
        "events_received": received,
        "arrival_rate_eps": round(rate, 3),
        "sink_dispatch": "round-robin rotation per source event",
        "residual_interference": "all sinks share one Python process and event loop",
        "outputs": [
            {
                "batch_size": s.batch_size,
                "file": s.path.name,
                "events": s.written,
                "batches": s.batches,
                "final_partial_flush": s.final_partial,
            }
            for s in sinks
        ],
    }
    manifest_path = out_dir / f"capture_manifest_{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"  manifest -> {manifest_path.name}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--duration", type=int, default=600, help="capture duration in seconds"
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        action="append",
        dest="batch_sizes",
        help="batch size to record; repeat the flag to compare several on one stream",
    )
    ap.add_argument("--symbols", default="BTC-USD,ETH-USD,SOL-USD,BNB-USD,XRP-USD")
    ap.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "results")
    args = ap.parse_args()

    batch_sizes = sorted(set(args.batch_sizes or [50]))
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(capture(args.duration, batch_sizes, symbols, args.out_dir))


if __name__ == "__main__":
    main()
