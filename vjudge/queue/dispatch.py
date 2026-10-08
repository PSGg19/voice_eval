"""P4.1 — dispatch: the Executor that enqueues instead of executing.

`QueueExecutor` satisfies the same Protocol the API has always dispatched
through, but `execute` now means "mark the row queued and XADD one entry".
The row-first rule is structural: the router only calls an executor for a run
`create_run` already committed.

Redis-down semantics (the PRD's asymmetric acceptance): admission never
fails on Redis. The row is committed as 'queued' FIRST; if the XADD then
fails, the run simply stays 'queued' with no stream entry, the API keeps
accepting, and `redispatch_stuck` — a periodic sweep on the API process —
re-enqueues every 'queued' row older than a grace window once Redis returns.
The sweep can produce a duplicate entry for a run whose first XADD DID land;
that is deliberate slack, and the worker's lease CAS (P4.2) collapses
duplicates to one execution.
"""
from __future__ import annotations

import logging
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vjudge.db.models import Run
from vjudge.queue.streams import StageQueue

logger = logging.getLogger(__name__)

# 'queued' rows older than this with (presumably) no live entry are re-sent.
# Generous: a healthy dispatch is milliseconds; only a Redis outage leaves
# strays, and re-sending too eagerly just makes harmless duplicates.
REDISPATCH_AFTER_S = 60.0


def stage_for_transport(transport: str) -> str:
    """Voice transports converse on the API process this phase (P4 'not in
    this phase'); text fans out to workers. Same stream mechanics either way."""
    return "converse-text" if transport == "text" else "converse-voice"


def lane_for_run(run: Run) -> str:
    """P4.6: a single POSTed run is a person waiting — interactive lane; a
    batch member is throughput work — batch lane."""
    return "batch" if run.batch_id is not None else "interactive"


class QueueExecutor:
    """Executor Protocol implementation that dispatches to Redis Streams.

    `dispatcher` (P4.5, optional): when present, BATCH members park in the
    fair dispatcher's per-suite pool and reach the stream via water-filled,
    token-paced pump cycles; interactive runs always go straight to their
    stream — a person is waiting.
    """

    def __init__(self, *, engine: Engine, queue: StageQueue, dispatcher=None):
        self.engine = engine
        self.queue = queue
        self.dispatcher = dispatcher

    async def execute(self, run_uuid: uuid_mod.UUID) -> None:
        from vjudge.db.models import Scenario as ScenarioRow

        with Session(self.engine) as session:
            run = session.execute(
                select(Run).where(Run.run_uuid == run_uuid)
            ).scalar_one_or_none()
            if run is None:  # pragma: no cover — row committed before dispatch
                logger.error("dispatch: run %s not found", run_uuid)
                return
            stage = stage_for_transport(run.transport)
            lane = lane_for_run(run)
            created_at = run.created_at
            batch_id = run.batch_id
            body: dict = {}
            if self.dispatcher is not None and batch_id is not None:
                row = session.get(ScenarioRow, run.scenario_id)
                body = dict(row.body) if row is not None else {}
            # The row is the record: 'queued' is committed BEFORE the XADD so
            # a Redis failure leaves a truthful row, not a lost run.
            run.status = "queued"
            session.commit()

        try:
            if self.dispatcher is not None and batch_id is not None:
                from vjudge.queue.admission import budget_keys

                _client, assistant = budget_keys(body)
                await self.dispatcher.submit(
                    assistant=assistant, batch=str(batch_id), stage=stage,
                    run_uuid=run_uuid, created_at=created_at.isoformat(),
                    lane=lane,
                )
            else:
                await self.queue.enqueue(
                    stage, run_uuid=run_uuid, created_at=created_at, lane=lane
                )
        except Exception as exc:
            logger.warning(
                "dispatch deferred: Redis enqueue failed for run %s (%s) — row "
                "stays 'queued'; redispatch_stuck will re-send when Redis returns",
                run_uuid, exc,
            )


async def redispatch_stuck(engine: Engine, queue: StageQueue) -> int:
    """Re-enqueue 'queued' rows that have sat past the grace window.

    Returns how many were re-sent. Runs periodically on the API process; a
    failure here is logged and retried next cycle — never raised into the
    request path.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=REDISPATCH_AFTER_S)
    with Session(engine) as session:
        rows = session.execute(
            select(Run.run_uuid, Run.created_at, Run.transport, Run.batch_id)
            .where(Run.status == "queued", Run.created_at < cutoff)
            .limit(500)
        ).all()

    sent = 0
    for run_uuid, created_at, transport, batch_id in rows:
        try:
            await queue.enqueue(
                stage_for_transport(transport),
                run_uuid=run_uuid,
                created_at=created_at,
                lane="batch" if batch_id is not None else "interactive",
            )
            sent += 1
        except Exception as exc:
            logger.warning("redispatch: enqueue failed for %s (%s)", run_uuid, exc)
            break  # Redis is still down; try again next cycle
    if sent:
        logger.info("redispatch: re-sent %d stuck run(s)", sent)
    return sent
