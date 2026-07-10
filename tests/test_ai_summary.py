"""Tests for the AI failure-summary fallback path.

No ANTHROPIC_API_KEY is set in the test environment, so summarize_failure()
always exercises the rule-based branch here -- that's intentional, it keeps
this test free of network calls and API costs while still locking in the
one guarantee that matters: this feature never raises and never blocks the
dashboard, regardless of whether a real AI key is configured.
"""
from app.services.ai_summary import summarize_failure


async def test_falls_back_to_rule_based_summary_without_api_key():
    summary = await summarize_failure("send-email", 3, ["ConnectionError: [Errno 61] Connection refused"])
    assert "send-email" in summary
    assert "3 attempt" in summary
    assert "connectivity" in summary.lower()


async def test_rule_based_summary_handles_no_error_messages():
    summary = await summarize_failure("cleanup", 1, [])
    assert "cleanup" in summary
    assert "no error message was recorded" in summary


async def test_rule_based_summary_detects_handler_bug_pattern():
    summary = await summarize_failure("import-row", 2, ["KeyError: 'row_id'"])
    assert "bug in the job handler" in summary.lower()
