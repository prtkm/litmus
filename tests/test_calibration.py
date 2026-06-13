"""THE WS-A GATE — the calibration kernel admits/rejects verifiers with zero human labels.

This is the project's reward function (DESIGN §7, §8). It must:
  * admit the honest reference verifier (sum_check) as SCORING;
  * REJECT a non-deterministic judge (G4) — a hard invariant;
  * REJECT a verifier with no self_test (no calibration fuel);
  * REJECT a verifier whose FAIL ships a script that does NOT reproduce its expected output
        (G3) — "no script, no flag";
  * keep an over-FPR-but-otherwise-sound verifier as ADVISORY (not scoring, not rejected).

Every fixture is built from the real core classes (Verifier, VerifierManifest, Finding, ...),
so the gate is tested against real contracts, not mocks.
"""

from __future__ import annotations

import random

from litmus.core.calibration import AdmissionStatus, calibrate
from litmus.core.claim import (
    Claim,
    Evidence,
    EvidenceKind,
    EpistemicTier,
    Location,
)
from litmus.core.finding import (
    EvidencePacket,
    Severity,
    Status,
    VerifierKind,
)
from litmus.core.verifier import (
    Determinism,
    SelfTestCase,
    Verifier,
    VerifierManifest,
)
from litmus.verifiers.sum_check import SumCheck


# --- shared self_test fuel (4 clean + 4 planted, 2 claim types) --------------
def _evidence(name: str, parts: list, total) -> Evidence:
    return Evidence(
        id=f"ev_{name}",
        kind=EvidenceKind.TABLE,
        location=Location(section="self_test", quote=f"total {total}"),
        extracted_values={"parts": list(parts), "reported_total": total},
    )


def _claim(name: str) -> Claim:
    return Claim(id=f"claim_{name}", text="t", epistemic_tier=EpistemicTier.T0)


def _standard_cases() -> list[SelfTestCase]:
    """4 clean (correct) + 4 planted (wrong) across table_total and sum_claim."""
    specs = [
        ("c1", "clean", "table_total", [1, 2, 3], 6),
        ("c2", "clean", "table_total", [10, 20, 30], 60),
        ("c3", "clean", "sum_claim", [5, 5], 10),
        ("c4", "clean", "sum_claim", [7, 8, 9], 24),
        ("p1", "planted", "table_total", [1, 2, 3], 7),
        ("p2", "planted", "table_total", [10, 20, 30], 70),
        ("p3", "planted", "sum_claim", [5, 5], 11),
        ("p4", "planted", "sum_claim", [7, 8, 9], 25),
    ]
    cases = []
    for name, kind, ctype, parts, total in specs:
        cases.append(
            SelfTestCase(
                name=name,
                kind=kind,
                claim=_claim(name),
                evidence=[_evidence(name, parts, total)],
                claim_type=ctype,
            )
        )
    return cases


def _good_packet(reported, computed) -> EvidencePacket:
    """A correct recompute script + matching expected_output (reproduces in the sandbox)."""
    expected = f"MISMATCH reported={reported} computed={computed}"
    script = (
        f"print('MISMATCH reported={reported} computed={computed}')\n"
    )
    return EvidencePacket(recompute_script=script, expected_output=expected)


# =============================================================================
# 1. sum_check is admitted as SCORING.
# =============================================================================
def test_sum_check_is_scoring():
    card = calibrate(SumCheck())
    assert card.admission == AdmissionStatus.SCORING, card.reasons
    assert card.recall is not None and card.recall >= 0.9
    assert card.fpr_overall is not None and card.fpr_overall <= card.declared_fpr_ceiling
    assert card.deterministic is True
    assert card.reproducibility == 1.0
    # per-claim-type FPR (G6) all within ceiling
    assert all(v <= card.declared_fpr_ceiling for v in card.fpr_by_claim_type.values())
    assert card.gates["G1"] and card.gates["G2"] and card.gates["G3"]
    assert card.gates["G4"] and card.gates["G6"]


def test_sum_check_exercises_two_claim_types():
    """G6 only means something if >1 claim_type is present in the clean set."""
    card = calibrate(SumCheck())
    assert len(card.fpr_by_claim_type) >= 2


# =============================================================================
# 2. A non-deterministic judge is REJECTED via G4.
# =============================================================================
class NonDeterministicVerifier(Verifier):
    """Picks its verdict with an RNG -> violates G4. Has a self_test, so rejection is
    specifically about non-determinism, not missing fuel.

    It draws from ``random`` (the actual forbidden source, DESIGN §3.1) AND advances an
    instance call-counter that forces the verdict to flip between consecutive invocations.
    The counter guarantees the kernel observes differing output across its N runs, so the
    test asserts a hard fact rather than relying on a coin landing differently three times.
    """

    manifest = VerifierManifest(
        id="nondet.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["table_total", "sum_claim"],
        fpr_ceiling=0.05,
        description="non-deterministic on purpose",
    )

    def __init__(self):
        self._calls = 0

    def judge(self, claim, evidence):
        # Genuine RNG (what G4 is meant to catch) plus a counter so verdicts are guaranteed
        # to differ between runs -> the kernel always sees non-determinism (no flake).
        self._calls += 1
        flip = self._calls % 2 == 0
        if flip or random.random() < 0.5:
            return self.make_finding(
                claim=claim,
                status=Status.FAIL,
                severity=random.choice([Severity.A, Severity.B, Severity.C]),
                message="random fail",
                evidence=_good_packet(100, 97),
            )
        return self.make_finding(claim=claim, status=Status.PASS, message="random pass")

    def self_test(self):
        return _standard_cases()


def test_nondeterministic_verifier_is_rejected_g4():
    card = calibrate(NonDeterministicVerifier())
    assert card.admission == AdmissionStatus.REJECTED
    assert card.deterministic is False
    assert card.gates.get("G4") is False
    assert any("G4" in r for r in card.reasons)


# =============================================================================
# 3. A verifier with no self_test is REJECTED.
# =============================================================================
class NoSelfTestVerifier(Verifier):
    manifest = VerifierManifest(
        id="noselftest.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["table_total"],
        fpr_ceiling=0.05,
        description="ships no calibration fuel",
    )

    def judge(self, claim, evidence):
        return self.make_finding(claim=claim, status=Status.PASS, message="ok")

    def self_test(self):
        return []


def test_no_self_test_verifier_is_rejected():
    card = calibrate(NoSelfTestVerifier())
    assert card.admission == AdmissionStatus.REJECTED
    assert any("self_test" in r for r in card.reasons)


# =============================================================================
# 4. A FAIL whose script does NOT reproduce expected_output is REJECTED via G3.
# =============================================================================
class BadScriptVerifier(Verifier):
    """Deterministic, has fuel, catches all planted cases — but its FAIL ships a script
    whose stdout differs from expected_output, so reproducibility < 1 (G3 fail)."""

    manifest = VerifierManifest(
        id="badscript.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["table_total", "sum_claim"],
        fpr_ceiling=0.05,
        description="emits a non-reproducing recompute script",
    )

    def judge(self, claim, evidence):
        ev = evidence[0]
        vals = ev.extracted_values
        parts = vals["parts"]
        total = vals["reported_total"]
        computed = sum(parts)
        if computed == total:
            return self.make_finding(claim=claim, status=Status.PASS, message="ok")
        # The script prints WRONG_OUTPUT, but expected_output says otherwise.
        packet = EvidencePacket(
            recompute_script="print('WRONG_OUTPUT')\n",
            expected_output=f"MISMATCH reported={total} computed={computed}",
        )
        return self.make_finding(
            claim=claim,
            status=Status.FAIL,
            severity=Severity.B,
            message="mismatch",
            evidence=packet,
        )

    def self_test(self):
        return _standard_cases()


def test_bad_script_verifier_is_rejected_g3():
    card = calibrate(BadScriptVerifier())
    assert card.admission == AdmissionStatus.REJECTED
    assert card.gates.get("G3") is False
    assert card.reproducibility is not None and card.reproducibility < 1.0
    assert any("G3" in r for r in card.reasons)


# =============================================================================
# 5. An over-FPR (but deterministic + reproducible) verifier is ADVISORY.
# =============================================================================
class OverFPRVerifier(Verifier):
    """Catches the planted cases (good recall) AND flags some clean cases (FPR over ceiling).
    Deterministic, and every FAIL ships a reproducing script -> not rejected, just ADVISORY."""

    manifest = VerifierManifest(
        id="overfpr.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["table_total", "sum_claim"],
        fpr_ceiling=0.05,
        description="flags too eagerly; over its declared FPR ceiling",
    )

    def judge(self, claim, evidence):
        ev = evidence[0]
        vals = ev.extracted_values
        parts = vals["parts"]
        total = vals["reported_total"]
        computed = sum(parts)
        # Deterministic over-flagging: any total that is even gets flagged as FAIL,
        # regardless of correctness. (Several clean cases have even totals -> FPs.)
        if computed != total or total % 2 == 0:
            return self.make_finding(
                claim=claim,
                status=Status.FAIL,
                severity=Severity.B,
                message="flagged",
                reported=total,
                computed=computed,
                evidence=_good_packet(total, computed),
            )
        return self.make_finding(claim=claim, status=Status.PASS, message="ok")

    def self_test(self):
        return _standard_cases()


def test_over_fpr_verifier_is_advisory():
    card = calibrate(OverFPRVerifier())
    assert card.admission == AdmissionStatus.ADVISORY, card.reasons
    # It is deterministic and reproducible (so NOT rejected)...
    assert card.deterministic is True
    assert card.reproducibility == 1.0
    assert card.gates.get("G4") is True
    assert card.gates.get("G3") is True
    # ...but its measured FPR exceeds its declared ceiling (so NOT scoring).
    assert card.fpr_overall is not None and card.fpr_overall > card.declared_fpr_ceiling
    assert card.gates.get("G2") is False or card.gates.get("G6") is False


def test_over_fpr_verifier_recall_is_fine():
    """Sanity: the ADVISORY downgrade is due to FPR, not poor recall."""
    card = calibrate(OverFPRVerifier())
    assert card.recall is not None and card.recall >= 0.9
