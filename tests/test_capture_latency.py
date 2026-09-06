from capture_latency import arrival_rate, rotated_sinks


def test_sink_order_rotates_instead_of_always_favouring_first_sink() -> None:
    sinks = ["b1", "b10", "b50", "b100"]

    assert rotated_sinks(sinks, 0) == ["b1", "b10", "b50", "b100"]
    assert rotated_sinks(sinks, 1) == ["b10", "b50", "b100", "b1"]
    assert rotated_sinks(sinks, 4) == sinks


def test_arrival_rate_uses_measured_elapsed_time() -> None:
    assert arrival_rate(events=20, elapsed_s=4.0) == 5.0
