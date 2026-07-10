"""Pure functions for computing retry delays. Kept dependency-free and
side-effect-free so they're trivially unit-testable (see tests/test_retry.py).
"""
from app.models import RetryStrategy


def compute_retry_delay_seconds(
    strategy: RetryStrategy,
    attempt_number: int,
    base_delay_seconds: float,
    multiplier: float,
    max_delay_seconds: float,
) -> float:
    """attempt_number is 1-indexed: this is the delay to apply *before* the
    next attempt, given that `attempt_number` attempts have already failed.
    """
    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")

    if strategy == RetryStrategy.FIXED:
        delay = base_delay_seconds
    elif strategy == RetryStrategy.LINEAR:
        delay = base_delay_seconds * attempt_number
    elif strategy == RetryStrategy.EXPONENTIAL:
        delay = base_delay_seconds * (multiplier ** (attempt_number - 1))
    else:
        raise ValueError(f"Unknown retry strategy: {strategy}")

    return min(delay, max_delay_seconds)


def should_dead_letter(attempt_number: int, max_retries: int) -> bool:
    """Job goes to the DLQ once it has exhausted max_retries *additional*
    attempts beyond the first. attempt_number counts completed attempts.
    """
    return attempt_number > max_retries
