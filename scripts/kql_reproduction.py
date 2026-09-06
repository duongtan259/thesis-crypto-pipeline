"""
Medallion reproduction on the Kusto engine.

Builds the Bronze -> Silver -> Gold medallion from the project's own KQL scripts in a
local Kusto emulator, ingests a real captured event file into Bronze, and runs the
measurement queries that Sections 5.2, 5.4 and 5.5 of the thesis specify but that the
decommissioned Fabric capacity could not answer.

Scope, and the limits of it. The emulator runs the same Kusto engine that backs a
Fabric KQL Database, so update-policy firing, the Silver validation predicate and the
stored functions execute the same logic on the same data. Row counts, the Silver
filter rate and the anomaly-function output are therefore properties of the data and
the KQL, and transfer. Anything governed by the hosting tier's scheduler — above all
the materialized-view refresh cadence — is a property of the deployment and does NOT
transfer; those results are reported as engine-level observations only.

Usage:
    docker run -d --name kustainer -p 8080:8080 -e ACCEPT_EULA=Y \
        mcr.microsoft.com/azuredataexplorer/kustainer-linux:latest
    python scripts/kql_reproduction.py --capture scripts/results/capture_batch50_*.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENDPOINT = "http://localhost:8080"
DB = "NetDefaultDB"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).parent.parent,
        text=True,
    ).strip()


class Kusto:
    def __init__(self, endpoint: str = ENDPOINT, db: str = DB):
        self.endpoint, self.db = endpoint, db

    def _post(self, route: str, csl: str, timeout: int = 300) -> dict:
        body = json.dumps({"db": self.db, "csl": csl}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.endpoint}/v1/rest/{route}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _rows(payload: dict) -> list[dict]:
        """v1 REST returns several frames; the first is the primary result."""
        tables = payload.get("Tables") or []
        if not tables:
            return []
        table = tables[0]
        names = [c["ColumnName"] for c in table["Columns"]]
        return [dict(zip(names, row)) for row in table["Rows"]]

    def query(self, csl: str) -> list[dict]:
        return self._rows(self._post("query", csl))

    def mgmt(self, csl: str) -> list[dict]:
        return self._rows(self._post("mgmt", csl))

    def scalar(self, csl: str):
        rows = self.query(csl)
        return next(iter(rows[0].values())) if rows else None

    def wait_ready(self, attempts: int = 90) -> None:
        for i in range(attempts):
            try:
                self.mgmt(".show version")
                print(f"  emulator ready after {i * 2}s")
                return
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                OSError,
                TimeoutError,
            ):
                time.sleep(2)
        raise SystemExit("emulator did not become ready — is the container running?")


# price_raw is created by Eventstream in Fabric, so it has no DDL in kql/01_bronze.kql.
# The column set is the one the Bronze ingestion mapping targets.
BRONZE_DDL = """
.create-merge table price_raw (
    event_id: string, symbol: string, price: real, volume_24h: real, market_cap: real,
    timestamp_utc: datetime, source: string, sequence: long, ingestion_time: datetime,
    raw_payload: string
)
"""


def split_statements(script: str) -> list[str]:
    """Split a .kql file into statements: a new one starts at a top-level '.' command."""
    out, current = [], []
    for line in script.split("\n"):
        stripped = line.strip()
        if stripped.startswith("//") or not stripped:
            continue
        if stripped.startswith(".") and current:
            out.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        out.append("\n".join(current))
    return [s for s in out if s.strip()]


def apply_script(k: Kusto, path: Path, skip: tuple[str, ...] = ()) -> None:
    for statement in split_statements(path.read_text()):
        head = statement.strip().split("\n")[0][:70]
        if any(s in statement for s in skip):
            print(f"  skip  {head}")
            continue
        try:
            k.mgmt(statement)
            print(f"  ok    {head}")
        except urllib.error.HTTPError as e:
            print(
                f"  FAIL  {head}\n        {e.read().decode('utf-8', 'replace')[:300]}"
            )
            raise


def recreate_gold_with_retry(k: Kusto, path: Path, attempts=6) -> None:
    """Recreate Gold after the emulator releases the dropped continuous job."""
    statements = [
        statement
        for statement in split_statements(path.read_text())
        if ".show " not in statement
    ]
    for attempt in range(1, attempts + 1):
        try:
            k.mgmt(statements[0])
            print(f"  ok    {statements[0].strip().splitlines()[0][:70]}")
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            retryable = (
                exc.code == 429 and "ContinuousJobAlreadyRunningException" in body
            )
            if not retryable or attempt == attempts:
                print(f"  FAIL  Gold recreation\n        {body[:300]}")
                raise
            delay = attempt * 5
            print(f"  wait  Gold background job release ({delay}s)")
            time.sleep(delay)

    for statement in statements[1:]:
        k.mgmt(statement)
        print(f"  ok    {statement.strip().splitlines()[0][:70]}")


PROJECT_FROM_DYNAMIC = """
.set-or-append price_raw <|
print payload = dynamic({rows})
| mv-expand e = payload
| project event_id       = tostring(e.event_id),
          symbol         = tostring(e.symbol),
          price          = toreal(e.price),
          volume_24h     = toreal(e.volume_24h),
          market_cap     = toreal(e.market_cap),
          timestamp_utc  = todatetime(e.timestamp_utc),
          source         = tostring(e.source),
          sequence       = tolong(e.sequence),
          ingestion_time = todatetime(e.ingestion_time),
          raw_payload    = tostring(e.raw_payload)
"""


def ingest(k: Kusto, capture: Path, chunk: int = 1000) -> int:
    """Load the capture into Bronze, so the update policy fires as it would in Fabric.

    Inline JSON ingestion is tried first because it exercises the real ingestion
    mapping from kql/01_bronze.kql. Not every engine build accepts a format property
    on inline ingestion, so a projection from a dynamic literal is used as a fallback;
    both are ordinary ingestion operations and both trigger the update policy.
    """
    lines = [line for line in capture.read_text().split("\n") if line.strip()]
    use_mapping = True
    for start in range(0, len(lines), chunk):
        batch = lines[start : start + chunk]
        if use_mapping:
            try:
                k.mgmt(
                    ".ingest inline into table price_raw with "
                    "(format='json', ingestionMappingReference='RawPricesMapping') <|\n"
                    + "\n".join(batch)
                )
                print(
                    f"  ingested {min(start + chunk, len(lines)):,}/{len(lines):,} (mapping)"
                )
                continue
            except urllib.error.HTTPError:
                print(
                    "  inline JSON ingestion unavailable — falling back to projection"
                )
                use_mapping = False
        rows = "[" + ",".join(batch) + "]"
        k.mgmt(PROJECT_FROM_DYNAMIC.format(rows=rows))
        print(
            f"  ingested {min(start + chunk, len(lines)):,}/{len(lines):,} (projection)"
        )
    return len(lines)


def measure(k: Kusto, source_events: int) -> dict:
    r: dict = {}

    # --- Section 5.2: medallion row counts and the Silver filter rate ---
    bronze = k.scalar("price_raw | count")
    silver = k.scalar("price_silver | count")
    r["medallion"] = {
        "source_events_in_file": source_events,
        "bronze_rows": bronze,
        "silver_rows": silver,
        "filtered_rows": bronze - silver,
        "filter_rate_pct": round((bronze - silver) / bronze * 100, 4)
        if bronze
        else None,
    }
    r["event_id_reconciliation"] = k.query("""
        let bronze = price_raw
            | where source == 'coinbase_ws'
            | project event_id = tostring(event_id);
        let silver = price_silver
            | where source == 'coinbase_ws'
            | project event_id = tostring(event_id);
        print
            bronze_rows = toscalar(bronze | count),
            bronze_distinct_ids = toscalar(bronze | summarize by event_id | count),
            silver_rows = toscalar(silver | count),
            silver_distinct_ids = toscalar(silver | summarize by event_id | count),
            bronze_missing_in_silver = toscalar(
                bronze | join kind=leftanti silver on event_id | count),
            silver_missing_in_bronze = toscalar(
                silver | join kind=leftanti bronze on event_id | count)
    """)

    # --- Section 5.2: latency as computed inside the database, and per symbol ---
    r["latency_kql"] = k.query("""
        price_silver
        | summarize p50 = percentile(toreal(latency_ms), 50),
                    p95 = percentile(toreal(latency_ms), 95),
                    p99 = percentile(toreal(latency_ms), 99),
                    avg_ms = avg(toreal(latency_ms)),
                    min_ms = min(toreal(latency_ms)),
                    max_ms = max(toreal(latency_ms)),
                    events = count()
    """)
    r["per_symbol_kql"] = k.query("""
        price_silver
        | summarize event_count = count(), avg_latency = round(avg(toreal(latency_ms)), 1),
                    p95_latency = percentile(toreal(latency_ms), 95) by symbol
        | order by event_count desc
    """)

    # Gold correctness against an independently written query using the same declared
    # timestamp, sequence, event-id ordering contract. Predicate-test records are excluded.
    r["gold_ohlc_accuracy"] = k.query("""
        price_silver
        | where source == 'coinbase_ws'
        | extend truth_order = strcat(
            tostring(timestamp_utc), '|',
            substring(strcat('00000000000000000000', tostring(sequence)), -20),
            '|', event_id)
        | extend first_order = truth_order, last_order = truth_order,
                 true_open = price_usd, true_close = price_usd,
                 true_latest_volume_24h = volume_24h
        | summarize arg_min(first_order, true_open),
                    arg_max(last_order, true_close, true_latest_volume_24h),
                    true_high = max(price_usd), true_low = min(price_usd), n = count()
          by symbol, w = bin(timestamp_utc, 1m)
        | project symbol, w, n, true_open, true_close,
                  true_latest_volume_24h, true_high, true_low
        | join kind=inner (price_gold | where symbol !startswith 'TEST-'
                           | project symbol, w = window_start, open, close, high, low,
                                     latest_volume_24h) on symbol, w
        | summarize windows = count(),
                    high_correct  = countif(abs(high - true_high) < 1e-9),
                    low_correct   = countif(abs(low - true_low) < 1e-9),
                    open_correct  = countif(abs(open - true_open) < 1e-9),
                    close_correct = countif(abs(close - true_close) < 1e-9),
                    latest_volume_24h_correct = countif(
                        abs(latest_volume_24h - true_latest_volume_24h) < 1e-9),
                    max_open_err_pct  = round(max(abs(open - true_open) / true_open * 100), 4),
                    max_close_err_pct = round(max(abs(close - true_close) / true_close * 100), 4),
                    open_eq_close = countif(abs(open - close) < 1e-9),
                    true_open_ne_close = countif(abs(true_open - true_close) > 1e-9),
                    single_event_windows = countif(n == 1)
    """)

    # --- Section 5.4: Gold layer and materialized-view behaviour ---
    r["gold_sample"] = k.query("price_gold | order by window_start desc | take 5")
    r["gold_rows"] = k.scalar("price_gold | count")
    try:
        r["mv_details"] = k.mgmt(".show materialized-view price_gold details")
    except urllib.error.HTTPError as e:
        r["mv_details"] = {"error": e.read().decode("utf-8", "replace")[:400]}
    # The lag query as written in the thesis compares the newest Silver timestamp with
    # the newest Gold *window start*. For a 1-minute bin that difference contains up to
    # 60 s of pure window alignment before any refresh delay, so the window-end form is
    # recorded alongside it and the two are reported separately.
    r["mv_lag"] = k.query("""
        print silver_latest = toscalar(price_silver | summarize max(timestamp_utc)),
              gold_latest   = toscalar(price_gold  | summarize max(window_start))
        | extend lag_to_window_start_seconds = datetime_diff('second', silver_latest, gold_latest)
        | extend lag_beyond_window_end_seconds =
                 datetime_diff('second', silver_latest, datetime_add('minute', 1, gold_latest))
    """)
    r["gold_windows"] = k.query("""
        price_gold
        | summarize windows = count(), symbols = dcount(symbol),
                    events_in_gold = sum(event_count),
                    earliest = min(window_start), latest = max(window_start)
    """)

    # --- Section 5.5: anomaly detection ---
    for name, csl in (
        ("DetectPriceSpikes(1.0, 60s)", "DetectPriceSpikes(1.0, 60s)"),
        (
            "DetectVolume24hSnapshotChange(2.0, 10m)",
            "DetectVolume24hSnapshotChange(2.0, 10m)",
        ),
        ("GetVolatility(1h)", "GetVolatility(1h)"),
    ):
        try:
            r.setdefault("anomaly", {})[name] = k.query(csl)
        except urllib.error.HTTPError as e:
            r.setdefault("anomaly", {})[name] = {
                "error": e.read().decode("utf-8", "replace")[:400]
            }

    # A single threshold only shows whether that threshold fired. Sweeping it shows the
    # function responds monotonically to real price movement, which is what validates it.
    sweep = []
    for threshold in (0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0):
        try:
            rows = k.query(f"DetectPriceSpikes({threshold}, 60s) | count")
            sweep.append(
                {"threshold_pct": threshold, "alerts": next(iter(rows[0].values()))}
            )
        except urllib.error.HTTPError:
            sweep.append({"threshold_pct": threshold, "alerts": None})
    r["spike_threshold_sweep"] = sweep

    # A separately written equivalence check for the consecutive-tick calculation.
    # It is not independent ground truth for whether a market move is anomalous.
    truth = []
    for threshold in (0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0):
        rows = k.query(f"""
            price_silver
            | where timestamp_utc > ago(60s)
            | sort by symbol asc, timestamp_utc asc, sequence asc, event_id asc
            | serialize
            | extend prev_price = prev(price_usd, 1), prev_symbol = prev(symbol, 1),
                     prev_time = prev(timestamp_utc, 1)
            | where symbol == prev_symbol and timestamp_utc - prev_time <= 60s
            | extend change_pct = (price_usd - prev_price) / prev_price * 100.0
            | where abs(change_pct) >= {threshold}
            | count
        """)
        truth.append(
            {"threshold_pct": threshold, "moves": next(iter(rows[0].values()))}
        )
    r["spike_ground_truth"] = truth
    r["events_in_spike_window"] = k.scalar(
        "price_silver | where timestamp_utc > ago(60s) | count"
    )

    # The largest observed consecutive-tick move, for context on the sweep above.
    r["largest_tick_move"] = k.query("""
        price_silver
        | sort by symbol asc, timestamp_utc asc
        | serialize
        | extend prev_price = prev(price_usd, 1), prev_symbol = prev(symbol, 1)
        | where symbol == prev_symbol and prev_price > 0
        | extend change_pct = abs((price_usd - prev_price) / prev_price * 100.0)
        | summarize max_move_pct = round(max(change_pct), 5),
                    p99_move_pct = round(percentile(change_pct, 99), 5) by symbol
        | order by max_move_pct desc
    """)

    # --- Update policy and Silver validation, verified rather than assumed ---
    r["update_policy"] = k.mgmt(".show table price_silver policy update")
    r["silver_invalid_rows"] = k.scalar(
        "price_silver | where is_valid == false or price_usd <= 0 or isempty(symbol) | count"
    )
    return r


def gold_snapshot(k: Kusto) -> list[dict]:
    """Return a stable projection used to compare clean materialized-view rebuilds."""
    return k.query("""
        price_gold
        | where symbol !startswith 'TEST-'
        | project symbol, window_start, open, high, low, close,
                  latest_volume_24h, event_count, avg_latency_ms,
                  p95_latency_ms, p99_latency_ms
        | order by symbol asc, window_start asc
    """)


def snapshot_sha256(rows: list[dict]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_silver_predicate(k: Kusto) -> dict:
    """Functional test of the Silver validation predicate on the real engine.

    Live market data contains no malformed records, so a filter rate measured on it
    is near zero and shows only that valid data survives. This test injects records
    that are individually invalid in each of the ways SilverTransform() screens for,
    plus valid controls, and checks that exactly the controls reach Silver.
    """
    now = datetime.now(timezone.utc).isoformat()
    cases = [
        ("valid_control_a", {"symbol": "TEST-A", "price": 100.0}, True),
        ("valid_control_b", {"symbol": "TEST-B", "price": 0.00001}, True),
        ("null_price", {"symbol": "TEST-C"}, False),
        ("zero_price", {"symbol": "TEST-D", "price": 0.0}, False),
        ("negative_price", {"symbol": "TEST-E", "price": -42.5}, False),
        ("empty_symbol", {"symbol": "", "price": 100.0}, False),
    ]
    events = []
    for name, fields, _ in cases:
        events.append(
            json.dumps(
                {
                    "event_id": f"validation-{name}",
                    "symbol": fields.get("symbol"),
                    "price": fields.get("price"),
                    "volume_24h": 1.0,
                    "market_cap": 0.0,
                    "timestamp_utc": now,
                    "source": "validation_test",
                    "sequence": 0,
                    "ingestion_time": now,
                    "raw_payload": "{}",
                }
            )
        )

    before_bronze = k.scalar("price_raw | count")
    before_silver = k.scalar("price_silver | count")
    k.mgmt(
        ".ingest inline into table price_raw with "
        "(format='json', ingestionMappingReference='RawPricesMapping') <|\n"
        + "\n".join(events)
    )
    time.sleep(8)

    admitted = {
        r["event_id"]
        for r in k.query(
            "price_silver | where source == 'validation_test' | project event_id"
        )
    }
    results = []
    for name, _, should_pass in cases:
        observed = f"validation-{name}" in admitted
        results.append(
            {
                "case": name,
                "expected_in_silver": should_pass,
                "observed_in_silver": observed,
                "correct": observed == should_pass,
            }
        )

    return {
        "cases": results,
        "all_correct": all(r["correct"] for r in results),
        "injected": len(cases),
        "admitted_to_silver": len(admitted),
        "bronze_before": before_bronze,
        "bronze_after": k.scalar("price_raw | count"),
        "silver_before": before_silver,
        "silver_after": k.scalar("price_silver | count"),
    }


def validate_anomaly_fixture(k: Kusto) -> dict:
    """Exercise the live-window spike function with controlled recent events.

    Historical captures eventually fall outside ``ago(window)``. Four fresh control
    rows make both the threshold and the supplied window independently testable: one
    qualifying move is inside 60 seconds and a second is only inside 120 seconds.
    """
    now = datetime.now(timezone.utc)
    points = [
        (-100, 100.0),
        (-90, 101.0),
        (-20, 101.0),
        (-10, 103.0),
    ]
    events = []
    for sequence, (seconds, price) in enumerate(points, 1):
        timestamp = (now + timedelta(seconds=seconds)).isoformat()
        events.append(
            json.dumps(
                {
                    "event_id": f"anomaly-control-{sequence}",
                    "symbol": "TEST-SPIKE",
                    "price": price,
                    "volume_24h": 1.0,
                    "market_cap": 0.0,
                    "timestamp_utc": timestamp,
                    "source": "anomaly_test",
                    "sequence": sequence,
                    "ingestion_time": timestamp,
                    "raw_payload": "{}",
                }
            )
        )

    k.mgmt(
        ".ingest inline into table price_raw with "
        "(format='json', ingestionMappingReference='RawPricesMapping') <|\n"
        + "\n".join(events)
    )
    time.sleep(8)

    alerts_60s = k.scalar("DetectPriceSpikes(0.5, 60s) | count")
    alerts_120s = k.scalar("DetectPriceSpikes(0.5, 120s) | count")
    return {
        "threshold_pct": 0.5,
        "expected_alerts_60s": 1,
        "observed_alerts_60s": alerts_60s,
        "expected_alerts_120s": 2,
        "observed_alerts_120s": alerts_120s,
        "all_correct": alerts_60s == 1 and alerts_120s == 2,
    }


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--capture", type=Path, default=None, help="JSON-lines event file to ingest"
    )
    ap.add_argument("--kql-dir", type=Path, default=here.parent / "kql")
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    capture = args.capture
    if capture is None:
        candidates = sorted((here / "results").glob("capture_batch50_*.jsonl"))
        if not candidates:
            raise SystemExit(
                "no capture_batch50_*.jsonl found — run capture_latency.py first"
            )
        capture = candidates[-1]

    k = Kusto(args.endpoint)
    print("waiting for the Kusto emulator...")
    k.wait_ready()

    # The emulator keeps its state across restarts, so a rerun would hit "already exists"
    # on .create statements and would double-count rows. Start from an empty database.
    print("\nclearing any previous run")
    for drop in (
        ".drop materialized-view price_gold ifexists",
        ".drop table price_silver ifexists",
        ".drop table price_raw ifexists",
        ".drop table price_alerts ifexists",
    ):
        try:
            k.mgmt(drop)
            print(f"  ok    {drop}")
        except urllib.error.HTTPError as e:
            print(f"  note  {drop} -> {e.code}")

    print("\ncreating Bronze table")
    k.mgmt(BRONZE_DDL)
    for script in (
        "01_bronze.kql",
        "02_silver.kql",
        "03_gold.kql",
        "05_anomaly_detection.kql",
    ):
        print(f"\napplying kql/{script}")
        # .show statements are diagnostics in the source files, not part of the build.
        apply_script(k, args.kql_dir / script, skip=(".show ",))

    print(f"\ningesting {capture.name}")
    n = ingest(k, capture)

    print("\nwaiting for the update policy and the materialized view to settle")
    time.sleep(20)

    first_gold = gold_snapshot(k)
    first_hash = snapshot_sha256(first_gold)
    print("\nrebuilding Gold from unchanged Silver input")
    k.mgmt(".drop materialized-view price_gold ifexists")
    recreate_gold_with_retry(k, args.kql_dir / "03_gold.kql")
    time.sleep(20)
    second_gold = gold_snapshot(k)
    second_hash = snapshot_sha256(second_gold)

    print("\nmeasuring")
    results = measure(k, n)
    results["gold_deterministic_rebuild"] = {
        "same_silver_input": True,
        "first_rows": len(first_gold),
        "second_rows": len(second_gold),
        "first_sha256": first_hash,
        "second_sha256": second_hash,
        "byte_stable_sorted_projection": first_hash == second_hash,
    }

    # Run only after the counts above are taken, so injected controls cannot
    # contaminate measured row counts, Gold accuracy, or the live-data filter rate.
    print("\nvalidating anomaly thresholds and window semantics with control events")
    results["anomaly_fixture_validation"] = validate_anomaly_fixture(k)
    print("\nvalidating the Silver predicate with injected malformed records")
    results["silver_predicate_test"] = validate_silver_predicate(k)
    results["meta"] = {
        "schema_version": 2,
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capture_file": capture.name,
        "capture_sha256": file_sha256(capture),
        "git_commit": git_commit(),
        "engine": k.mgmt(".show version")[0] if k.mgmt(".show version") else None,
        "note": "Kusto emulator — same engine as a Fabric KQL Database, but not Fabric F2. "
        "Scheduler-dependent results (materialized-view refresh cadence) do not "
        "transfer to F2; row counts, filter rate and function output do.",
    }

    out = args.out or (
        here
        / "results"
        / f"kql_reproduction_v2_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    )
    out.write_text(json.dumps(results, indent=2, default=str) + "\n")

    v = results["silver_predicate_test"]
    print(
        f"Silver predicate test: {sum(c['correct'] for c in v['cases'])}/{len(v['cases'])} "
        f"cases behaved as specified"
    )

    ohlc = results.get("gold_ohlc_accuracy") or [{}]
    if ohlc and ohlc[0]:
        o = ohlc[0]
        print(
            f"Gold OHLC over {o['windows']} windows: high {o['high_correct']}, low {o['low_correct']}, "
            f"open {o['open_correct']}, close {o['close_correct']} correct "
            f"(open==close in {o['open_eq_close']}, true open!=close in {o['true_open_ne_close']})"
        )

    m = results["medallion"]
    print(
        f"\nBronze {m['bronze_rows']:,}  Silver {m['silver_rows']:,}  "
        f"filtered {m['filtered_rows']:,}  filter rate {m['filter_rate_pct']}%"
    )
    stable = results["gold_deterministic_rebuild"]["byte_stable_sorted_projection"]
    print(f"Gold deterministic rebuild: {stable} ({first_hash})")
    print(f"Gold rows (1-minute price-candle windows): {results['gold_rows']:,}")
    if results["latency_kql"]:
        lat = results["latency_kql"][0]
        print(
            f"KQL-side latency: p50 {lat['p50']:.0f}  p95 {lat['p95']:.0f}  "
            f"p99 {lat['p99']:.0f}  min {lat['min_ms']:.0f}  max {lat['max_ms']:.0f}"
        )
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
