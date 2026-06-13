"""THE WS-F GATE — on-the-fly verifier synthesis earns trust through the SAME kernel (DESIGN §8).

This is the WS-F reward function. It proves the §8 loop end to end:

  * LIVE (guarded by ANTHROPIC_API_KEY): the synthesizer asks Opus 4.8 to WRITE a bespoke
    verifier for a metric no first-party verifier covers (turnover frequency, TOF), then runs
    it through the real calibration kernel. The test asserts it is admitted SCORING/advisory,
    that judging a planted instance yields a FAIL, and that the FAIL's stdlib recompute_script
    actually reproduces its expected_output in the network-less sandbox (DESIGN §7 G3).

  * NON-LIVE (always runs): a deliberately NON-DETERMINISTIC proposed source (judge draws from
    ``random``) is fed through materialize + the kernel and MUST be REJECTED (G4). A
    no-self_test source MUST be REJECTED too (no calibration fuel). These hold with zero API
    calls — the gate's verdict does not depend on the model.

The whole point (DESIGN §8): "The trust comes from the gate, not the LLM." A synthesized
verifier passes the exact kernel a chemist's PR would, or it never scores.
"""

from __future__ import annotations

import os

import pytest

from litmus.core import sandbox
from litmus.core.calibration import AdmissionStatus, calibrate
from litmus.core.finding import Status, TrustTier, VerifierKind
from litmus.core.verifier import Determinism
from litmus.synth import materialize, synthesize
from litmus.synth.synthesizer import SynthesisError, propose_verifier

_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# =============================================================================
# Hand-written proposed sources for the NON-LIVE gate tests (no API).
# These stand in for what the model would emit — the gate doesn't care who wrote them.
# =============================================================================
# A judge that draws its verdict from ``random`` -> must fail G4 (non-deterministic). A
# per-instance counter forces the verdict to flip across the kernel's N runs, so the test
# asserts a hard fact rather than relying on a coin landing the same way three times. It has a
# full self_test (5 clean + 5 planted, 2 claim_types) AND runs one judge call cleanly, so it
# passes sandbox vetting and reaches the kernel — making the rejection specifically about G4.
_NONDETERMINISTIC_SRC = r'''
import random
from litmus.core.claim import Claim, Evidence, EvidenceKind, EpistemicTier, Location
from litmus.core.finding import EvidencePacket, Severity, Status, VerifierKind
from litmus.core.verifier import Determinism, SelfTestCase, Verifier, VerifierManifest


class RandomVerifier(Verifier):
    """Picks its verdict with an RNG -> violates the determinism invariant (DESIGN §3.1, §7 G4)."""

    manifest = VerifierManifest(
        id="synth_random.v1", version="1.0", kind=VerifierKind.SYNTHESIZED,
        epistemic_tier=EpistemicTier.T0, determinism=Determinism.SYNTHESIZED,
        consumes=["rand_claim"], fpr_ceiling=0.05, description="non-deterministic on purpose",
    )

    def __init__(self):
        self._calls = 0

    def judge(self, claim, evidence):
        self._calls += 1
        flip = self._calls % 2 == 0
        if flip or random.random() < 0.5:
            pkt = EvidencePacket(recompute_script="print('X')\n", expected_output="X")
            return self.make_finding(claim=claim, status=Status.FAIL, severity=Severity.B,
                                     message="random fail", evidence=pkt)
        return self.make_finding(claim=claim, status=Status.PASS, message="random pass")

    def self_test(self):
        out = []
        for i in range(5):
            ev = Evidence(id="ev_clean_%d" % i, kind=EvidenceKind.NUMBER, location=Location(),
                          extracted_values={"x": i})
            cl = Claim(id="cl_clean_%d" % i, text="t", location=Location(),
                       epistemic_tier=EpistemicTier.T0)
            out.append(SelfTestCase(name="clean_%d" % i, kind="clean", claim=cl, evidence=[ev],
                                    claim_type="a" if i % 2 else "b"))
        for i in range(5):
            ev = Evidence(id="ev_planted_%d" % i, kind=EvidenceKind.NUMBER, location=Location(),
                          extracted_values={"x": i})
            cl = Claim(id="cl_planted_%d" % i, text="t", location=Location(),
                       epistemic_tier=EpistemicTier.T0)
            out.append(SelfTestCase(name="planted_%d" % i, kind="planted", claim=cl, evidence=[ev],
                                    claim_type="a" if i % 2 else "b"))
        return out


VERIFIERS = [RandomVerifier()]
'''

# A deterministic verifier that ships NO self_test -> the kernel has no calibration fuel and
# must REJECT it (DESIGN §6.3: no self_test -> never scores).
_NO_SELF_TEST_SRC = r'''
from litmus.core.claim import Claim, Evidence, EpistemicTier, Location
from litmus.core.finding import Status, VerifierKind
from litmus.core.verifier import Determinism, Verifier, VerifierManifest


class NoSelfTestVerifier(Verifier):
    manifest = VerifierManifest(
        id="synth_noself.v1", version="1.0", kind=VerifierKind.SYNTHESIZED,
        epistemic_tier=EpistemicTier.T0, determinism=Determinism.SYNTHESIZED,
        consumes=["x"], fpr_ceiling=0.05, description="ships no calibration fuel",
    )

    def judge(self, claim, evidence):
        return self.make_finding(claim=claim, status=Status.PASS, message="ok")

    def self_test(self):
        return []


VERIFIERS = [NoSelfTestVerifier()]
'''


# =============================================================================
# NON-LIVE GATE: a non-deterministic synthesized verifier is REJECTED (G4).
# =============================================================================
def test_nondeterministic_synthesized_verifier_is_rejected_g4():
    """The whole §8 loop on a non-deterministic proposal ends in rejection — no API call.

    Trust is the gate, not the model: even a perfectly-shaped proposal with a full self_test
    never scores if its judge isn't deterministic. ``synthesize(..., src=...)`` runs the real
    materialize + kernel path, just skipping the proposal API call.
    """
    out = synthesize("any rand metric", {}, src=_NONDETERMINISTIC_SRC)

    assert out["admission"] == "rejected", out["reason"]
    assert out["kernel_admission"] == AdmissionStatus.REJECTED.value
    card = out["scorecard"]
    assert card is not None  # it materialized + reached the kernel
    assert card.deterministic is False
    assert card.gates.get("G4") is False
    assert any("G4" in r for r in card.reasons)


def test_no_self_test_synthesized_verifier_is_rejected_by_kernel():
    """A synthesized verifier with no self_test is rejected for lack of calibration fuel.

    Driven straight through the kernel (``materialize(vet=False)`` + ``calibrate``) so the
    rejection is unambiguously the kernel's no-self_test invariant (DESIGN §6.3), not the
    pre-import sandbox harness. (The harness also rejects it, exercised below.)
    """
    verifier = materialize(_NO_SELF_TEST_SRC, vet=False)
    assert verifier.manifest.kind is VerifierKind.SYNTHESIZED

    card = calibrate(verifier)
    assert card.admission == AdmissionStatus.REJECTED
    assert any("self_test" in r for r in card.reasons)


def test_no_self_test_source_is_caught_by_pre_import_sandbox():
    """End to end, the no-self_test proposal is rejected (here, at the §8 sandbox+determinism
    pre-import check) — ``synthesize`` returns ``admission='rejected'`` either way."""
    out = synthesize("any metric", {}, src=_NO_SELF_TEST_SRC)
    assert out["admission"] == "rejected", out["reason"]


def test_nonterminating_proposal_is_killed_in_sandbox_before_import():
    """A proposal whose judge never terminates is killed in the isolated sandbox, never imported
    into this process (DESIGN §8 sandbox + determinism check, §15 network-less, resource-limited)."""
    hangs = r'''
from litmus.core.claim import Claim, Evidence, EpistemicTier, Location
from litmus.core.finding import Status, VerifierKind
from litmus.core.verifier import Determinism, SelfTestCase, Verifier, VerifierManifest


class HangVerifier(Verifier):
    manifest = VerifierManifest(
        id="synth_hang.v1", version="1.0", kind=VerifierKind.SYNTHESIZED,
        epistemic_tier=EpistemicTier.T0, determinism=Determinism.SYNTHESIZED,
        consumes=["x"], fpr_ceiling=0.05, description="never terminates",
    )

    def judge(self, claim, evidence):
        while True:
            pass

    def self_test(self):
        ev = Evidence(id="e", location=Location(), extracted_values={"x": 1})
        cl = Claim(id="c", text="t", location=Location(), epistemic_tier=EpistemicTier.T0)
        return [SelfTestCase(name="clean", kind="clean", claim=cl, evidence=[ev])]
'''
    with pytest.raises(SynthesisError):
        materialize(hangs, vet_timeout_s=6.0)


# =============================================================================
# LIVE GATE: synthesize a real bespoke verifier (TOF) and confirm it through the kernel.
# =============================================================================
_TOF_CLAIM = (
    "Bespoke checkable metric — turnover frequency (TOF). For a catalysis result, "
    "TOF = product_mol / (catalyst_mol * time_h). A paper REPORTS a TOF value; flag it as a "
    "violation (severity A) if the reported TOF disagrees with the recomputed TOF by more than "
    "1% relative. PASS if they agree within 1%. The verifier binds to evidence whose "
    "extracted_values carries the keys 'product_mol', 'catalyst_mol', 'time_h', and "
    "'reported_tof' (all numbers)."
)
_TOF_EVIDENCE_EXAMPLE = {
    "product_mol": 2.0,
    "catalyst_mol": 0.1,
    "time_h": 1.0,
    "reported_tof": 20.0,
}


@pytest.mark.skipif(not _HAS_KEY, reason="no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set")
def test_live_synthesize_tof_verifier_passes_the_same_gate():
    """Opus writes a TOF verifier on the fly; it must pass the SAME kernel as every verifier.

    Asserts (DESIGN §8): it materializes, calibrates to SCORING or advisory, is tagged
    synthesized in both manifest and findings, AND a planted instance produces a FAIL whose
    recompute_script reproduces its expected_output byte-for-byte in the network-less sandbox.
    """
    out = synthesize(_TOF_CLAIM, _TOF_EVIDENCE_EXAMPLE)

    # --- it survived propose -> sandbox-vet -> import ------------------------
    assert out["verifier"] is not None, f"did not materialize: {out['reason']}"
    verifier = out["verifier"]
    card = out["scorecard"]
    assert card is not None

    # --- tagged synthesized (manifest), the synthesis kind/determinism ------
    m = verifier.manifest
    assert m.kind is VerifierKind.SYNTHESIZED
    assert m.determinism is Determinism.SYNTHESIZED
    print("\nSYNTHESIZED MANIFEST:", m.to_dict())
    print("CALIBRATION:", card.summary_line())

    # --- admitted by the gate: SCORING (preferred) or at least advisory -----
    assert out["admission"] in ("calibrated_synthesized", "advisory"), out["reason"]
    assert card.admission in (AdmissionStatus.SCORING, AdmissionStatus.ADVISORY), card.reasons
    # It is deterministic and every emitted flag reproduces — the hard invariants hold.
    assert card.deterministic is True, card.reasons
    assert card.gates.get("G4") is True
    assert card.gates.get("G3") is True

    # --- judging a PLANTED instance yields a FAIL with a reproducing script --
    planted = [c for c in verifier.self_test() if c.kind == "planted"]
    assert planted, "synthesized verifier produced no planted self_test cases"

    reproduced_any = False
    for case in planted:
        finding = verifier.judge(case.claim, case.evidence)
        if finding.status is not Status.FAIL:
            continue
        # Contract: a FAIL ships executable evidence and is tagged synthesized (DESIGN §3.2, §3.6).
        assert finding.validate() == [], finding.validate()
        assert finding.verifier_kind is VerifierKind.SYNTHESIZED
        assert finding.trust_tier is TrustTier.CALIBRATED_SYNTHESIZED
        script = finding.evidence.recompute_script or ""
        expected = finding.evidence.expected_output or ""
        ok, result = sandbox.reproduces(script, expected)
        assert ok, (
            f"synthesized FAIL did not reproduce in the sandbox for {case.name}: "
            f"expected={expected!r} got={result.stdout!r} stderr={result.stderr[:300]!r}"
        )
        reproduced_any = True
        print(f"CONFIRMED synthesized FLAG [{case.name}]: {finding.discrepancy} "
              f"-> script reproduces {expected!r}")
        break

    assert reproduced_any, "no planted instance produced a reproducing FAIL"


@pytest.mark.skipif(not _HAS_KEY, reason="no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set")
def test_live_propose_returns_loadable_source():
    """The proposal step alone returns a structured, non-empty verifier source (smoke).

    Kept separate and minimal so the expensive end-to-end assertion lives in one place; this
    just confirms the forced-tool plumbing yields source we can materialize.
    """
    proposal = propose_verifier(_TOF_CLAIM, _TOF_EVIDENCE_EXAMPLE)
    assert proposal["judge_src"].strip()
    assert "Verifier" in proposal["judge_src"]
    verifier = materialize(proposal["judge_src"])
    assert verifier.manifest.kind is VerifierKind.SYNTHESIZED
