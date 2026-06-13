"""The audit report + provenance graph (DESIGN §10, §14).

The per-paper audit is a second JSON document, *derived* by running the verifiers over the
claim graph (DESIGN §10) — re-running the library regenerates it without re-extracting. It
renders two ways (DESIGN §14):

  * **CHECKABLE**       — every confirmed flag, each with a recompute script (DESIGN §3.2).
  * **ROUTE-TO-HUMAN**  — subjective dimensions (T8) + integrity signals (T7), surfaced,
                          explicitly NOT scored (DESIGN §3.5); plus the *abstained* list and
                          the *dropped-flag* log (fresh-context self-caught false positives —
                          the autonomy evidence, DESIGN §13 step 4).

Every finding carries its trust tier; the report never blurs tiers (DESIGN §3.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from litmus.core.claim import ClaimGraph
from litmus.core.finding import Finding, Status, TrustTier


@dataclass
class DroppedFlag:
    """A flag that did not survive fresh-context confirmation (DESIGN §13 step 4)."""

    finding: Finding
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"finding": self.finding.to_dict(), "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DroppedFlag":
        return cls(finding=Finding.from_dict(d["finding"]), reason=d.get("reason", ""))


@dataclass
class RoutedItem:
    """A dimension routed to a human, never scored (DESIGN §3.5, §14)."""

    claim_id: Optional[str]
    dimension: str  # e.g. "significance", "novelty", "integrity:image-duplication"
    note: str = ""
    quote: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"claim_id": self.claim_id, "dimension": self.dimension, "note": self.note, "quote": self.quote}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoutedItem":
        return cls(
            claim_id=d.get("claim_id"),
            dimension=d["dimension"],
            note=d.get("note", ""),
            quote=d.get("quote"),
        )


@dataclass
class AuditReport:
    """The per-paper audit (DESIGN §14). Derived; schema-validated (audit.schema.json)."""

    paper_id: str
    findings: list[Finding] = field(default_factory=list)  # confirmed (survived §13.4)
    dropped_flags: list[DroppedFlag] = field(default_factory=list)
    routed_to_human: list[RoutedItem] = field(default_factory=list)
    abstained: list[Finding] = field(default_factory=list)  # status == inconclusive
    meta: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0"

    # --- views (DESIGN §14) --------------------------------------------------
    @property
    def checkable(self) -> list[Finding]:
        """Confirmed flags (status == fail), highest trust first."""
        order = {
            TrustTier.DETERMINISTIC_CONFIRMED: 0,
            TrustTier.CALIBRATED_SYNTHESIZED: 1,
            TrustTier.ADVISORY_ASSISTED: 2,
            TrustTier.ROUTED_TO_HUMAN: 3,
        }
        flags = [f for f in self.findings if f.status is Status.FAIL]
        return sorted(flags, key=lambda f: order.get(f.trust_tier, 9))

    def summary(self) -> dict[str, Any]:
        flags = self.checkable
        by_tier: dict[str, int] = {}
        by_sev: dict[str, int] = {}
        for f in flags:
            by_tier[f.trust_tier.value] = by_tier.get(f.trust_tier.value, 0) + 1
            if f.severity:
                by_sev[f.severity.value] = by_sev.get(f.severity.value, 0) + 1
        return {
            "n_findings": len(self.findings),
            "n_flags": len(flags),
            "n_dropped": len(self.dropped_flags),
            "n_routed_to_human": len(self.routed_to_human),
            "n_abstained": len(self.abstained),
            "flags_by_trust_tier": by_tier,
            "flags_by_severity": by_sev,
        }

    # --- provenance graph (DESIGN §14: the report rendered as a graph) -------
    def provenance_graph(self, claim_graph: Optional[ClaimGraph] = None) -> dict[str, Any]:
        """Nodes (claims, evidence, verifiers, findings) + typed edges.

        claims ──rests_on──▶ evidence ──verified_by──▶ verifier ──produced──▶ finding.
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        seen_verifiers: set[str] = set()

        if claim_graph is not None:
            for c in claim_graph.claims:
                nodes.append({"id": f"claim:{c.id}", "type": "claim", "label": c.text[:80]})
            for e in claim_graph.evidence:
                nodes.append({"id": f"evidence:{e.id}", "type": "evidence", "label": e.kind.value})
            for c in claim_graph.claims:
                for e in claim_graph.evidence_for(c):
                    edges.append({"from": f"claim:{c.id}", "to": f"evidence:{e.id}", "rel": "rests_on"})

        for i, f in enumerate(self.findings):
            fid = f"finding:{i}"
            nodes.append(
                {"id": fid, "type": "finding", "label": f.status.value, "trust_tier": f.trust_tier.value}
            )
            if f.verifier_id not in seen_verifiers:
                seen_verifiers.add(f.verifier_id)
                nodes.append({"id": f"verifier:{f.verifier_id}", "type": "verifier", "label": f.verifier_id})
            edges.append({"from": f"verifier:{f.verifier_id}", "to": fid, "rel": "produced"})
            if f.claim_id:
                edges.append({"from": f"claim:{f.claim_id}", "to": f"verifier:{f.verifier_id}", "rel": "verified_by"})
        return {"nodes": nodes, "edges": edges}

    # --- serialization -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "paper_id": self.paper_id,
            "meta": self.meta,
            "summary": self.summary(),
            "findings": [f.to_dict() for f in self.findings],
            "dropped_flags": [d.to_dict() for d in self.dropped_flags],
            "routed_to_human": [r.to_dict() for r in self.routed_to_human],
            "abstained": [f.to_dict() for f in self.abstained],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuditReport":
        return cls(
            paper_id=d["paper_id"],
            findings=[Finding.from_dict(f) for f in d.get("findings", [])],
            dropped_flags=[DroppedFlag.from_dict(x) for x in d.get("dropped_flags", [])],
            routed_to_human=[RoutedItem.from_dict(x) for x in d.get("routed_to_human", [])],
            abstained=[Finding.from_dict(f) for f in d.get("abstained", [])],
            meta=dict(d.get("meta") or {}),
            schema_version=d.get("schema_version", "1.0"),
        )
