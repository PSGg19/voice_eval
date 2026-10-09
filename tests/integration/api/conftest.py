"""Fixtures for V3 API tests: a real app over the throwaway migrated DB,
with a fake executor — no Twilio, no Deepgram, no LLM, ever."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from judge_it.api import install_v3
from judge_it.core.config import Settings
from judge_it.db.models import Run


def make_settings(**over) -> Settings:
    """Explicit kwargs only — immune to whatever the developer's shell exports."""
    base = dict(
        twilio_account_sid="ACtest",
        twilio_auth_token="token",
        twilio_phone_number="+15550001111",
        deepgram_api_key="dg",
        osv_base_url="https://llm-studio.judge_it.dev/us/api/interface",
        text_runtime_base_url="https://llm-studio.judge_it.dev/us/api/text",
        sip_destination_host="",
        sip_trunk_number="",
        sip_destination_host_us="",
        sip_trunk_number_us="",
        sip_provider_config_us="",
        sip_destination_host_in="",
        sip_trunk_number_in="",
        sip_provider_config_in="",
        sip_destination_host_ca="",
        sip_trunk_number_ca="",
        sip_provider_config_ca="",
        api_token="",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


@dataclass
class FakeExecutor:
    """Marks runs completed and remembers which ones it saw."""

    engine: object
    executed: list[uuid.UUID] = field(default_factory=list)

    async def execute(self, run_uuid: uuid.UUID) -> None:
        self.executed.append(run_uuid)
        with Session(self.engine) as session:
            run = session.execute(
                select(Run).where(Run.run_uuid == run_uuid)
            ).scalar_one()
            run.status = "completed"
            run.passed = True
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.finished_at = datetime.now(timezone.utc)
            session.commit()


@dataclass
class Api:
    client: TestClient
    executor: FakeExecutor
    engine: object
    settings: Settings


def build_api(migrated_engine, **settings_over) -> Api:
    settings = make_settings(**settings_over)
    app = FastAPI()
    executor = FakeExecutor(engine=migrated_engine)
    install_v3(app, settings=settings, engine=migrated_engine, executor=executor)
    return Api(
        client=TestClient(app),
        executor=executor,
        engine=migrated_engine,
        settings=settings,
    )


@pytest.fixture()
def api(migrated_engine) -> Api:
    return build_api(migrated_engine)
