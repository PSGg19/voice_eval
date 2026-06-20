"""RunContext (P1.1): everything one run needs, resolved ONCE at the boundary.

The rule this type enforces: configuration is read where a run is CREATED
(CLI command, API admission), never where it executes. Below the boundary the
code asks the context, not `Settings` and not `os.getenv` — which is what
lets two runs with different targets, regions and providers execute in one
process without an ambient global to fight over (the failure mode that
required re-sourcing `.env` between an India and a US call).

Two doors, one type:

* `RunContext.from_settings(scenario, settings)` — the production boundary.
  Resolves the effective transport, the region bundle (P3.8's resolver), the
  model names and the limits from the deployment config.
* Direct construction — tests and embedders. A RunContext is buildable with
  NO environment at all (the P1 acceptance test does exactly that), because
  every field is explicit and `Ports` carries the collaborators.

`Ports` is dependency injection without a framework: any port left None means
"build the production default from Settings", so the existing paths change
behaviour not at all, while a fully-ported context executes with no Settings
object in existence.
"""
from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, TYPE_CHECKING

from opkura.core.region_profile import RegionProfile, default_region, resolve_profile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opkura.core.config import Settings
    from opkura.core.models import Scenario


@dataclass(frozen=True)
class Limits:
    """What bounds the run. `run_deadline_s` is the wall-clock cap P1.6
    enforces on text runs (voice already has one derived from max_turns)."""

    max_turns: int = 30
    run_deadline_s: Optional[float] = None
    evaluator_timeout_s: Optional[float] = None  # None → runner default


@dataclass(frozen=True)
class Ports:
    """Injected collaborators. None → the runner builds its Settings-backed
    default; set → used verbatim (and never re-read from anywhere ambient).

    `text_transport` is a factory `(scenario, session_id) -> transport` with
    the TextTransport surface (`start`, `send`, `close`, `interaction_uuid`).
    `result_store` needs only `save_result(result, *, scenario, transport)`.
    """

    caller_llm: Any = None
    evaluator_llm: Any = None
    text_transport: Optional[Callable[..., Any]] = None
    result_store: Any = None


@dataclass(frozen=True)
class RunContext:
    run_id: str
    scenario: "Scenario"
    target: str
    transport: str
    region: str
    profile: RegionProfile
    caller_model: str
    evaluator_model: str
    idempotency_key: str = ""
    # The resolved variables and where each came from — snapshot semantics,
    # same shape the V3 store persists. Wrapped read-only so nothing below
    # the boundary can mutate what the run means.
    vars: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, str] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    ports: Ports = field(default_factory=Ports)
    skip_evaluation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "vars", MappingProxyType(dict(self.vars)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    @classmethod
    def from_settings(
        cls,
        scenario: "Scenario",
        settings: "Settings",
        *,
        run_id: Optional[str] = None,
        ports: Optional[Ports] = None,
        idempotency_key: str = "",
    ) -> "RunContext":
        """The production boundary: ambient config is read HERE, once."""
        transport = scenario.transport or settings.call_transport
        region = scenario.region or default_region(settings)
        # URL/trunk bundle for the run. No `for_transport` gate here — the
        # dial-time resolve keeps that gate so an unconfigured trunk is an
        # ERROR result with the fix named, not a boundary crash.
        profile = resolve_profile(scenario.region, settings)
        return cls(
            run_id=run_id or uuid_mod.uuid4().hex,
            scenario=scenario,
            target=scenario.target,
            transport=transport,
            region=region,
            profile=profile,
            caller_model=scenario.llm.caller_model or settings.caller_model,
            evaluator_model=scenario.llm.evaluator_model or settings.evaluator_model,
            idempotency_key=idempotency_key,
            vars=dict(scenario.vars or {}),
            limits=Limits(max_turns=scenario.max_turns),
            ports=ports or Ports(),
            skip_evaluation=settings.skip_evaluation,
        )
