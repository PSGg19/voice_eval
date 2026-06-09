"""Test runner — orchestrates calls and collects results."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .call import CallManager
from .config import Settings
from .flow import FlowEngine
from .models import CallResult, CallStatus, Flow
from .server import register_session
from .store import ResultStore
from .stream import MediaStreamSession
from .tts import DeepgramTTS

logger = logging.getLogger(__name__)


def load_flow(path: str | Path) -> Flow:
    """Load a flow definition from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return Flow(**data)


class TestRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.call_mgr = CallManager(settings)
        self.tts = DeepgramTTS(api_key=settings.deepgram_api_key)
        self.store = ResultStore(settings.db_path)

    async def run_flow(self, flow: Flow) -> CallResult:
        """Execute a single test flow: make a call, converse, evaluate."""
        session_id = uuid.uuid4().hex[:12]
        result = CallResult(
            id=session_id,
            flow_name=flow.name,
            target=flow.target,
            started_at=datetime.now(timezone.utc),
        )

        engine = FlowEngine(flow)
        stream_session = MediaStreamSession(
            flow_engine=engine,
            deepgram_api_key=self.settings.deepgram_api_key,
            tts=self.tts,
            result=result,
        )

        completion = asyncio.Event()
        register_session(
            session_id,
            {
                "settings": self.settings,
                "stream_session": stream_session,
                "completion_event": completion,
                "result": result,
            },
        )

        # Initiate the call
        try:
            call_sid = self.call_mgr.make_call(flow.target, session_id)
            result.call_sid = call_sid
            result.status = CallStatus.RINGING
        except Exception as exc:
            result.finish(CallStatus.ERROR, f"Failed to initiate call: {exc}")
            return result

        # Wait for conversation to complete (or timeout)
        max_duration = sum(s.timeout_ms for s in flow.steps) / 1000 + 30  # extra buffer
        try:
            await asyncio.wait_for(completion.wait(), timeout=max_duration)
        except asyncio.TimeoutError:
            logger.warning("Call timed out after %.0fs", max_duration)
            result.finish(CallStatus.ERROR, "Call timed out")
            self.call_mgr.hang_up(call_sid)

        # Collect step results from engine
        result.steps = engine.step_results
        if result.status == CallStatus.IN_PROGRESS:
            result.finish(CallStatus.COMPLETED)

        # Persist
        await self.store.save_result(result)

        return result

    async def run_flow_file(self, path: str | Path) -> CallResult:
        """Load a YAML flow and run it."""
        flow = load_flow(path)
        return await self.run_flow(flow)

    async def run_batch(self, paths: list[str | Path]) -> list[CallResult]:
        """Run multiple flows sequentially."""
        results = []
        for p in paths:
            result = await self.run_flow_file(p)
            results.append(result)
        return results

    async def run_repeated(
        self, flow: Flow, count: int = 0, interval_s: float = 60
    ) -> list[CallResult]:
        """Run a flow repeatedly. count=0 means forever."""
        results = []
        iteration = 0
        while count == 0 or iteration < count:
            iteration += 1
            logger.info("--- Run %d ---", iteration)
            result = await self.run_flow(flow)
            results.append(result)
            if count == 0 or iteration < count:
                logger.info("Waiting %.0fs before next run...", interval_s)
                await asyncio.sleep(interval_s)
        return results

    async def close(self):
        await self.tts.close()
