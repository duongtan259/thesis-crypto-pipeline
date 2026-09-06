from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gold_is_deterministic_and_does_not_sum_rolling_volume_snapshot() -> None:
    kql = (ROOT / "kql" / "03_gold.kql").read_text()

    assert "take_any" not in kql
    assert "arg_min(open_order, open)" in kql
    assert "arg_max(close_order, close, latest_volume_24h)" in kql
    assert "sum(volume_24h)" not in kql
    assert "latest_volume_24h" in kql
    assert ".create-or-alter materialized-view" in kql


def test_price_spike_function_honours_its_window_parameter() -> None:
    kql = (ROOT / "kql" / "05_anomaly_detection.kql").read_text()
    price_function = kql.split(".create-or-alter function DetectPriceSpikes", 1)[
        1
    ].split(".create-or-alter", 1)[0]

    assert "ago(window)" in price_function
    assert "prev_time" in price_function
    assert "timestamp_utc - prev_time <= window" in price_function


def test_volume_function_names_the_rolling_snapshot_it_uses() -> None:
    kql = (ROOT / "kql" / "05_anomaly_detection.kql").read_text()

    assert "DetectVolumeSurges" not in kql
    assert "DetectVolume24hSnapshotChange" in kql


def test_reproduction_records_reconciliation_and_deterministic_rebuild() -> None:
    script = (ROOT / "scripts" / "kql_reproduction.py").read_text()

    assert "DetectVolumeSurges" not in script
    assert 'r["event_id_reconciliation"]' in script
    assert 'results["gold_deterministic_rebuild"]' in script
    assert "OHLCV" not in script
