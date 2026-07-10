"""Pluggable job handler registry.

Real deployments register a handler per job `name` (e.g. "send_email",
"generate_report"). This skeleton ships a default handler used for demo /
load-testing: it "executes" by sleeping for `payload.duration_seconds`
(default 0.2s) and can be told to fail via `payload.simulate = "fail"` or
raise a timeout via `payload.simulate = "hang"`, which is useful for
exercising the retry/DLQ and graceful-shutdown paths in tests and demos.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable

JobHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, JobHandler] = {}


def register(name: str):
    def decorator(fn: JobHandler) -> JobHandler:
        _REGISTRY[name] = fn
        return fn
    return decorator


async def default_handler(payload: dict[str, Any]) -> dict[str, Any]:
    duration = float(payload.get("duration_seconds", 0.2))
    simulate = payload.get("simulate")

    await asyncio.sleep(duration)

    if simulate == "fail":
        raise RuntimeError(payload.get("error_message", "Simulated job failure"))
    if simulate == "flaky" and random.random() < float(payload.get("fail_probability", 0.5)):
        raise RuntimeError("Simulated flaky failure")

    return {"echo": payload, "duration_seconds": duration}


def get_handler(name: str) -> JobHandler:
    return _REGISTRY.get(name, default_handler)
