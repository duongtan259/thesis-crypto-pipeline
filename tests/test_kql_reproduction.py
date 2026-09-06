from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_reproduction_uses_timestamped_anomaly_fixture():
    source = (ROOT / "scripts" / "kql_reproduction.py").read_text()

    assert "def validate_anomaly_fixture" in source
    assert 'DetectPriceSpikes(0.5, 60s)' in source
    assert 'DetectPriceSpikes(0.5, 120s)' in source
    assert '"expected_alerts_60s": 1' in source
    assert '"expected_alerts_120s": 2' in source


def test_gold_rebuild_retries_emulator_job_release():
    source = (ROOT / "scripts" / "kql_reproduction.py").read_text()

    assert "def recreate_gold_with_retry" in source
    assert "ContinuousJobAlreadyRunningException" in source
    assert "attempts=6" in source


def test_kusto_report_records_source_provenance():
    source = (ROOT / "scripts" / "kql_reproduction.py").read_text()

    assert '"git_commit": git_commit()' in source
    assert '"capture_sha256": file_sha256(capture)' in source
