"""The Finding — a verifier's verdict on one claim (DESIGN §6.3, §14).

A Finding is the *only* thing a verifier emits. It is produced by deterministic code, never
by the model (DESIGN §3.1). Every ``fail`` MUST ship executable evidence — a recompute script
and its expected output (DESIGN §3.2: "No script, no flag.").

Every Finding carries its ``trust_tier`` (DESIGN §3.6): the UI and API never blur
``deterministic_confirmed`` and ``advisory_assisted``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from litmus.core.claim import Location


class Status(str, Enum):
    """A verifier's verdict on a claim.

    ``ABSTAIN`` (DESIGN §3.4: "Abstain > guess") is returned when extraction/binding is
    ambiguous; a verifier never silently absolves, so the absence of a flag is never a PASS
    unless the verifier actually ran and confirmed.
    """

    PASS = "pass"  # the check ran and the claim holds
    FAIL = "fail"  # the check ran and the claim is violated (ships executable evidence)
    INCONCLUSIVE = "inconclusive"  # could not bind/extract; abstained
    ERROR = "error"  # the verifier itself errored (treated as inconclusive downstream)


class Severity(str, Enum):
    """Severity of a failed check (DESIGN §6.3). A > B > C."""

    A = "A"  # impossible / hard violation (negative yield, >100% EA, p inconsistent with stat)
    B = "B"  # inconsistency (prose vs table mismatch, CI crosses null but "significant")
    C = "C"  # minor (rounding, presentation)


class TrustTier(str, Enum):
    """How much the verdict can be trusted (DESIGN §3.6, §14). Never collapses."""

    DETERMINISTIC_CONFIRMED = "deterministic_confirmed"
    CALIBRATED_SYNTHESIZED = "calibrated_synthesized"
    ADVISORY_ASSISTED = "advisory_assisted"
    ROUTED_TO_HUMAN = "routed_to_human"


class VerifierKind(str, Enum):
    """Who produced the verifier (DESIGN §6.1). Drives provenance + trust mapping."""

    PREBUILT = "prebuilt"  # A: first-party, hardened
    TEMPLATED = "templated"  # B: trusted skeleton, LLM fills paper-specific content
    SYNTHESIZED = "synthesized"  # C: new logic written on the fly
    ASSISTED = "assisted"  # D: needs an LLM judgement, anchored deterministically


@dataclass
class EvidencePacket:
    """The executable evidence backing a Finding (DESIGN §3.2, §14).

    ``recompute_script`` is a self-contained Python program; run in the recompute sandbox
    it must print ``expected_output`` (DESIGN §7 G3). This is what makes a flag falsifiable
    by a skeptical reader: they run it themselves.
    """

    quote: Optional[str] = None
    location: Location = field(default_factory=Location)
    recompute_script: Optional[str] = None
    expected_output: Optional[str] = None
    # Declared, pinned deps the script needs beyond the stdlib (DESIGN §3.8, P8). Empty = stdlib-only.
    script_dependencies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote": self.quote,
            "location": self.location.to_dict(),
            "recompute_script": self.recompute_script,
            "expected_output": self.expected_output,
            "script_dependencies": list(self.script_dependencies),
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "EvidencePacket":
        if not d:
            return cls()
        return cls(
            quote=d.get("quote"),
            location=Location.from_dict(d.get("location")),
            recompute_script=d.get("recompute_script"),
            expected_output=d.get("expected_output"),
            script_dependencies=list(d.get("script_dependencies") or []),
        )


@dataclass
class Finding:
    """A verifier's verdict on one claim (DESIGN §6.3).

    Contract: a ``FAIL`` must carry ``evidence.recompute_script`` + ``evidence.expected_output``
    (enforced by :meth:`validate`). ``computed`` / ``reported`` capture the numeric discrepancy.
    """

    verifier_id: str
    claim_id: Optional[str]
    status: Status
    trust_tier: TrustTier
    verifier_kind: VerifierKind
    severity: Optional[Severity] = None
    message: str = ""
    discrepancy: Optional[str] = None
    reported: Any = None  # what the paper claimed
    computed: Any = None  # what deterministic recompute produced
    evidence: EvidencePacket = field(default_factory=EvidencePacket)
    details: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Return a list of contract violations (empty == valid). DESIGN §3.2."""
        problems: list[str] = []
        if self.status is Status.FAIL:
            if not self.evidence.recompute_script:
                problems.append("FAIL without recompute_script (DESIGN §3.2: no script, no flag)")
            if self.evidence.expected_output is None:
                problems.append("FAIL without expected_output")
            if self.severity is None:
                problems.append("FAIL without severity")
        return problems

    @property
    def is_flag(self) -> bool:
        return self.status is Status.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_id": self.verifier_id,
            "claim_id": self.claim_id,
            "status": self.status.value,
            "trust_tier": self.trust_tier.value,
            "verifier_kind": self.verifier_kind.value,
            "severity": self.severity.value if self.severity else None,
            "message": self.message,
            "discrepancy": self.discrepancy,
            "reported": self.reported,
            "computed": self.computed,
            "evidence": self.evidence.to_dict(),
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        sev = d.get("severity")
        return cls(
            verifier_id=d["verifier_id"],
            claim_id=d.get("claim_id"),
            status=Status(d["status"]),
            trust_tier=TrustTier(d["trust_tier"]),
            verifier_kind=VerifierKind(d["verifier_kind"]),
            severity=Severity(sev) if sev else None,
            message=d.get("message", ""),
            discrepancy=d.get("discrepancy"),
            reported=d.get("reported"),
            computed=d.get("computed"),
            evidence=EvidencePacket.from_dict(d.get("evidence")),
            details=dict(d.get("details") or {}),
        )
