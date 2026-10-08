"""Region substitution for OSV URLs.

The region is a **path segment**, not a query parameter or a header:
`https://llm-studio.vjudge.dev/us/api/interface`. Assistants do not exist across
regions — asking the US gateway for an India assistant returns
`404 No assistant found for request.assistant_uuid=… and +91…`, which reads
like a bad uuid rather than a misroute. That cost a debugging session once.

Lives here rather than on the transport because two different base URLs need
the same treatment (`text_runtime_base_url` and `osv_base_url`) and they must
agree — otherwise a run talks to the India runtime and then looks its own
interaction up in the US.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

ALLOWED_REGIONS = ("us", "in", "ca")

_REGION_SEGMENT = re.compile(r"(?<=/)(us|in|ca)(?=/)")


# Callers reach apply_region from a *property* (`TextTransport.base_url`), so it
# runs on every turn and every retry. Logging unconditionally printed the same
# line five times in a three-turn run — the useful signal is "which region did
# this run use", which is worth saying exactly once. Keyed by the decision, not
# by call site, so a genuinely different substitution still gets logged.
_ANNOUNCED: set[tuple[str, str, str]] = set()


def reset_announcements() -> None:
    """Forget what has been logged. For tests, and for long-lived processes
    (`vjudge serve`) that run many scenarios and should re-announce per run."""
    _ANNOUNCED.clear()


def apply_region(url: str, region: Optional[str], *, what: str = "url") -> str:
    """Swap the region path segment in `url`. No region → unchanged."""
    url = url.rstrip("/")
    if not region:
        return url
    swapped, n = _REGION_SEGMENT.subn(region, url, count=1)
    key = (region, what, url)
    announce = key not in _ANNOUNCED
    if announce:
        _ANNOUNCED.add(key)
    if not n:
        if announce:
            logger.warning(
                "scenario pins region=%s but %s %r has no recognisable region "
                "path segment — using it unchanged",
                region, what, url,
            )
        return url
    if swapped != url and announce:
        logger.info(
            "scenario pins region=%s for %s (deployment default is %r)",
            region, what, url,
        )
    return swapped
