"""App wiring for the V3 API: one state object, explicit dependencies.

`install_v3` is called once per app — by transport/server.py for the real
process, by tests with their own Settings/engine/executor. Nothing in the
routers reads ambient config: everything arrives through `V3State`, which is
the API-side expression of the RunContext rule (P1.1) — swap the state, swap
the world, no module globals to patch.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from vjudge.api.errors import api_error, install_error_envelope
from vjudge.core.config import Settings
from vjudge.db.engine import sync_engine, sync_session_factory


@dataclass
class V3State:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker
    executor: "object"  # vjudge.api.executor.Executor (Protocol)
    # P4.4 — present only when VJUDGE_ADMISSION_ENABLED=true (env-gated
    # existence); None means the historical no-op gate.
    admission_gate: "object | None" = None


def install_v3(
    app: FastAPI,
    *,
    settings: Optional[Settings] = None,
    engine: Optional[Engine] = None,
    executor: Optional[object] = None,
) -> V3State:
    """Wire the V3 API onto `app`. Every collaborator is injectable; the
    defaults are the production ones."""
    settings = settings or Settings.from_env()
    engine = engine or sync_engine(settings.database_url)
    if executor is None:
        # P4 exit criterion: the in-process BackgroundTasks execution path is
        # deleted. The default executor DISPATCHES (row → 'queued' + one
        # stream entry); StageWorkers do the running — dedicated `vjudge
        # worker` processes, and/or the embedded worker this function starts
        # below when queue_enabled (voice always runs there this phase).
        from vjudge.queue.dispatch import QueueExecutor
        from vjudge.queue.streams import StageQueue, connect

        executor = QueueExecutor(
            engine=engine, queue=StageQueue(connect(settings.redis_url))
        )

    admission_gate = None
    if settings.admission_enabled:
        import redis as sync_redis

        from vjudge.queue.admission import AdmissionGate

        admission_gate = AdmissionGate(
            sync_redis.from_url(settings.redis_url, decode_responses=True),
            enabled=True,
        )

    state = V3State(
        settings=settings,
        engine=engine,
        session_factory=sync_session_factory(engine),
        executor=executor,
        admission_gate=admission_gate,
    )
    app.state.v3 = state
    install_error_envelope(app)

    from vjudge.api.router import router

    app.include_router(router)
    if settings.queue_enabled:
        _install_queue_services(app, state)
    return state


def _install_queue_services(app: FastAPI, state: V3State) -> None:
    """The API process's embedded StageWorker + housekeeping (queue_enabled).

    Voice converses HERE this phase — per-worker addressing for media
    streams is Phase 6 — and consuming every stage as well makes a
    single-process deployment complete without a separate `vjudge worker`.
    Shutdown drains: no new claims, in-flight finishes (bounded by the drain
    deadline), housekeeping stops last.
    """
    import asyncio
    import uuid as uuid_mod

    services: dict = {}

    async def _start() -> None:
        from vjudge.queue.streams import StageQueue, connect
        from vjudge.queue.worker import StageWorker, housekeeping

        settings = state.settings
        queue = StageQueue(connect(settings.redis_url))
        try:
            await queue.ensure_groups()
        except Exception as exc:
            # Redis down at boot: the API must still serve (admission keeps
            # accepting; rows wait as 'queued'). Services stay off until a
            # restart — the degraded state is logged, not fatal.
            import logging

            logging.getLogger(__name__).error(
                "queue services NOT started (Redis unreachable: %s) — runs "
                "will queue in Postgres until a worker can dispatch them", exc
            )
            return
        worker = StageWorker(
            settings=settings, engine=state.engine, queue=queue,
            consumer=f"api-{uuid_mod.uuid4().hex[:6]}",
            stages=("converse-voice", "converse-text", "evaluate", "settle"),
        )
        stop = asyncio.Event()
        services["worker"] = worker
        services["stop"] = stop
        services["loop"] = asyncio.create_task(worker.run_forever(
            slots=settings.worker_slots,
            interactive_reserve=settings.worker_interactive_reserve,
        ))
        services["housekeeper"] = asyncio.create_task(housekeeping(
            queue, state.engine,
            lease_s=settings.queue_lease_s,
            max_attempts=settings.queue_max_attempts,
            interval_s=max(settings.queue_lease_s / 2, 1.0),
            stop=stop,
        ))

    async def _stop() -> None:
        worker = services.get("worker")
        if worker is None:
            return
        worker.request_stop()
        try:
            await asyncio.wait_for(
                services["loop"], timeout=state.settings.queue_drain_deadline_s
            )
        except (asyncio.TimeoutError, Exception):
            services["loop"].cancel()
        services["stop"].set()
        try:
            await asyncio.wait_for(services["housekeeper"], timeout=5)
        except (asyncio.TimeoutError, Exception):
            services["housekeeper"].cancel()

    # Starlette 1.0 removed add_event_handler (the startup/shutdown event API
    # is gone — lifespan only), and the defensive mount in transport/server.py
    # turned that AttributeError into a one-line warning while the routes kept
    # working: the API answered but nothing consumed converse-voice. Wrap the
    # app's existing lifespan instead; keep the old path for older Starlette.
    if hasattr(app, "add_event_handler"):
        app.add_event_handler("startup", _start)
        app.add_event_handler("shutdown", _stop)
    else:
        from contextlib import asynccontextmanager

        existing = app.router.lifespan_context

        @asynccontextmanager
        async def _lifespan(app_):
            async with existing(app_):
                await _start()
                try:
                    yield
                finally:
                    await _stop()

        app.router.lifespan_context = _lifespan


def get_state(request: Request) -> V3State:
    return request.app.state.v3


def get_session(state: V3State = Depends(get_state)) -> Iterator[Session]:
    with state.session_factory() as session:
        yield session


def require_auth(request: Request, state: V3State = Depends(get_state)) -> None:
    """Bearer auth (llm-studio convention). A blank VJUDGE_API_TOKEN turns
    auth off — acceptable only while the server is localhost-only."""
    expected = state.settings.api_token
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
        raise api_error(401, "unauthorized", "missing or invalid bearer token")
