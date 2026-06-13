"""The planner (DESIGN §12).

Given a ``ClaimGraph``, decide per claim what to do: route to verifiers, route to a human
(never scored), or abstain. The planner is **deterministic control flow over an LLM-proposed
plan** — the only dynamic steps in the whole pipeline are extraction and synthesis, which keeps
every run reproducible and gradeable (DESIGN §12).

Routing is **permissive + abstain-as-signal**: a checkable claim is sent to its candidate
verifiers, and each verifier abstains (``INCONCLUSIVE``) when the claim doesn't carry the inputs
it needs (DESIGN §3.4). The driver then reads coverage off the verdicts — if *every* candidate
abstains on a checkable (T0–T2) claim, no existing verifier covers it, which is exactly the
signal to synthesize one (DESIGN §8, WS-F). This avoids a brittle routing-key table while still
detecting synthesis gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from litmus.commons.registry import Registry
from litmus.core.claim import Claim, ClaimGraph, EpistemicTier
from litmus.core.verifier import Verifier

# Tiers routed to a human and explicitly never scored (DESIGN §3.5, §5).
ROUTE_TO_HUMAN_TIERS = {EpistemicTier.T7, EpistemicTier.T8}
# Tiers where "every verifier abstained" means a synthesis candidate (DESIGN §8).
CHECKABLE_TIERS = {EpistemicTier.T0, EpistemicTier.T1, EpistemicTier.T2}
DEFAULT_CONFIDENCE_FLOOR = 0.25


class Action(str, Enum):
    VERIFY = "verify"
    ROUTE_TO_HUMAN = "route_to_human"
    ABSTAIN = "abstain"


@dataclass
class ClaimPlan:
    """The planned disposition of one claim (DESIGN §12)."""

    claim_id: str
    action: Action
    verifier_ids: list[str] = field(default_factory=list)
    reason: str = ""


def candidate_verifiers(claim: Claim, registry: Registry) -> list[Verifier]:
    """Verifiers that might apply to ``claim``.

    Capability-tag routing is used as a *prefilter* when the claim carries tags that intersect a
    verifier's ``capability_tags`` or ``consumes``; otherwise we fall back to the full library and
    rely on each verifier abstaining when inapplicable (DESIGN §3.4, §12). Correctness never
    depends on the prefilter — only efficiency does.
    """
    tags = set(filter(None, [claim.predicate, claim.strength, claim.scope]))
    # tokens the claim exposes for routing: its own hint tags + any tier marker
    hinted: list[Verifier] = []
    for v in registry.all():
        keys = set(v.manifest.consumes) | set(v.manifest.capability_tags)
        if keys & tags:
            hinted.append(v)
    return hinted or registry.all()


def plan_claim(
    claim: Claim, registry: Registry, *, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
) -> ClaimPlan:
    tier = claim.epistemic_tier
    if tier in ROUTE_TO_HUMAN_TIERS:
        return ClaimPlan(
            claim.id,
            Action.ROUTE_TO_HUMAN,
            reason=f"tier {tier.value}: subjective/integrity — surfaced, not scored (DESIGN §3.5)",
        )
    if claim.confidence < confidence_floor:
        return ClaimPlan(
            claim.id,
            Action.ABSTAIN,
            reason=f"extraction confidence {claim.confidence:.2f} < floor {confidence_floor:.2f} (DESIGN §3.4)",
        )
    cands = candidate_verifiers(claim, registry)
    return ClaimPlan(
        claim.id,
        Action.VERIFY,
        verifier_ids=[v.manifest.id for v in cands],
        reason="routed to candidate verifiers; abstain-coverage decides synthesis need",
    )


def plan_audit(
    graph: ClaimGraph, registry: Registry, *, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
) -> list[ClaimPlan]:
    """Plan every claim in the graph (DESIGN §12)."""
    return [plan_claim(c, registry, confidence_floor=confidence_floor) for c in graph.claims]
