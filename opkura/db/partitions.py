"""Monthly partition management for `runs` (P3.1).

Partitions are named `runs_yYYYYmMM` and cover one calendar month (UTC).
`ensure_month_partitions` is idempotent (`IF NOT EXISTS`) and always creates
the current month plus `ahead` future months, so the insert path never races
month rollover: call it at API startup and from a daily housekeeping tick, and
the partition for "now" already exists by the time a run row wants it.

Why not a default partition: a DEFAULT partition silently absorbs rows whose
partition is missing, which converts "housekeeping stopped running" from a
loud insert error into a table that quietly grows unpartitioned. The loud
error is the better failure.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

PARTITION_NAME = "runs_y{y:04d}m{m:02d}"


def _month_start(year: int, month: int) -> str:
    # The explicit +00 offset is load-bearing: a bare date literal is cast to
    # timestamptz in the SERVER's TimeZone, so on a server running IST the
    # "UTC months" this module promises silently became IST months — found
    # live at the 2026-08-31→09-01 IST midnight rollover, when a row
    # timestamped Aug 31 UTC landed in the September partition.
    return f"{year:04d}-{month:02d}-01 00:00:00+00"


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def month_partition_ddl(year: int, month: int) -> str:
    ny, nm = _next_month(year, month)
    name = PARTITION_NAME.format(y=year, m=month)
    return (
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF runs "
        f"FOR VALUES FROM ('{_month_start(year, month)}') "
        f"TO ('{_month_start(ny, nm)}')"
    )


def ensure_month_partitions(
    conn: Connection, *, now: datetime | None = None, ahead: int = 1
) -> list[str]:
    """Create the partition for `now`'s month and `ahead` months after it.
    Returns the partition names ensured (created or already present)."""
    moment = now or datetime.now(timezone.utc)
    year, month = moment.year, moment.month
    ensured: list[str] = []
    for _ in range(ahead + 1):
        conn.execute(text(month_partition_ddl(year, month)))
        ensured.append(PARTITION_NAME.format(y=year, m=month))
        year, month = _next_month(year, month)
    return ensured
