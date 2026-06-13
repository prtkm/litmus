"""WS-G gate: the local audit pipeline end to end (DESIGN §13, §19 WS-G).

Deterministic — no API. A crafted ClaimGraph is run through LocalExecutor and we assert the
report has confirmed, reproducible flags, a routed-to-human item, an abstain, a schema-valid
shape, and (separately) that a non-reproducing flag is DROPPED in fresh-context confirmation
(DESIGN §13.4 — the autonomy evidence)."""

from __future__ import annotations

from litmus.commons.registry import Registry
from litmus.core import schema
from litmus.core.claim import (
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
from litmus.core.verifier import (
    Determinism,
    SelfTestCase,
    Verifier,
    VerifierManifest,
)
from litmus.pipeline.executor import LocalExecutor
from litmus.verifiers import percent_change, sum_check, yield_check


def _focused_registry() -> Registry:
    """A registry of just the verifiers this test relies on — isolated from sibling churn."""
    reg = Registry()
    reg.register_all(sum_check.VERIFIERS + percent_change.VERIFIERS + yield_check.VERIFIERS)
    return reg


def _claim(cid, tier, conf, ev_id):
    return Claim(
        id=cid,
        text=f"claim {cid}",
        location=Location(section="test", quote=f"q{cid}"),
        epistemic_tier=tier,
        confidence=conf,
        evidence_refs=[ev_id],
    )


def _crafted_graph() -> ClaimGraph:
    evidence = [
        Evidence(id="e_sum", kind=EvidenceKind.TABLE, extracted_values={"parts": [12, 25, 60], "reported_total": 100}),
        Evidence(id="e_pct", kind=EvidenceKind.NUMBER, extracted_values={"old_value": 50, "new_value": 68, "reported_pct_change": 40}),
        Evidence(id="e_yield", kind=EvidenceKind.NUMBER, extracted_values={"reported_yield_pct": 142}),
        Evidence(id="e_ok", kind=EvidenceKind.TABLE, extracted_values={"parts": [40, 35, 25], "reported_total": 100}),
        Evidence(id="e_subj", kind=EvidenceKind.TEXT, extracted_values={}),
        Evidence(id="e_lc", kind=EvidenceKind.TABLE, extracted_values={"parts": [1, 2], "reported_total": 3}),
    ]
    claims = [
        _claim("c_sum", EpistemicTier.T0, 0.9, "e_sum"),       # sum_check FAIL (97 != 100)
        _claim("c_pct", EpistemicTier.T2, 0.9, "e_pct"),       # percent_change FAIL (36 != 40)
        _claim("c_yield", EpistemicTier.T1, 0.9, "e_yield"),   # yield_check FAIL (>100)
        _claim("c_ok", EpistemicTier.T0, 0.9, "e_ok"),         # sum_check PASS
        _claim("c_subj", EpistemicTier.T8, 0.9, "e_subj"),     # routed to human
        _claim("c_lc", EpistemicTier.T0, 0.1, "e_lc"),         # abstain (low confidence)
    ]
    return ClaimGraph(paper_id="crafted-test", claims=claims, evidence=evidence)


def test_pipeline_confirmed_flags_and_routing():
    report = LocalExecutor(confirm=True).audit_graph(_crafted_graph(), _focused_registry())

    # three confirmed, reproducible flags (sum, percent, yield); all deterministic_confirmed.
    flags = report.checkable
    assert len(flags) == 3, [f.verifier_id for f in flags]
    assert all(f.status is Status.FAIL for f in flags)
    assert all(f.trust_tier is TrustTier.DETERMINISTIC_CONFIRMED for f in flags)
    flagged_verifiers = {f.verifier_id for f in flags}
    assert flagged_verifiers == {"sum_check.v1", "percent_change.v1", "yield_check.v1"}

    # the correct table passed -> a PASS finding is present, not a flag.
    assert any(f.status is Status.PASS for f in report.findings)

    # routing + abstain (DESIGN §3.4, §3.5).
    assert len(report.routed_to_human) == 1 and report.routed_to_human[0].claim_id == "c_subj"
    assert any(f.claim_id == "c_lc" for f in report.abstained)

    # all real flags reproduced -> nothing dropped.
    assert len(report.dropped_flags) == 0

    # the report is schema-valid (DESIGN §10, §14).
    assert schema.validate(report.to_dict(), "audit") == []


class _BadFlagVerifier(Verifier):
    """A verifier whose FAIL ships a recompute_script that does NOT reproduce its
    expected_output — must be dropped in fresh-context confirmation (DESIGN §13.4)."""

    manifest = VerifierManifest(
        id="bad_flag.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["anything"],
    )

    def judge(self, claim, evidence):
        return self.make_finding(
            claim=claim,
            status=Status.FAIL,
            severity=Severity.A,
            message="bogus flag",
            evidence=EvidencePacket(
                recompute_script="print('WRONG')",
                expected_output="RIGHT",  # never reproduces
            ),
        )

    def self_test(self):
        return []  # not used by the pipeline


def test_pipeline_drops_non_reproducing_flag():
    reg = Registry()
    reg.register(_BadFlagVerifier())
    graph = ClaimGraph(
        paper_id="drop-test",
        claims=[_claim("c1", EpistemicTier.T0, 0.9, "e1")],
        evidence=[Evidence(id="e1", kind=EvidenceKind.NUMBER, extracted_values={"x": 1})],
    )
    report = LocalExecutor(confirm=True).audit_graph(graph, reg)
    assert len(report.checkable) == 0  # the bogus flag did not survive confirmation
    assert len(report.dropped_flags) == 1
    assert "did not reproduce" in report.dropped_flags[0].reason
    assert schema.validate(report.to_dict(), "audit") == []


def test_pipeline_no_confirm_keeps_flag():
    reg = Registry()
    reg.register(_BadFlagVerifier())
    graph = ClaimGraph(
        paper_id="noconfirm",
        claims=[_claim("c1", EpistemicTier.T0, 0.9, "e1")],
        evidence=[Evidence(id="e1", kind=EvidenceKind.NUMBER, extracted_values={"x": 1})],
    )
    report = LocalExecutor(confirm=False).audit_graph(graph, reg)
    # with confirmation disabled, the unreproduced flag is NOT dropped (shows confirm is load-bearing).
    assert len(report.checkable) == 1
    assert len(report.dropped_flags) == 0
