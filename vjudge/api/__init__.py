"""V3 run API (P3.3–P3.5, P3.7): runs as payloads, Postgres as the record.

`install_v3(app, …)` is the one entry point — it wires state, error envelope
and routes onto an existing FastAPI app (the same app that serves Twilio
webhooks and /v1/jobs, so the V3 surface costs no extra process).
"""
from vjudge.api.state import install_v3  # noqa: F401
