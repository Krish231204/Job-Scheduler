from datetime import datetime, timedelta, timezone

from app.services.job_service import compute_initial_next_run


def test_recurring_job_next_run_is_in_the_future():
    next_run = compute_initial_next_run("*/5 * * * *", None, is_recurring=True)
    assert next_run > datetime.now(timezone.utc)
    assert next_run <= datetime.now(timezone.utc) + timedelta(minutes=5, seconds=5)


def test_one_off_scheduled_job_uses_given_run_at():
    run_at = datetime.now(timezone.utc) + timedelta(hours=3)
    next_run = compute_initial_next_run(None, run_at, is_recurring=False)
    assert next_run == run_at


def test_one_off_without_run_at_falls_back_to_now():
    before = datetime.now(timezone.utc)
    next_run = compute_initial_next_run(None, None, is_recurring=False)
    after = datetime.now(timezone.utc)
    assert before <= next_run <= after
