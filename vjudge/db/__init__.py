"""V3 store (P3.1): Postgres as the record.

Scenarios, variable sets, targets and runs live here; a run is created by API
payload and is fully explicable from its own row a month later (snapshots are
copied on at creation, never joined back to mutable rows). SQLite remains only
in the legacy V1/V2 result store until P3.3 switches the write path.
"""
