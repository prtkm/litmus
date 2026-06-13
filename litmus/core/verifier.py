"""The verifier contract — the universal interface (DESIGN §6.3).

Every verifier — prebuilt, templated, synthesized, or contributed — is a self-describing
package: a ``VerifierManifest`` (who/what wrote it, what it consumes, its tier, its FPR
ceiling) plus two methods:

  * ``judge(claim, evidence) -> Finding``      — PURE; the verdict (deterministic code).
  * ``self_test() -> [SelfTestCase, ...]``     — REQUIRED; the admission fuel: clean
        instances that should PASS and planted instances that should FAIL. A verifier
        ships the means to calibrate itself (DESIGN §6.3). No self_test -> advisory only.

The trust comes from the calibration gate (DESIGN §7), not from the model.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from litmus.core.claim import Claim, EpistemicTier, Evidence
from litmus.core.finding import (
    EvidencePacket,
    Finding,
    Severity,
    Status,
    TrustTier,
    VerifierKind,
)


class Determinism(str, Enum):
    """How a verifier reaches its verdict (DESIGN §6.3)."""

    DETERMINISTIC = "deterministic"  # pure computation on the paper's numbers
    LOOKUP = "lookup"  # deterministic given a versioned reference DB
    ASSISTED = "assisted"  # needs an LLM judgement, anchored deterministically
    SYNTHESIZED = "synthesized"  # logic written on the fly, calibrated before trust


def default_trust_tier(kind: VerifierKind, determinism: Determinism) -> TrustTier:
    """The trust tier a freshly-admitted verifier's findings default to (DESIGN §3.6).

    The planner may *downgrade* this (never upgrade) based on admission status.
    """
    if determinism is Determinism.ASSISTED:
        return TrustTier.ADVISORY_ASSISTED
    if kind is VerifierKind.SYNTHESIZED or determinism is Determinism.SYNTHESIZED:
        return TrustTier.CALIBRATED_SYNTHESIZED
    return TrustTier.DETERMINISTIC_CONFIRMED


@dataclass
class VerifierManifest:
    """Self-describing metadata for a verifier (DESIGN §6.3).

    ``built_vs_borrowed`` makes attribution explicit (DESIGN §3.7: reimplement, don't
    wrap-and-claim). ``fpr_ceiling`` is the declared bar the calibration kernel holds it to.
    """

    id: str
    version: str
    kind: VerifierKind
    epistemic_tier: EpistemicTier
    determinism: Determinism
    consumes: list[str]  # routing keys: claim_type | record_type (DESIGN §12)
    capability_tags: list[str] = field(default_factory=list)
    fpr_ceiling: float = 0.05
    authors: list[str] = field(default_factory=list)
    license: str = "Apache-2.0"
    provenance: str = "first-party"
    built_vs_borrowed: dict[str, list[str]] = field(
        default_factory=lambda: {"ours": [], "libs": []}
    )
    dependencies: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "kind": self.kind.value,
            "epistemic_tier": self.epistemic_tier.value,
            "determinism": self.determinism.value,
            "consumes": list(self.consumes),
            "capability_tags": list(self.capability_tags),
            "fpr_ceiling": self.fpr_ceiling,
            "authors": list(self.authors),
            "license": self.license,
            "provenance": self.provenance,
            "built_vs_borrowed": self.built_vs_borrowed,
            "dependencies": list(self.dependencies),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerifierManifest":
        return cls(
            id=d["id"],
            version=d["version"],
            kind=VerifierKind(d["kind"]),
            epistemic_tier=EpistemicTier(d["epistemic_tier"]),
            determinism=Determinism(d["determinism"]),
            consumes=list(d.get("consumes") or []),
            capability_tags=list(d.get("capability_tags") or []),
            fpr_ceiling=float(d.get("fpr_ceiling", 0.05)),
            authors=list(d.get("authors") or []),
            license=d.get("license", "Apache-2.0"),
            provenance=d.get("provenance", "first-party"),
            built_vs_borrowed=d.get("built_vs_borrowed") or {"ours": [], "libs": []},
            dependencies=list(d.get("dependencies") or []),
            description=d.get("description", ""),
        )


@dataclass
class SelfTestCase:
    """One calibration instance a verifier generates about itself (DESIGN §6.3).

    ``kind == "planted"`` means a known error was injected: ``judge`` must FAIL it (feeds G1
    recall). ``kind == "clean"`` means a correct instance: ``judge`` must NOT fail it (feeds
    G2/G6 FPR). ``claim_type`` lets the kernel measure FPR *per claim_type* (DESIGN §7 G6).
    """

    name: str
    kind: str  # "clean" | "planted"
    claim: Claim
    evidence: list[Evidence] = field(default_factory=list)
    claim_type: str = "default"

    @property
    def expected_status(self) -> Status:
        return Status.FAIL if self.kind == "planted" else Status.PASS


class Verifier(abc.ABC):
    """Base class for every verifier. Subclasses set ``manifest`` and implement
    ``judge`` + ``self_test``.

    ``judge`` MUST be pure and deterministic (DESIGN §3.1, §7 G4): no RNG, clock, network,
    or LLM call inside. The calibration kernel verifies this empirically (G4) before the
    verifier is trusted to score.
    """

    manifest: VerifierManifest

    @abc.abstractmethod
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        """Render a verdict on one claim. Pure + deterministic. Returns a Finding."""

    @abc.abstractmethod
    def self_test(self) -> list[SelfTestCase]:
        """Generate clean + planted instances with their expected verdicts (DESIGN §6.3)."""

    # --- helpers for subclasses ---------------------------------------------
    def make_finding(
        self,
        *,
        claim: Optional[Claim],
        status: Status,
        severity: Optional[Severity] = None,
        message: str = "",
        discrepancy: Optional[str] = None,
        reported: Any = None,
        computed: Any = None,
        evidence: Optional[EvidencePacket] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> Finding:
        """Construct a Finding pre-filled with this verifier's identity + trust tier."""
        return Finding(
            verifier_id=self.manifest.id,
            claim_id=claim.id if claim is not None else None,
            status=status,
            trust_tier=default_trust_tier(self.manifest.kind, self.manifest.determinism),
            verifier_kind=self.manifest.kind,
            severity=severity,
            message=message,
            discrepancy=discrepancy,
            reported=reported,
            computed=computed,
            evidence=evidence or EvidencePacket(),
            details=details or {},
        )

    def abstain(self, claim: Optional[Claim], reason: str) -> Finding:
        """Return an INCONCLUSIVE finding (DESIGN §3.4: abstain > guess)."""
        return self.make_finding(claim=claim, status=Status.INCONCLUSIVE, message=reason)
