#!/usr/bin/env python3
"""Reconstruct how many calls were *actually* concurrent from call logs.

For each per-call debug.log it finds the media-stream active window
(connect -> end), then sweeps all windows to report:
  * how many calls connected at all,
  * the peak number simultaneously active,
  * a second-by-second "active count" timeline (so you can watch it
    climb 1,2,3...N and see whether it ever reaches your -m target).

Usage:
    # point at a run's scenario log root (or any dir containing debug.logs)
    python scripts/concurrency_report.py logs/clients/load-test/.generated/scenarios
    python scripts/concurrency_report.py <dir> --since 2026-06-03T1018
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

# Log line timestamps look like: "2026-06-03 15:48:22,944 INFO ..."
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
CONNECT = "Twilio stream connected"
# End markers in priority order — first one found wins.
END_MARKERS = [
    "Conversation ended — signaling runner",
    "Call hung up",
    "Twilio stream stopped",
]


def _ts(line: str):
    m = TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f")


def _window(log: Path):
    """Return (connect_dt, end_dt) for one call, or None if never connected."""
    connect = end = last = None
    with log.open(errors="ignore") as f:
        for line in f:
            dt = _ts(line)
            if dt is None:
                continue
            last = dt
            if connect is None and CONNECT in line:
                connect = dt
            elif connect is not None and end is None:
                if any(m in line for m in END_MARKERS):
                    end = dt
    if connect is None:
        return None
    return connect, (end or last or connect)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", help="Dir to scan recursively for debug.log files.")
    ap.add_argument(
        "--since",
        default="",
        help="Only include calls whose folder/path contains this string "
        "(e.g. a run timestamp prefix like 2026-06-03T1018).",
    )
    ap.add_argument(
        "--step", type=float, default=1.0, help="Timeline sample step (seconds)."
    )
    args = ap.parse_args()

    root = Path(args.root)
    logs = [
        p
        for p in root.rglob("debug.log")
        if (not args.since or args.since in str(p))
    ]
    if not logs:
        raise SystemExit(f"no debug.log files under {root} (since={args.since!r})")

    windows = []
    never_connected = 0
    for log in logs:
        w = _window(log)
        if w is None:
            never_connected += 1
        else:
            windows.append(w)

    total = len(logs)
    connected = len(windows)
    print(f"call logs scanned:     {total}")
    print(f"connected a stream:    {connected}")
    print(f"never connected:       {never_connected}")
    if not windows:
        return

    # Sweep-line peak concurrency: +1 at each connect, -1 at each end.
    events = []
    for c, e in windows:
        events.append((c, 1))
        events.append((e, -1))
    events.sort(key=lambda x: (x[0], -x[1]))  # process connects before ends at a tie
    cur = peak = 0
    peak_at = None
    for dt, delta in events:
        cur += delta
        if cur > peak:
            peak, peak_at = cur, dt
    print(f"\nPEAK concurrent calls: {peak}   (at {peak_at})")

    # Timeline: active count sampled every `step` seconds.
    start = min(c for c, _ in windows)
    finish = max(e for _, e in windows)
    span = (finish - start).total_seconds()
    print(
        f"window: {start.strftime('%H:%M:%S')} -> {finish.strftime('%H:%M:%S')} "
        f"({span:.0f}s)\n"
    )
    print("t(s)  active  bar")
    t = 0.0
    while t <= span + args.step:
        moment = start.timestamp() + t
        active = sum(
            1 for c, e in windows if c.timestamp() <= moment < e.timestamp()
        )
        print(f"{t:4.0f}  {active:5d}  {'█' * active}")
        t += args.step


if __name__ == "__main__":
    main()