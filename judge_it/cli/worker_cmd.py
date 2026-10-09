"""P4.7 — `judge_it worker`: a stage worker process with graceful drain.

SIGTERM (or SIGINT): stop claiming, let in-flight entries finish, flush, and
exit. Past the drain deadline the loop is cancelled instead — the claimed
entries stay in each stream's pending-entries list with our now-silent
consumer name, and the reaper on any surviving process re-queues them once
the lease window lapses. Either way a restart loses zero runs: rows are the
record, entries are recoverable, leases expire.

The worker also runs the housekeeping loops every deployment needs at least
one of (all idempotent and safe to run on many processes at once): the
reaper, the stuck-dispatch sweep, and the fair-dispatch pump.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import socket
import uuid as uuid_mod
from typing import Optional

import typer

from judge_it.cli.main import _setup_logging, cli_app
from judge_it.core.config import Settings

logger = logging.getLogger(__name__)


def install_signal_handlers(loop: asyncio.AbstractEventLoop, worker) -> None:
    """SIGTERM/SIGINT → drain. Separated from the command for testability."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: (
                logger.warning("received %s — draining (no new claims)", s.name),
                worker.request_stop(),
            ),
        )


async def worker_main(
    settings: Settings,
    *,
    stages: tuple[str, ...],
    consumer: str,
    slots: int,
    interactive_reserve: int,
    install_signals: bool = True,
    runner_factory=None,
) -> None:
    from judge_it.db.engine import sync_engine
    from judge_it.queue.registry import WorkerRegistry
    from judge_it.queue.streams import StageQueue, connect
    from judge_it.queue.worker import StageWorker, housekeeping

    engine = sync_engine(settings.database_url)
    queue = StageQueue(connect(settings.redis_url))
    await queue.ensure_groups()

    # P6.4 — worker-owned dialing. A voice worker serves its OWN TwiML /
    # status / media-stream endpoints and dials with its OWN public URL, so
    # the call lands on the process that holds the session state
    # (transport/server.py's _sessions is worker-local by construction).
    if "converse-voice" in stages:
        from judge_it.cli.main import _start_server

        _start_server(settings, transport="sip")
        logger.info("voice worker serving callbacks at %s", settings.public_url)

    registry = WorkerRegistry(queue.redis)
    await registry.register(
        consumer=consumer, public_url=settings.public_url, stages=stages
    )

    async def _registry_heartbeat() -> None:
        while True:
            await asyncio.sleep(registry.ttl_s / 3)
            if not await registry.heartbeat(consumer):
                await registry.register(
                    consumer=consumer, public_url=settings.public_url,
                    stages=stages,
                )

    worker = StageWorker(
        settings=settings, engine=engine, queue=queue,
        consumer=consumer, stages=stages,
        runner_factory=runner_factory,
    )
    if install_signals:
        install_signal_handlers(asyncio.get_running_loop(), worker)

    stop_housekeeping = asyncio.Event()
    housekeeper = asyncio.create_task(housekeeping(
        queue, engine,
        lease_s=settings.queue_lease_s,
        max_attempts=settings.queue_max_attempts,
        interval_s=max(settings.queue_lease_s / 2, 1.0),
        stop=stop_housekeeping,
    ))
    heartbeat_task = asyncio.create_task(_registry_heartbeat())

    loop_task = asyncio.create_task(worker.run_forever(
        slots=slots, interactive_reserve=interactive_reserve
    ))
    logger.info(
        "worker %s up: stages=%s slots=%d reserve=%d lease=%.0fs",
        consumer, ",".join(stages), slots, interactive_reserve,
        settings.queue_lease_s,
    )
    try:
        # run_forever returns only after request_stop + drain. Enforce the
        # drain deadline from the first stop request.
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(loop_task), timeout=1.0)
                break
            except asyncio.TimeoutError:
                if worker._stopping:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(loop_task),
                            timeout=settings.queue_drain_deadline_s,
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "drain deadline (%.0fs) passed — cancelling; "
                            "claimed entries stay pending and the reaper "
                            "re-queues them after the lease window",
                            settings.queue_drain_deadline_s,
                        )
                        loop_task.cancel()
                    break
    finally:
        stop_housekeeping.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await registry.deregister(consumer)
        except Exception:
            pass
        try:
            await asyncio.wait_for(housekeeper, timeout=5)
        except (asyncio.TimeoutError, Exception):
            housekeeper.cancel()
        for t in (loop_task,):
            if not t.done():
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        logger.info("worker %s exited", consumer)


@cli_app.command()
def worker(
    stages: str = typer.Option(
        "converse-text,evaluate,settle", "--stages",
        help="Comma-separated stages this worker consumes. Voice stays on "
             "the API process this phase.",
    ),
    consumer: Optional[str] = typer.Option(
        None, "--name", help="Consumer name (default: host-pid-suffix)."
    ),
    slots: Optional[int] = typer.Option(None, "--slots"),
    interactive_reserve: Optional[int] = typer.Option(None, "--reserve"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run a queue worker: claim stage entries, execute, heartbeat, drain on
    SIGTERM."""
    _setup_logging(verbose)
    settings = Settings()
    name = consumer or f"{socket.gethostname()}-{uuid_mod.uuid4().hex[:6]}"
    asyncio.run(worker_main(
        settings,
        stages=tuple(s.strip() for s in stages.split(",") if s.strip()),
        consumer=name,
        slots=slots if slots is not None else settings.worker_slots,
        interactive_reserve=(
            interactive_reserve if interactive_reserve is not None
            else settings.worker_interactive_reserve
        ),
    ))
