"""Probe Twilio's concurrent-call cap for our account.

Phase 2 Task 0: determine the real concurrent-call ceiling before
designing the campaign worker pool.

Twilio does not expose the cap via API — the only way to measure it
is to fire N parallel create_call requests and count which hit error
21220 ("concurrent calls capacity exceeded"). This script does exactly
that with minimal side effects:

  * Target: Resurgent's bot (same number used in A-001 live tests).
    Brief calls; no conversation.
  * TwiML: 3-second pause + hangup. Long enough that all N calls
    overlap (needed to actually trigger the concurrent state), short
    enough that total cost is well under $0.01.
  * Method: asyncio.gather of N parallel client.calls.create requests
    (wrapped in to_thread because Twilio's Python SDK is sync).
  * Output: count of successes vs failures, with error 21220 specifically
    flagged as cap-hit.

Cost estimate: N * 3s * ($0.013/min) ≈ $0.0065 for N=10. Negligible.

Usage:
    python scripts/probe_twilio_concurrency.py            # N=10 default
    python scripts/probe_twilio_concurrency.py 5          # N=5
    python scripts/probe_twilio_concurrency.py 10 +1...   # custom target
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from twilio.rest import Client

from judge_it.core.config import Settings

# Default target = the Resurgent bot number we already use in A-001.
# Brief calls (3s) won't disrupt their bot but DO consume concurrent
# slots in our Twilio account, which is the whole point of the probe.
DEFAULT_TARGET = "+18445661376"

# 3-second pause keeps the call active long enough for all N parallel
# create requests to be "in flight" simultaneously, ensuring we actually
# hit the concurrent state. Without the pause the calls would create +
# hang up too quickly to overlap.
TWIML = "<Response><Pause length='3'/><Hangup/></Response>"

CAP_ERROR_CODE = 21220  # "Account is not allowed to make calls due to concurrent calls capacity"


async def fire_one(
    client: Client, from_number: str, to_number: str, idx: int
) -> dict[str, Any]:
    """Fire one create_call. Return result dict (never raises)."""
    t0 = time.monotonic()
    try:
        call = await asyncio.to_thread(
            client.calls.create,
            from_=from_number,
            to=to_number,
            twiml=TWIML,
        )
        return {
            "idx": idx,
            "ok": True,
            "sid": call.sid,
            "elapsed_ms": (time.monotonic() - t0) * 1000,
        }
    except Exception as exc:
        # Twilio exceptions carry .code; non-Twilio exceptions don't.
        code = getattr(exc, "code", None)
        return {
            "idx": idx,
            "ok": False,
            "code": code,
            "msg": str(exc)[:200],
            "elapsed_ms": (time.monotonic() - t0) * 1000,
        }


async def main(n: int, target: str) -> None:
    settings = Settings.from_env()
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    from_number = settings.twilio_phone_number

    print(f"Probing concurrent-call cap with N={n}")
    print(f"  From: {from_number}")
    print(f"  To:   {target}")
    print(f"  TwiML: 3s pause + hangup (per call)")
    print(f"  Estimated cost: ~${n * 3 * (0.013 / 60):.3f}")
    print()

    t0 = time.monotonic()
    results = await asyncio.gather(
        *[fire_one(client, from_number, target, i) for i in range(n)]
    )
    elapsed_total = (time.monotonic() - t0) * 1000

    succ = [r for r in results if r["ok"]]
    err = [r for r in results if not r["ok"]]

    print(f"All {n} create_call requests settled in {elapsed_total:.0f}ms")
    print(f"  Succeeded: {len(succ)} / {n}")
    print(f"  Failed:    {len(err)} / {n}")
    print()

    if succ:
        print("Successful calls (will hang up automatically after TwiML pause):")
        for r in succ:
            print(f"  [{r['idx']:2}] sid={r['sid']}  ({r['elapsed_ms']:.0f}ms)")
        print()

    if err:
        print("Failures:")
        for r in err:
            tag = "  ← CONCURRENT CAP HIT" if r["code"] == CAP_ERROR_CODE else ""
            print(f"  [{r['idx']:2}] code={r['code']}{tag}")
            print(f"        {r['msg']}")
        print()

    cap_hits = sum(1 for r in err if r["code"] == CAP_ERROR_CODE)
    print("=" * 60)
    if cap_hits > 0:
        # Account cap is at minimum the number of successes (those got
        # through), but could be exactly len(succ) if the rest all
        # tried to claim slots after the cap was full.
        print(f"VERDICT: Concurrent cap = {len(succ)}")
        print(f"  {cap_hits} call(s) rejected with error 21220.")
        print(f"  Set campaign max_concurrency ≤ {len(succ)}.")
    elif len(err) > 0:
        # Failures but no 21220 — different problem, possibly Resurgent-side
        print(f"VERDICT: Inconclusive. {len(err)} failure(s) but no 21220 cap error.")
        print(f"  Failures are likely target-number / network issues, not Twilio cap.")
        print(f"  Cap is ≥{len(succ)}; re-run with higher N if needed.")
    else:
        print(f"VERDICT: All {n} concurrent calls accepted — cap is ≥{n}.")
        print(f"  Re-run with higher N to find the ceiling, or trust this number.")
    print("=" * 60)


if __name__ == "__main__":
    n_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    target_arg = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TARGET
    asyncio.run(main(n_arg, target_arg))
