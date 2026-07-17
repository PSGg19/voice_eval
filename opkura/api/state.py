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

from opkura.api.errors import api_error, install_error_envelope
from opkura.core.config import Settings
from opkura.db.engine import sync_engine, sync_session_factory


@dataclass
class V3State:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker
    executor: "object"  # opkura.api.executor.Executor (Protocol)
    # P4.4 — present only when AUTOCALLS_ADMISSION_ENABLED=true (env-gated
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
        from opkura.api.executor import LocalRunnerExecutor

        executor = LocalRunnerExecutor(settings=settings, engine=engine)

    admission_gate = None
    if settings.admission_enabled:
        import redis as sync_redis

        from opkura.queue.admission import AdmissionGate

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

    from opkura.api.router import router

    app.include_router(router)
    return state


def get_state(request: Request) -> V3State:
    return request.app.state.v3


def get_session(state: V3State = Depends(get_state)) -> Iterator[Session]:
    with state.session_factory() as session:
        yield session


def require_auth(request: Request, state: V3State = Depends(get_state)) -> None:
    """Bearer auth (llm-studio convention). A blank AUTOCALLS_API_TOKEN turns
    auth off — acceptable only while the server is localhost-only."""
    expected = state.settings.api_token
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
        raise api_error(401, "unauthorized", "missing or invalid bearer token")
