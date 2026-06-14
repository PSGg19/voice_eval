"""Variable sets: the caller's identity, stored once and referenced.

Today every scenario inlines its own `vars` (`scenario_loader.py:587` accepts
only an inline mapping — there is no reference mechanism). `clients/*/
variables.json` exists but is a *human* reference: scenarios copy values out of
it by hand, annotated with comments like `# accounts.nora_ibarra_sanudo
.full_name`. That manual duplication is why one customer's identity ended up
spread across a hundred files, and it is what this replaces.

Resolution has three inputs, applied in order:

    1. the target's default set      — "just run it", the common case
    2. an explicitly chosen set      — "run it as this other identity"
    3. inline overrides              — "same identity but this one phone"

**The safety property is in the ordering.** A stored set can be verified
synthetic once and then trusted. An inline override cannot inherit that trust —
it is new data from the caller — so any override marks the resolution unverified
and forces the value guard to run again. That is deliberate: yesterday's
production leak was not a wrong assistant, it was a *correct* assistant with a
real application number sitting in the scenario's own vars.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class VariableSet:
    """A named bundle of caller-known facts, scoped to a bot.

    `is_synthetic` is an assertion by whoever created the set: these values do
    not identify a real person. It is what lets the value guard stay quiet on a
    set that has already been reviewed, instead of re-litigating every run.
    """

    id: str
    name: str
    values: dict[str, Any] = field(default_factory=dict)
    is_synthetic: bool = False
    target_id: Optional[str] = None


@dataclass
class Resolution:
    """The merged variables actually used by one run, plus where each came from.

    `values` is what gets snapshotted onto the run — snapshotted, not
    referenced, so a run stays explicable after someone edits the set it used.
    """

    values: dict[str, Any]
    # key -> "default" | "explicit" | "override", for the audit trail and for
    # explaining a guard finding without re-reading three records.
    provenance: dict[str, str]
    verified_synthetic: bool
    source_set_id: Optional[str]
    overridden_keys: tuple[str, ...]

    @property
    def needs_value_check(self) -> bool:
        """True when the guard must inspect the values themselves.

        An unverified set, or any override on top of a verified one.
        """
        return not self.verified_synthetic


def resolve(
    *,
    default_set: Optional[VariableSet] = None,
    explicit_set: Optional[VariableSet] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> Resolution:
    """Merge the three sources into what one run will actually use.

    An explicit set *replaces* the default rather than layering on it — picking
    a different identity should not silently inherit half of another one. Inline
    overrides do layer, key by key, because "this identity but one field
    changed" is the actual use case.
    """
    chosen = explicit_set or default_set
    values: dict[str, Any] = dict(chosen.values) if chosen else {}
    provenance = {
        k: ("explicit" if explicit_set else "default") for k in values
    }

    overrides = overrides or {}
    for key, value in overrides.items():
        values[key] = value
        provenance[key] = "override"

    # Trust is not inheritable: a reviewed set stays trusted only while nothing
    # is layered on top of it.
    verified = bool(chosen and chosen.is_synthetic and not overrides)

    return Resolution(
        values=values,
        provenance=provenance,
        verified_synthetic=verified,
        source_set_id=chosen.id if chosen else None,
        overridden_keys=tuple(overrides),
    )
