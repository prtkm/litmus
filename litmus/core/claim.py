"""Claim IR — the structured representation of a paper's checkable assertions (DESIGN §11).

The extractor (Opus, WS-B) emits a ``ClaimGraph``; every downstream consumer — planner,
verifiers, kernel, web, API — reads it. JSON is the canonical store (DESIGN §10); these
dataclasses are the in-memory view, with lossless ``to_dict``/``from_dict`` round-tripping
to that JSON (validated against ``schemas/claim.schema.json``).

INVARIANT (DESIGN §3.1, §11): nothing here renders a verdict. Claims and evidence only
*transcribe and locate*. A missing value is recorded as ``None``, never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EpistemicTier(str, Enum):
    """The verifiability tier of an assertion (DESIGN §5). Drives routing and trust.

    Proposed by the extractor, confirmed/overridden by the planner.
    """

    T0 = "T0"  # internal arithmetic (yields, p-from-statistic, table totals, units)
    T1 = "T1"  # fixed external knowledge (constants, atom/charge balance, retraction)
    T2 = "T2"  # internal cross-consistency (prose vs the paper's own table/figure)
    T3 = "T3"  # method appropriateness (right test? correction? power? assumptions)
    T4 = "T4"  # claim<->evidence support (over-generalization, extrapolation, causal)
    T5 = "T5"  # external / literature (contradiction, citation distortion, prior-art)
    T6 = "T6"  # reproducibility (rerun provided data/code -> numbers reproduce?)
    T7 = "T7"  # integrity signals (image dup, Benford, tortured phrases) -> human
    T8 = "T8"  # irreducibly subjective (significance, novelty, taste) -> human


class EvidenceKind(str, Enum):
    """What kind of artifact a piece of evidence is (DESIGN §11)."""

    TABLE = "table"
    FIGURE = "figure"
    DATASET = "dataset"
    EQUATION = "equation"
    STATISTIC = "statistic"
    NUMBER = "number"
    TEXT = "text"


def _coerce_span(value: Any) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return (int(value[0]), int(value[1]))
    raise ValueError(f"char_span must be a [start, end] pair, got {value!r}")


@dataclass
class Location:
    """Where an assertion or evidence lives in the source document (DESIGN §11).

    ``quote`` is the verbatim source substring; the WS-B quote guard requires it to be
    substring-verifiable against the PDF text layer (DESIGN §11).
    """

    section: Optional[str] = None
    page: Optional[int] = None
    char_span: Optional[tuple[int, int]] = None
    quote: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "page": self.page,
            "char_span": list(self.char_span) if self.char_span is not None else None,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "Location":
        if not d:
            return cls()
        return cls(
            section=d.get("section"),
            page=d.get("page"),
            char_span=_coerce_span(d.get("char_span")),
            quote=d.get("quote"),
        )


@dataclass
class Evidence:
    """A figure/table/dataset/equation/statistic/number the paper points to (DESIGN §11).

    ``extracted_values`` holds transcribed numbers (e.g. ``{"yield_pct": 142.0}``) — the
    raw material a verifier recomputes against. Never a judgement; just what's on the page.
    """

    id: str
    kind: EvidenceKind
    location: Location = field(default_factory=Location)
    extracted_values: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "location": self.location.to_dict(),
            "extracted_values": self.extracted_values,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        return cls(
            id=d["id"],
            kind=EvidenceKind(d["kind"]),
            location=Location.from_dict(d.get("location")),
            extracted_values=dict(d.get("extracted_values") or {}),
            confidence=float(d.get("confidence", 1.0)),
        )


@dataclass
class Claim:
    """A checkable assertion extracted from the paper (DESIGN §11).

    ``epistemic_tier`` is *proposed* by the extractor. ``predicate`` is the operationalized
    form ("X is precise enough to check as: ..."). ``evidence_refs`` lists the ids of the
    Evidence records this claim rests on.
    """

    id: str
    text: str
    location: Location = field(default_factory=Location)
    epistemic_tier: Optional[EpistemicTier] = None
    predicate: Optional[str] = None
    strength: Optional[str] = None
    scope: Optional[str] = None
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "location": self.location.to_dict(),
            "epistemic_tier": self.epistemic_tier.value if self.epistemic_tier else None,
            "predicate": self.predicate,
            "strength": self.strength,
            "scope": self.scope,
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        tier = d.get("epistemic_tier")
        return cls(
            id=d["id"],
            text=d["text"],
            location=Location.from_dict(d.get("location")),
            epistemic_tier=EpistemicTier(tier) if tier else None,
            predicate=d.get("predicate"),
            strength=d.get("strength"),
            scope=d.get("scope"),
            evidence_refs=list(d.get("evidence_refs") or []),
            confidence=float(d.get("confidence", 1.0)),
        )


@dataclass
class Binding:
    """A claim ──rests_on──▶ evidence edge (DESIGN §11)."""

    claim_id: str
    evidence_id: str
    relation: str = "rests_on"

    def to_dict(self) -> dict[str, Any]:
        return {"claim_id": self.claim_id, "evidence_id": self.evidence_id, "relation": self.relation}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Binding":
        return cls(
            claim_id=d["claim_id"],
            evidence_id=d["evidence_id"],
            relation=d.get("relation", "rests_on"),
        )


@dataclass
class ClaimGraph:
    """The per-paper graph of claims, evidence, and bindings (DESIGN §10, §11).

    One JSON document, schema-validated, is the source of truth for *what is in the paper*.
    It is the LLM's structured extraction output and the input to the planner + verifiers.
    """

    paper_id: str
    claims: list[Claim] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    bindings: list[Binding] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0"

    # --- convenience lookups -------------------------------------------------
    def evidence_by_id(self, evidence_id: str) -> Optional[Evidence]:
        for e in self.evidence:
            if e.id == evidence_id:
                return e
        return None

    def claim_by_id(self, claim_id: str) -> Optional[Claim]:
        for c in self.claims:
            if c.id == claim_id:
                return c
        return None

    def evidence_for(self, claim: Claim) -> list[Evidence]:
        """The Evidence records a claim rests on, via both evidence_refs and bindings."""
        ids: list[str] = list(claim.evidence_refs)
        for b in self.bindings:
            if b.claim_id == claim.id and b.evidence_id not in ids:
                ids.append(b.evidence_id)
        out: list[Evidence] = []
        for eid in ids:
            ev = self.evidence_by_id(eid)
            if ev is not None:
                out.append(ev)
        return out

    # --- serialization -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "paper_id": self.paper_id,
            "meta": self.meta,
            "claims": [c.to_dict() for c in self.claims],
            "evidence": [e.to_dict() for e in self.evidence],
            "bindings": [b.to_dict() for b in self.bindings],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClaimGraph":
        return cls(
            paper_id=d["paper_id"],
            claims=[Claim.from_dict(c) for c in d.get("claims", [])],
            evidence=[Evidence.from_dict(e) for e in d.get("evidence", [])],
            bindings=[Binding.from_dict(b) for b in d.get("bindings", [])],
            meta=dict(d.get("meta") or {}),
            schema_version=d.get("schema_version", "1.0"),
        )
