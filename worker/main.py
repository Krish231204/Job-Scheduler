"""Worker process entrypoint.

Usage:
    python -m worker.main --concurrency 8

Sends SIGTERM/SIGINT into a graceful shutdown: the worker stops claiming new
jobs, waits for in-flight jobs to finish (Kubernetes' default grace period is
30s, so keep job handlers under that or raise `terminationGracePeriodSeconds`),
then deregisters and exits.
"""
import argparse
import asyncio
import logging
import signal

from worker.runner import WorkerRunner

# Without this, the root logger defaults to WARNING with no handler, so every
# logger.info(...) call in worker/runner.py (registration, claims, completions,
# failures) is silently dropped -- the process runs fine, you just can't see
# it. app/main.py and scheduler/main.py both set this up already; this file
# was the one place it got missed.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("jobsched.worker")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Job scheduler worker process")
    parser.add_argument("--concurrency", type=int, default=None, help="Max concurrent jobs this process runs")
    args = parser.parse_args()

    runner = WorkerRunner(concurrency=args.concurrency)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runner.request_shutdown)

    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
