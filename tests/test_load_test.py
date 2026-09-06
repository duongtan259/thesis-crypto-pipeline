import asyncio

import load_test
from load_test import build_result, scheduled_deadline, summarise_latencies


def test_absolute_schedule_does_not_accumulate_work_time() -> None:
    start = 100.0

    assert scheduled_deadline(start, sequence=0, eps=20.0) == 100.0
    assert scheduled_deadline(start, sequence=20, eps=20.0) == 101.0
    assert scheduled_deadline(start, sequence=40, eps=20.0) == 102.0


def test_latency_summary_is_honest_when_nothing_was_acknowledged() -> None:
    assert summarise_latencies([]) == {
        "count": 0,
        "mean_ms": None,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "max_ms": None,
    }


def test_result_keeps_scheduled_generated_and_acknowledged_counts_distinct() -> None:
    result = build_result(
        eps=100.0,
        requested_duration_s=2.0,
        target="kafka",
        batch_size=25,
        queue_size=50,
        scheduled=200,
        generated=198,
        acknowledged=190,
        queue_overflows=2,
        errors=8,
        batches=8,
        elapsed_s=2.5,
        ack_latencies_ms=[4.0, 6.0],
        started_at="2026-09-05T08:00:00+00:00",
        finished_at="2026-09-05T08:00:02+00:00",
    )

    assert result["counts"] == {
        "scheduled": 200,
        "generated": 198,
        "broker_acknowledged": 190,
        "queue_overflows": 2,
        "errors": 8,
    }
    assert result["rates_eps"]["broker_acknowledged"] == 76.0
    assert result["claims"]["proves_downstream_losslessness"] is False


def test_final_partial_batch_is_acknowledged(monkeypatch, tmp_path) -> None:
    class FakePublisher:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def send_batch(self, events):
            return len(events)

    monkeypatch.setattr(load_test, "_publisher", lambda target: FakePublisher())

    result = asyncio.run(
        load_test.run_load_test(
            eps=100,
            duration=0.03,
            target="kafka",
            batch_size=10,
            queue_size=10,
            output_dir=tmp_path,
        )
    )

    assert result["counts"]["scheduled"] == 3
    assert result["counts"]["broker_acknowledged"] == 3
    assert result["batches"] == 1
