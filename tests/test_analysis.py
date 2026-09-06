import json
from pathlib import Path

import numpy as np
from analyse_experiment import (
    cloud_commit_p50,
    fig_decomposition,
    fig_throughput,
    newey_west_mean_ci,
    paired_model_diagnostics,
    select_common_cohort,
)


def write_events(path: Path, rows: list[tuple[str, str, float]]) -> None:
    with path.open("w") as handle:
        for event_id, symbol, latency_ms in rows:
            handle.write(
                json.dumps(
                    {
                        "event_id": event_id,
                        "symbol": symbol,
                        "source": "coinbase_ws",
                        "timestamp_utc": "2026-09-05T08:00:00+00:00",
                        "ingestion_time": f"2026-09-05T08:00:00.{int(latency_ms):06d}+00:00",
                    }
                )
                + "\n"
            )


def test_common_cohort_uses_same_ordered_event_ids_after_partial_flush_removal(
    tmp_path: Path,
) -> None:
    write_events(
        tmp_path / "b1.jsonl",
        [("a", "BTC", 10), ("b", "ETH", 20), ("c", "BTC", 30), ("d", "ETH", 40)],
    )
    write_events(
        tmp_path / "b2.jsonl",
        [("a", "BTC", 11), ("b", "ETH", 21), ("c", "BTC", 31), ("x", "ETH", 41)],
    )
    manifest = {
        "outputs": [
            {"batch_size": 1, "file": "b1.jsonl", "final_partial_flush": 0},
            {"batch_size": 2, "file": "b2.jsonl", "final_partial_flush": 1},
        ]
    }

    cohort = select_common_cohort(manifest, tmp_path)

    assert [row["event_id"] for row in cohort[1]] == ["a", "b", "c"]
    assert [row["event_id"] for row in cohort[2]] == ["a", "b", "c"]
    assert [row["symbol"] for row in cohort[2]] == ["BTC", "ETH", "BTC"]


def test_newey_west_interval_reflects_positive_serial_dependence() -> None:
    values = np.repeat(np.array([0.0, 10.0, 20.0, 30.0]), 40)

    result = newey_west_mean_ci(values)

    hac_width = result["ci95"][1] - result["ci95"][0]
    iid_width = result["iid_ci95"][1] - result["iid_ci95"][0]
    assert result["lag"] > 0
    assert hac_width >= iid_width


def test_paired_model_diagnostics_use_event_aligned_arm_differences() -> None:
    baseline = np.array([10.0, 30.0, 20.0, 40.0])
    runs = {
        1: {"latencies": baseline},
        2: {"latencies": baseline + np.array([50.0, 50.0, 50.0, 50.0])},
    }

    result = paired_model_diagnostics(runs, lam=10.0)

    assert result[0]["batch_size"] == 2
    assert result[0]["paired_mean_difference_ms"] == 50
    assert result[0]["predicted_difference_ms"] == 50
    assert result[0]["residual_mean_ms"] == 0
    assert result[0]["residual_ci95_ms"] == [0, 0]
    assert result[0]["contrast_batches"] == 2
    assert result[0]["lag_sensitivity"][0]["lag"] == 0


def test_throughput_uses_longest_duration_evidence_runs(tmp_path: Path) -> None:
    def write_run(name: str, duration: float, acknowledged_rate: float) -> None:
        payload = {
            "schema_version": 2,
            "test_config": {
                "target_eps": 100,
                "requested_duration_s": duration,
            },
            "rates_eps": {"broker_acknowledged": acknowledged_rate},
            "counts": {"errors": 0, "broker_acknowledged": int(duration * 100)},
        }
        (tmp_path / name).write_text(json.dumps(payload))

    write_run("load_test_v2_100eps_20260905_080000_000001.json", 2, 90)
    write_run("load_test_v2_100eps_20260905_081000_000001.json", 60, 99.8)
    write_run("load_test_v2_100eps_20260905_082000_000001.json", 60, 99.9)

    result = fig_throughput(tmp_path, tmp_path / "throughput.png")

    assert result["requested_duration_s"] == 60
    assert result["per_target"][0]["runs"] == 2


def test_decomposition_does_not_invent_unmeasured_latency_ranges(
    tmp_path: Path,
) -> None:
    result = fig_decomposition(
        {"transit_ms_from_batch1": 79}, 2516, tmp_path / "decomposition.png"
    )

    downstream = [row for row in result if not row["measured"]]
    assert downstream
    assert all(row["value_ms"] is None for row in downstream)


def test_decomposition_can_show_measured_combined_cloud_commit_boundary(
    tmp_path: Path,
) -> None:
    result = fig_decomposition(
        {"transit_ms_from_batch1": 79},
        2516,
        tmp_path / "decomposition.png",
        cloud_commit_p50_ms=1170,
    )

    combined = next(
        row for row in result if "Serialisation → Bronze commit" in row["segment"]
    )
    assert combined["value_ms"] == 1170
    assert combined["measured"] is True

    separate_managed_components = {
        row["segment"]: row
        for row in result
        if row["segment"] in {"Event Hub → Eventstream", "Eventstream → Bronze"}
    }
    assert separate_managed_components
    assert all(
        row["value_ms"] is None and row["measured"] is False
        for row in separate_managed_components.values()
    )


def test_cloud_commit_p50_reads_highest_rate_from_latest_report(tmp_path: Path) -> None:
    older = {
        "runs": [
            {
                "target_events_per_second": 1000,
                "commit_latency_ms": {"p50": 999},
            }
        ]
    }
    latest = {
        "runs": [
            {
                "target_events_per_second": 500,
                "commit_latency_ms": {"p50": 992},
            },
            {
                "target_events_per_second": 1000,
                "commit_latency_ms": {"p50": 1170},
            },
        ]
    }
    (tmp_path / "cloud_e2e_validation_20260904.json").write_text(json.dumps(older))
    (tmp_path / "cloud_e2e_validation_20260905.json").write_text(json.dumps(latest))

    assert cloud_commit_p50(tmp_path) == 1170
