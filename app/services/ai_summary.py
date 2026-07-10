"""AI-generated failure summaries for dead-lettered jobs (assignment bonus
feature).

Uses the Anthropic API when `ANTHROPIC_API_KEY` is configured; falls back to
a small rule-based heuristic otherwise, or if the API call itself fails for
any reason (network, rate limit, bad key). This means the feature is always
available for a demo -- it never requires a key to be set, and never turns
an AI provider outage into a broken dashboard page.
"""
import logging

from app.config import get_settings

logger = logging.getLogger("codity.ai_summary")
settings = get_settings()

_RULE_BASED_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("timeout", "timed out"),
        "This looks like a timeout -- check whether the handler is waiting on a slow external dependency.",
    ),
    (
        ("connection", "connect", "refused", "unreachable"),
        "This looks like a connectivity failure -- check that any downstream "
        "service or database the handler depends on is reachable.",
    ),
    (
        ("permission", "forbidden", "unauthorized", "401", "403"),
        "This looks like an authorization failure in the handler's downstream call, not a scheduler issue.",
    ),
    (
        ("valueerror", "keyerror", "typeerror", "attributeerror"),
        "This looks like a bug in the job handler itself (a Python exception "
        "on bad or missing data), not an infrastructure issue.",
    ),
]


def _rule_based_summary(job_name: str, attempt_count: int, errors: list[str]) -> str:
    last_error = errors[-1] if errors else "no error message was recorded"
    lowered = last_error.lower()
    for keywords, hint in _RULE_BASED_HINTS:
        if any(k in lowered for k in keywords):
            return f"Job '{job_name}' failed after {attempt_count} attempt(s). Last error: {last_error}. {hint}"
    return f"Job '{job_name}' failed after {attempt_count} attempt(s). Last error: {last_error}."


async def summarize_failure(job_name: str, attempt_count: int, errors: list[str]) -> str:
    """Best-effort plain-English summary of why a job dead-lettered."""
    if not settings.anthropic_api_key:
        return _rule_based_summary(job_name, attempt_count, errors)

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        error_text = "\n".join(errors[-5:]) or "no error message was recorded"
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"A background job named '{job_name}' failed {attempt_count} time(s) and was "
                        f"moved to a dead-letter queue. Its recent error message(s):\n{error_text}\n\n"
                        "In 2-3 plain-English sentences, summarize the likely root cause and suggest one "
                        "concrete next step for whoever is debugging it. No preamble, no markdown."
                    ),
                }
            ],
        )
        return message.content[0].text.strip()
    except Exception:
        logger.exception("AI failure summary generation failed; falling back to rule-based summary")
        return _rule_based_summary(job_name, attempt_count, errors)
