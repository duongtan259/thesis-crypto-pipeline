import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
RESULTS = ROOT / "scripts" / "results"


def load(name: str) -> dict:
    return json.loads((RESULTS / name).read_text())


def test_canonical_experiment_report_matches_thesis_claims() -> None:
    report = load("experiment_report_v2_20260905_201811.json")

    assert report["provenance"]["common_cohort_events"] == 18_000
    assert report["model_test"]["held_out_arms"] == [10, 50, 100]
    assert max(abs(row["error_pct"]) for row in report["model_test"]["rows"]) <= 0.7
    assert [
        row["residual_ci95_ms"] for row in report["paired_model_diagnostics"]
    ] == [[-16, 12], [-109, 108], [-217, 290]]
    assert all(
        low <= 0 <= high
        for row in report["paired_model_diagnostics"]
        for low, high in [row["residual_ci95_ms"]]
    )
    assert sum(row["total_sent"] for row in report["load_test"]["per_target"]) == 288_000
    assert sum(row["total_errors"] for row in report["load_test"]["per_target"]) == 0
    assert all(
        row["value_ms"] is None
        for row in report["latency_decomposition"]
        if not row["measured"]
    )
    cloud_boundary = next(
        row
        for row in report["latency_decomposition"]
        if row["segment"].startswith("Serialisation → Bronze commit")
    )
    assert cloud_boundary["value_ms"] == 1170
    assert cloud_boundary["measured"]


def test_canonical_kusto_report_matches_thesis_claims() -> None:
    report = load("kql_reproduction_v2_20260905_091800.json")
    reconciliation = report["event_id_reconciliation"][0]
    accuracy = report["gold_ohlc_accuracy"][0]

    assert reconciliation == {
        "bronze_rows": 18_089,
        "bronze_distinct_ids": 18_089,
        "silver_rows": 18_089,
        "silver_distinct_ids": 18_089,
        "bronze_missing_in_silver": 0,
        "silver_missing_in_bronze": 0,
    }
    for field in ("open_correct", "high_correct", "low_correct", "close_correct"):
        assert accuracy[field] == accuracy["windows"] == 155
    assert accuracy["latest_volume_24h_correct"] == 155
    assert report["gold_deterministic_rebuild"]["byte_stable_sorted_projection"]
    assert report["anomaly_fixture_validation"]["all_correct"]
    assert report["silver_predicate_test"]["all_correct"]


def test_cloud_validation_report_matches_thesis_claims() -> None:
    report = load("cloud_e2e_validation_20260905.json")

    assert report["environment"] == {
        "azure_region": "North Europe",
        "fabric_region": "Sweden Central",
        "fabric_capacity": "Trial",
        "event_hub": {
            "tier": "Standard",
            "partitions": 4,
            "throughput_units": 2,
            "retention_days": 7,
        },
        "kusto": {
            "build_version": "1.0.9741.10129",
            "product_version": "2026.09.02.0524-2635-6e3ec92-WeeklyStaging",
        },
    }
    assert [
        (
            row["target_events_per_second"],
            row["scheduled"],
            row["bronze_ids"],
            row["silver_ids"],
            row["missing_sequences"],
            tuple(row["commit_latency_ms"].values()),
        )
        for row in report["runs"]
    ] == [
        (100, 1_000, 1_000, 1_000, 0, (1294, 2110, 2113)),
        (500, 5_000, 5_000, 5_000, 0, (992, 1550, 1647)),
        (1000, 10_000, 10_000, 10_000, 0, (1170, 1811, 2007)),
    ]
    assert all(
        value == 0
        for key, value in report["gold_truth_check"].items()
        if key.endswith("mismatches")
    )
    assert all(report["post_validation_controls"].values())
