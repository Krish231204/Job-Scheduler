import pytest

from app.models import RetryStrategy
from app.services.retry import compute_retry_delay_seconds, should_dead_letter


def test_fixed_strategy_is_constant():
    for attempt in (1, 2, 5):
        assert compute_retry_delay_seconds(RetryStrategy.FIXED, attempt, base_delay_seconds=3.0, multiplier=2.0, max_delay_seconds=100) == 3.0


def test_linear_strategy_scales_with_attempt():
    assert compute_retry_delay_seconds(RetryStrategy.LINEAR, 1, base_delay_seconds=2.0, multiplier=2.0, max_delay_seconds=1000) == 2.0
    assert compute_retry_delay_seconds(RetryStrategy.LINEAR, 3, base_delay_seconds=2.0, multiplier=2.0, max_delay_seconds=1000) == 6.0


def test_exponential_strategy_doubles_each_attempt():
    assert compute_retry_delay_seconds(RetryStrategy.EXPONENTIAL, 1, base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=1000) == 1.0
    assert compute_retry_delay_seconds(RetryStrategy.EXPONENTIAL, 2, base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=1000) == 2.0
    assert compute_retry_delay_seconds(RetryStrategy.EXPONENTIAL, 4, base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=1000) == 8.0


def test_delay_is_capped_at_max_delay():
    delay = compute_retry_delay_seconds(RetryStrategy.EXPONENTIAL, 10, base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=30)
    assert delay == 30


def test_attempt_number_must_be_positive():
    with pytest.raises(ValueError):
        compute_retry_delay_seconds(RetryStrategy.FIXED, 0, base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=10)


@pytest.mark.parametrize(
    "attempt_number,max_retries,expected",
    [
        (1, 5, False),
        (5, 5, False),
        (6, 5, True),
        (1, 0, True),  # max_retries=0 means the first failure is terminal
    ],
)
def test_should_dead_letter(attempt_number, max_retries, expected):
    assert should_dead_letter(attempt_number, max_retries) is expected
