"""
Latency measurement — computes the `latency_ms` distribution reported in
thesis Section 5.6 from a capture of events published by the generator.

`latency_ms` = ingestion_time - timestamp_utc, i.e. the exchange tick to
publish-call segment. The generator stamps both fields (PriceEvent.to_json_bytes),
so the value is identical for a Kafka or an Event Hub sink.

Usage:
    # 1. capture events from the broker to a JSON-lines file
    docker compose --profile local exec -T kafka kafka-console-consumer \
      --bootstrap-server localhost:9092 --topic crypto-prices \
      --from-beginning --max-messages 90000 --timeout-ms 20000 > capture.jsonl

    # 2. compute percentiles (load_test events are excluded by default)
    python scripts/measure_latency.py capture.jsonl
    python scripts/measure_latency.py capture.jsonl --source load_test --json out.json

Results are written to scripts/results/latency_<timestamp>.json unless --json is given.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def parse_iso(value: str) -> datetime:
    # Python < 3.11 does not accept a trailing "Z" in fromisoformat.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(sorted_values: list[float], p: float) -> float:
    """Return the exact nearest-rank percentile used by this local analysis."""
    if not sorted_values:
        return float("nan")
    rank = max(1, min(len(sorted_values), int(-(-p * len(sorted_values) // 100))))
    return sorted_values[rank - 1]


def load_latencies(
    path: Path, source_filter: str
) -> tuple[list[float], dict[str, list[float]]]:
    overall: list[float] = []
    per_symbol: dict[str, list[float]] = {}
    skipped = 0

    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if source_filter and event.get("source") != source_filter:
                    continue
                delta = parse_iso(event["ingestion_time"]) - parse_iso(
                    event["timestamp_utc"]
                )
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                skipped += 1
                continue
            ms = delta.total_seconds() * 1000.0
            if ms < 0:  # clock skew between exchange and host; not a valid sample
                skipped += 1
                continue
            overall.append(ms)
            per_symbol.setdefault(event.get("symbol", "UNKNOWN"), []).append(ms)

    if skipped:
        print(f"skipped {skipped} unparseable or negative-latency records")
    return overall, per_symbol


def summarise(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": round(percentile(ordered, 50)),
        "p95": round(percentile(ordered, 95)),
        "p99": round(percentile(ordered, 99)),
        "avg": round(sum(ordered) / len(ordered)) if ordered else None,
        "min": round(ordered[0]) if ordered else None,
        "max": round(ordered[-1]) if ordered else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("capture", type=Path, help="JSON-lines capture of published events")
    ap.add_argument(
        "--source",
        default="coinbase_ws",
        help="only include events with this `source` value; empty string includes all",
    )
    ap.add_argument(
        "--json", type=Path, default=None, help="where to write the result JSON"
    )
    args = ap.parse_args()

    overall, per_symbol = load_latencies(args.capture, args.source)
    if not overall:
        raise SystemExit(
            f"no events with source={args.source!r} found in {args.capture}"
        )

    result = {
        "capture": str(args.capture),
        "source_filter": args.source,
        "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "overall": summarise(overall),
        "per_symbol": {
            s: summarise(v)
            for s, v in sorted(per_symbol.items(), key=lambda kv: -len(kv[1]))
        },
    }

    o = result["overall"]
    print(f"\nlatency_ms over {o['count']} events (source={args.source or 'all'})")
    print(
        f"  p50 {o['p50']}  p95 {o['p95']}  p99 {o['p99']}  avg {o['avg']}  min {o['min']}  max {o['max']}\n"
    )
    print(f"{'symbol':<10}{'events':>8}{'avg':>8}{'p95':>8}")
    for symbol, stats in result["per_symbol"].items():
        print(f"{symbol:<10}{stats['count']:>8}{stats['avg']:>8}{stats['p95']:>8}")

    out = args.json or (
        Path(__file__).parent
        / "results"
        / f"latency_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
