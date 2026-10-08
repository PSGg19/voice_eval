"""The reporting end of the job pipeline: what gets logged, and waiting for
the disposition that the post-call worker writes after we have already looked.
"""

import pytest

from vjudge.jobs import pipeline
from vjudge.jobs.models import Job, JobConfig, JobResultItem, JobStatus


def _job(**kw) -> Job:
    return Job(
        job_id="job_test",
        status=JobStatus.COMPLETED,
        config=JobConfig(target="+18883286947"),
        calls=["10ac18ab-97dd-40a1-82c9-5615fec1a126"],
        **kw,
    )


def _item(**kw) -> JobResultItem:
    base = dict(
        scenario_id="S01-10ac18ab",
        vjudge_session_id="59febbada75644c9af5c9219638e2ab6",
        source_interaction_uuid="10ac18ab-97dd-40a1-82c9-5615fec1a126",
        title="Interest waiver request triggers agent deflection",
        call_status="completed",
        bot_turns=8,
        caller_turns=8,
    )
    base.update(kw)
    return JobResultItem(**base)


# ------------------------------------------------------------------- formatting


def test_format_results_shows_the_fields_an_operator_reads():
    job = _job(
        eval_enabled=False,
        results=[_item(disposition="agent_unavailable_hangup", call_link="https://x/y")],
    )
    out = pipeline._format_results(job)

    assert "S01-10ac18ab" in out
    assert "Interest waiver request triggers agent deflection" in out
    assert "agent_unavailable_hangup" in out
    assert "turns bot=8 caller=8" in out
    assert "https://x/y" in out
    assert "eval=off" in out


def test_format_results_calls_out_a_silent_caller():
    """caller_turns=0 means nothing was tested, and `passed` does not catch it."""
    job = _job(eval_enabled=False, results=[_item(bot_turns=6, caller_turns=0)])
    assert "caller never spoke" in pipeline._format_results(job)


def test_format_results_marks_an_uncorrelated_call():
    job = _job(eval_enabled=False, results=[_item(call_link=None)])
    assert "NOT CORRELATED" in pipeline._format_results(job)


def test_format_results_reports_the_eval_verdict_only_when_eval_ran():
    passed = _item(passed=True, overall_reasoning="Bot answered the waiver question.")
    assert "eval=PASS" in pipeline._format_results(_job(eval_enabled=True, results=[passed]))
    # Same item, eval off: `passed` there is just call status, so it must not be
    # rendered as a verdict.
    assert "eval=PASS" not in pipeline._format_results(_job(eval_enabled=False, results=[passed]))


def test_format_results_includes_warnings():
    job = _job(eval_enabled=False, results=[_item()], warnings=["dialled a clone"])
    assert "warning: dialled a clone" in pipeline._format_results(job)


def test_one_line_collapses_and_truncates():
    assert pipeline._one_line("a\n  b   c") == "a b c"
    assert len(pipeline._one_line("x" * 500, limit=20)) == 20


# ---------------------------------------------------------------- disposition


class _FakeOSV:
    """Yields a disposition only on the Nth read, like the post-call worker."""

    def __init__(self, ready_on: int, disposition: str = "bot_initiated_transfer"):
        self.ready_on = ready_on
        self.disposition = disposition
        self.reads = 0

    async def get_interaction_metadata(self, iuuid):
        self.reads += 1
        disp = self.disposition if self.reads >= self.ready_on else None
        return {"uuid": iuuid, "disposition": disp}


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(pipeline, "DISPOSITION_WAIT", 0.0)


@pytest.mark.asyncio
async def test_await_disposition_does_not_re_read_when_already_present():
    osv = _FakeOSV(ready_on=1)
    meta = {"uuid": "f2839c9d", "disposition": "bot_initiated_transfer"}
    assert await pipeline.await_disposition(meta, osv) == meta
    assert osv.reads == 0


@pytest.mark.asyncio
async def test_await_disposition_picks_up_a_late_write():
    osv = _FakeOSV(ready_on=3)
    out = await pipeline.await_disposition({"uuid": "f2839c9d", "disposition": None}, osv)
    assert out["disposition"] == "bot_initiated_transfer"
    assert osv.reads == 3


@pytest.mark.asyncio
async def test_await_disposition_gives_up_and_returns_the_freshest_metadata():
    osv = _FakeOSV(ready_on=99)
    out = await pipeline.await_disposition({"uuid": "f2839c9d", "disposition": None}, osv)
    assert out["disposition"] is None
    assert osv.reads == pipeline.DISPOSITION_ATTEMPTS


@pytest.mark.asyncio
async def test_await_disposition_survives_an_osv_error():
    class _Boom:
        async def get_interaction_metadata(self, iuuid):
            raise RuntimeError("OSV 502")

    meta = {"uuid": "f2839c9d", "disposition": None}
    assert await pipeline.await_disposition(meta, _Boom()) == meta


@pytest.mark.asyncio
async def test_await_disposition_skips_a_match_with_no_uuid():
    osv = _FakeOSV(ready_on=1)
    assert await pipeline.await_disposition({"disposition": None}, osv) == {"disposition": None}
    assert osv.reads == 0
