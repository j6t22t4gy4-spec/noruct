from retry_policy import backoff_delay


def test_backoff_starts_at_base() -> None:
    assert backoff_delay(0, 2, 20) == 2


def test_backoff_is_capped() -> None:
    assert backoff_delay(5, 2, 10) == 10
