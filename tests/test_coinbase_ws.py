from datetime import datetime, timezone

import pytest
from sources.coinbase_ws import _event_time_or_now, _parse_coinbase_time


def test_parse_coinbase_time_accepts_z_and_truncates_nanoseconds() -> None:
    parsed = _parse_coinbase_time("2026-09-05T08:09:10.123456789Z")

    assert parsed == datetime(2026, 9, 5, 8, 9, 10, 123456, tzinfo=timezone.utc)


def test_parse_coinbase_time_preserves_explicit_offset() -> None:
    parsed = _parse_coinbase_time("2026-09-05T10:09:10.123456+02:00")

    assert parsed.utcoffset().total_seconds() == 7200


@pytest.mark.parametrize("raw", [None, "", "not-a-timestamp"])
def test_event_time_falls_back_for_missing_or_invalid_values(raw: str | None) -> None:
    fallback = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)

    assert _event_time_or_now(raw, lambda: fallback) == fallback
