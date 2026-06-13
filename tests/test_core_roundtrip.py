"""Core serialization + schema conformance (WS-A gate, DESIGN §10).

Every artifact that crosses a boundary must round-trip losslessly through its dict form AND
validate against its published JSON schema. These tests pin both for the ClaimGraph, Finding,
AuditReport, and VerifierManifest.
"""

from __future__ import annotations

import pytest

from litmus.core import schema
from litmus.core.claim import (
    Binding,
    Claim,
    ClaimGraph,
    EpistemicTier,
    Evidence,
    EvidenceKind,
    Location,
)
from litmus.core.finding import (
    EvidencePacket,
    Finding,
    Severity,
    Status,
    TrustTier,
    VerifierKind,
)
from litmus.core.provenance import AuditReport, DroppedFlag, RoutedItem
from litmus.core.verifier import Determinism, VerifierManifest


# --- fixtures (builders) -----------------------------------------------------
def _claim_graph() -> ClaimGraph:
    ev1 = Evidence(
        id="ev1",
        kind=EvidenceKind.TABLE,
        location=Location(section="Results", page=3, char_span=(120, 180), quote="Total 100"),
        extracted_values={"parts": [40, 35, 25], "reported_total": 100},
        confidence=0.9,
    )
    ev2 = Evidence(
        id="ev2",
        kind=EvidenceKind.STATISTIC,
        location=Location(quote="p = 0.03"),
        extracted_values={"p": 0.03},
    )
    c1 = Claim(
        id="c1",
        text="The reported total is 100.",
        location=Location(section="Results", page=3, char_span=(100, 119)),
        epistemic_tier=EpistemicTier.T0,
        predicate="reported_total == sum(parts)",
        strength="exact",
        scope="Table 1",
        evidence_refs=["ev1"],
        confidence=1.0,
    )
    c2 = Claim(id="c2", text="The effect is significant.", epistemic_tier=EpistemicTier.T3)
    return ClaimGraph(
        paper_id="paper-xyz",
        claims=[c1, c2],
        evidence=[ev1, ev2],
        bindings=[Binding(claim_id="c1", evidence_id="ev1"), Binding(claim_id="c2", evidence_id="ev2")],
        meta={"doi": "10.1/abc", "title": "A paper"},
    )


def _pass_finding() -> Finding:
    return Finding(
        verifier_id="sum_check.v1",
        claim_id="c1",
        status=Status.PASS,
        trust_tier=TrustTier.DETERMINISTIC_CONFIRMED,
        verifier_kind=VerifierKind.PREBUILT,
        message="reported total equals the sum of its parts",
        reported=100,
        computed=100,
        details={"parts": [40, 35, 25]},
    )


def _fail_finding() -> Finding:
    script = (
        "computed = sum([12, 25, 60])\n"
        "print('MISMATCH reported=100 computed=' + str(computed))\n"
    )
    return Finding(
        verifier_id="sum_check.v1",
        claim_id="c1",
        status=Status.FAIL,
        trust_tier=TrustTier.DETERMINISTIC_CONFIRMED,
        verifier_kind=VerifierKind.PREBUILT,
        severity=Severity.B,
        message="reported total does not equal the sum of its parts",
        discrepancy="reported 100 but parts sum to 97",
        reported=100,
        computed=97,
        evidence=EvidencePacket(
            quote="Total 100",
            location=Location(section="Results", page=3, quote="Total 100"),
            recompute_script=script,
            expected_output="MISMATCH reported=100 computed=97",
            script_dependencies=[],
        ),
        details={"parts": [12, 25, 60]},
    )


def _audit_report() -> AuditReport:
    return AuditReport(
        paper_id="paper-xyz",
        findings=[_pass_finding(), _fail_finding()],
        dropped_flags=[DroppedFlag(finding=_fail_finding(), reason="not confirmed on fresh context")],
        routed_to_human=[RoutedItem(claim_id="c2", dimension="significance", note="subjective", quote="significant")],
        abstained=[
            Finding(
                verifier_id="sum_check.v1",
                claim_id="c3",
                status=Status.INCONCLUSIVE,
                trust_tier=TrustTier.DETERMINISTIC_CONFIRMED,
                verifier_kind=VerifierKind.PREBUILT,
                message="could not bind evidence",
            )
        ],
        meta={"generated_by": "test"},
    )


def _manifest() -> VerifierManifest:
    return VerifierManifest(
        id="sum_check.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["table_total", "sum_claim"],
        capability_tags=["arithmetic"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        built_vs_borrowed={"ours": ["sum recompute"], "libs": []},
        description="sum check",
    )


# --- round-trip equality -----------------------------------------------------
def test_claim_graph_roundtrips():
    cg = _claim_graph()
    assert ClaimGraph.from_dict(cg.to_dict()).to_dict() == cg.to_dict()
    # And the rehydrated object equals the original dataclass.
    assert ClaimGraph.from_dict(cg.to_dict()) == cg


def test_pass_finding_roundtrips():
    f = _pass_finding()
    assert Finding.from_dict(f.to_dict()).to_dict() == f.to_dict()
    assert Finding.from_dict(f.to_dict()) == f


def test_fail_finding_roundtrips():
    f = _fail_finding()
    assert Finding.from_dict(f.to_dict()).to_dict() == f.to_dict()
    assert Finding.from_dict(f.to_dict()) == f


def test_audit_report_roundtrips():
    ar = _audit_report()
    # summary() is recomputed in to_dict(); round-trip via from_dict should be stable.
    assert AuditReport.from_dict(ar.to_dict()).to_dict() == ar.to_dict()


def test_manifest_roundtrips():
    m = _manifest()
    assert VerifierManifest.from_dict(m.to_dict()).to_dict() == m.to_dict()
    assert VerifierManifest.from_dict(m.to_dict()) == m


def test_location_roundtrips_with_and_without_span():
    loc = Location(section="X", page=2, char_span=(1, 9), quote="q")
    assert Location.from_dict(loc.to_dict()) == loc
    empty = Location()
    assert Location.from_dict(empty.to_dict()) == empty


# --- schema validation -------------------------------------------------------
def test_claim_graph_validates_against_schema():
    assert schema.validate(_claim_graph().to_dict(), "claim") == []
    assert schema.is_valid(_claim_graph().to_dict(), "claim")


def test_pass_finding_validates_against_schema():
    assert schema.validate(_pass_finding().to_dict(), "finding") == []


def test_fail_finding_validates_against_schema():
    assert schema.validate(_fail_finding().to_dict(), "finding") == []


def test_audit_report_validates_against_schema():
    assert schema.validate(_audit_report().to_dict(), "audit") == []


def test_manifest_validates_against_schema():
    assert schema.validate(_manifest().to_dict(), "verifier_manifest") == []


def test_assert_valid_raises_on_bad_instance():
    bad = {"paper_id": "p"}  # missing required claims/evidence
    with pytest.raises(ValueError):
        schema.assert_valid(bad, "claim")
