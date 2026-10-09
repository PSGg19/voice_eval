"""P4 — distributed execution: Redis Streams stages, leases, budgets.

The Postgres row is the record; the queue is dispatch, never the record. A
stream entry carries just enough to find the row (run_uuid + created_at, the
partitioned PK) plus routing metadata (lane, attempt). Losing Redis loses no
runs — rows in status 'queued' are re-dispatched when it returns.

Layout:

* `streams.py`  — StageQueue: one bounded stream per (stage, lane), consumer
                  groups, claim/ack/dead-letter plumbing (P4.1)
* `leases.py`   — heartbeats + the reaper that reclaims dead workers' runs
                  and dead-letters repeat offenders (P4.2)
* `worker.py`   — the worker loop: claim → execute stage → ack → enqueue the
                  next stage; SIGTERM drains (P4.7)
* `admission.py`— the real admission gate: fleet/per-client/per-assistant
                  budgets, Redis-hash live tuning, kill switch (P4.4)
* `fairness.py` — water-filling across suites + per-minute spread (P4.5)
"""
