"""Region → config-bundle resolution: one resolver, both transports (P3.8/P3.9).

A run's region decides two families of config that must agree:

* number/text mode — the OSV base URLs, where region is a PATH SEGMENT
  (`https://llm-studio.judge_it.dev/us/api/…`); already handled by `apply_region`.
* SIP-to-SIP mode — WHICH LiveKit ingress the INVITE lands on. Each region is
  a different LiveKit project with its own SIP host and inbound trunk number
  (US: `581mqcm8zvu…` trunk 1205; India: the `livekit-judge_ittl-2` project). The
  target DID always travels as the `X-VirtualNumber` header, never as a
  dialled number, so no PSTN leg exists on any path.

This module is the single place both get resolved. The registry is seeded
from Settings today (per-region env fields below); when the V3 store lands it
is seeded from OSV's `telephony_providerconfigs` instead — the resolver's
callers don't change.

Why a profile object rather than more env vars: a global
`JUDGE_IT_SIP_DESTINATION_HOST` means dialling India requires re-sourcing
`.env`, and two regions can never be in flight at once. A profile is resolved
per run and travels with it (RunContext, P1.1), so a US run and an India run
coexist in one process. (Verified failure mode: the 31 Aug SBI test — the
exported US host shadowed the scenario's India intent and the US orchestrator
answered "No assistant found for virtual number: +918062123187".)

P3.10 note — trunk-selection granularity: whether ONE regional ingress
resolves every DID in that region, or the target's `provider_config`
(livekit-judge_ittl-2 vs livekit-fonada …) must be matched, is an open question
for the trunk owner. `RegionProfile.provider_config` is the hook: today it is
informational; if the answer is "trunk-scoped", the registry key grows from
`region` to `(region, provider_config)` and `resolve_profile` gains a
`provider_config=` argument — callers keep the same shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .region import ALLOWED_REGIONS, apply_region

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Settings

# Twilio PSTN origination is compliant for these regions ONLY. A US number
# dialling an Indian number over the PSTN is a TRAI/DoT toll-bypass violation
# (the 21–26 Aug fraud-flagged traffic). SIP-to-SIP has no such restriction —
# there is no PSTN leg to regulate.
PSTN_ALLOWED_REGIONS = ("us", "ca")


class RegionNotConfigured(ValueError):
    """The region exists but its SIP trunk coordinates are unknown here."""


class RegionNotCompliant(ValueError):
    """The requested transport may not dial this region at all (S6)."""


@dataclass(frozen=True)
class RegionProfile:
    """Everything a run needs that varies by region. Frozen: resolved once at
    the boundary, then read-only below it."""

    region: str
    osv_base_url: str            # interactions/conversations API (enrichment)
    text_runtime_base_url: str   # number/text mode
    sip_destination_host: str    # SIP-to-SIP mode; "" = trunk unknown
    sip_trunk_number: str
    provider_config: str = ""    # e.g. "livekit-judge_ittl-2" — see P3.10 note

    @property
    def sip_configured(self) -> bool:
        return bool(self.sip_destination_host and self.sip_trunk_number)


def default_region(settings: "Settings") -> str:
    """The region the deployment's own base URLs point at.

    Inferred from the OSV base URL's path segment rather than declared twice —
    a separate JUDGE_IT_REGION could disagree with the URL, and the URL is
    what actually gets called.
    """
    for seg in settings.osv_base_url.split("/"):
        if seg in ALLOWED_REGIONS:
            return seg
    return "us"


def _per_region(settings: "Settings", field: str, region: str) -> str:
    return getattr(settings, f"{field}_{region}", "") or ""


def resolve_profile(
    region: Optional[str],
    settings: "Settings",
    *,
    for_transport: Optional[str] = None,
) -> RegionProfile:
    """Resolve `region` (None = deployment default) into its config bundle.

    `for_transport` makes dial-time requirements explicit:
      * "sip": the region's trunk must be known — otherwise
        RegionNotConfigured, naming exactly what is missing, BEFORE any call
        is placed.
      * "pstn": no trunk needed (it dials the E.164 directly), but S6 applies —
        a region outside PSTN_ALLOWED_REGIONS is refused as non-compliant,
        never dialled.
      * None/"text": URLs only; SIP fields may be blank.
    """
    # `explicit` preserves the historical URL semantics exactly: only a PINNED
    # region substitutes the path segment. With region=None the URLs pass
    # through untouched — which also keeps apply_region from warning about a
    # region-less local URL (http://localhost:8001) on every default-region run.
    explicit = region is not None and str(region).strip() != ""
    resolved = (str(region).strip().lower() if explicit else default_region(settings))
    if resolved not in ALLOWED_REGIONS:
        raise ValueError(
            f"region must be one of {ALLOWED_REGIONS}, got {region!r}"
        )

    if for_transport == "pstn" and resolved not in PSTN_ALLOWED_REGIONS:
        raise RegionNotCompliant(
            f"transport 'pstn' may not dial region '{resolved}': a Twilio PSTN "
            f"call into that region is non-compliant (TRAI/DoT toll bypass). "
            f"Use transport 'sip' — the SIP-to-SIP path has no PSTN leg and is "
            f"compliant everywhere."
        )

    is_default = resolved == default_region(settings)
    host = _per_region(settings, "sip_destination_host", resolved) or (
        settings.sip_destination_host if is_default else ""
    )
    trunk = _per_region(settings, "sip_trunk_number", resolved) or (
        settings.sip_trunk_number if is_default else ""
    )

    profile = RegionProfile(
        region=resolved,
        osv_base_url=apply_region(
            settings.osv_base_url, resolved if explicit else None,
            what="osv_base_url",
        ),
        text_runtime_base_url=apply_region(
            settings.text_runtime_base_url, resolved if explicit else None,
            what="text_runtime_base_url",
        ),
        sip_destination_host=host,
        sip_trunk_number=trunk,
        provider_config=_per_region(settings, "sip_provider_config", resolved),
    )

    # Only "sip" needs trunk coordinates — "pstn" dials the E.164 directly
    # (and got its compliance check above), "text"/None never dial at all.
    if for_transport == "sip" and not profile.sip_configured:
        missing = []
        if not profile.sip_destination_host:
            missing.append(f"JUDGE_IT_SIP_DESTINATION_HOST_{resolved.upper()}")
        if not profile.sip_trunk_number:
            missing.append(f"JUDGE_IT_SIP_TRUNK_NUMBER_{resolved.upper()}")
        raise RegionNotConfigured(
            f"no SIP trunk is configured for region '{resolved}' — set "
            f"{' and '.join(missing)} (the LiveKit SIP ingress host and inbound "
            f"trunk number for that region's project; for India that is the "
            f"livekit-judge_ittl-2 project, whose coordinates the trunk owner has). "
            f"No call was placed."
        )
    return profile
