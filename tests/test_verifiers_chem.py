"""Calibration + direct-judge tests for the chemistry/units verifier batch (DESIGN §6, §19 WS-D).

Each verifier must pass the full WS-A calibration gate as SCORING (DESIGN §7): deterministic
judge (G4), every emitted FAIL's recompute script reproduces its expected output in the network-
less sandbox (G3), recall >= 0.90 on planted errors (G1), and FPR within the declared ceiling
overall and per claim_type (G2/G6). On top of the gate we assert a few direct PASS/FAIL/ABSTAIN
verdicts so the verifiers' *semantics* (not just their self-consistency) are pinned.

Verifiers under test:
  * yield_check.v1      — T1 impossible-yield wedge (>100% / <0%) + T0 molar recompute.
  * mass_balance.v1     — T1 conservation of mass (output must not exceed input).
  * unit_consistency.v1 — T0 dual-unit equivalence under a fixed factor table.
"""

from __future__ import annotations

import pytest

from litmus.core.calibration import AdmissionStatus, calibrate
from litmus.core.claim import Claim, Evidence, EvidenceKind, EpistemicTier, Location
from litmus.core.finding import Severity, Status

from litmus.verifiers.yield_check import YieldCheck
from litmus.verifiers.mass_balance import MassBalance
from litmus.verifiers.unit_consistency import UnitConsistency


# --- small helpers ----------------------------------------------------------
def _claim(cid: str = "c") -> Claim:
    return Claim(id=cid, text="t", epistemic_tier=EpistemicTier.T0)


def _ev(values: dict, kind: EvidenceKind = EvidenceKind.NUMBER) -> Evidence:
    return Evidence(
        id="ev",
        kind=kind,
        location=Location(section="test", quote="q"),
        extracted_values=dict(values),
    )


# =============================================================================
# 1. Calibration: all three are admitted SCORING (the WS-A gate, DESIGN §7).
# =============================================================================
ALL_VERIFIERS = [YieldCheck, MassBalance, UnitConsistency]


@pytest.mark.parametrize("cls", ALL_VERIFIERS, ids=[c.manifest.id for c in ALL_VERIFIERS])
def test_verifier_is_scoring(cls):
    sc = calibrate(cls())
    assert sc.admission == AdmissionStatus.SCORING, (cls.manifest.id, sc.reasons)
    # The gate's individual measurements, spelled out (parallels test_calibration.py).
    assert sc.recall is not None and sc.recall >= 0.90, (cls.manifest.id, sc.recall)
    assert sc.fpr_overall is not None and sc.fpr_overall <= sc.declared_fpr_ceiling
    assert sc.deterministic is True
    assert sc.reproducibility == 1.0
    assert all(v <= sc.declared_fpr_ceiling for v in sc.fpr_by_claim_type.values())
    assert sc.gates["G1"] and sc.gates["G2"] and sc.gates["G3"]
    assert sc.gates["G4"] and sc.gates["G6"]


@pytest.mark.parametrize("cls", ALL_VERIFIERS, ids=[c.manifest.id for c in ALL_VERIFIERS])
def test_verifier_exercises_multiple_claim_types(cls):
    """G6 only means something with >1 claim_type in the clean set."""
    sc = calibrate(cls())
    assert len(sc.fpr_by_claim_type) >= 2, (cls.manifest.id, sc.fpr_by_claim_type)


# =============================================================================
# 2. yield_check — the flagship impossible-yield wedge (DESIGN §17).
# =============================================================================
def test_yield_over_100_fails_severity_a():
    f = YieldCheck().judge(_claim(), [_ev({"reported_yield_pct": 142.0})])
    assert f.status is Status.FAIL
    assert f.severity is Severity.A
    assert "100%" in f.message
    assert f.validate() == []  # ships a recompute script + expected_output + severity
    assert f.evidence.expected_output == "IMPOSSIBLE_YIELD reported=142 max=100"


def test_yield_negative_fails_severity_a():
    f = YieldCheck().judge(_claim(), [_ev({"reported_yield_pct": -5.0})])
    assert f.status is Status.FAIL
    assert f.severity is Severity.A
    assert "negative" in f.message.lower()
    assert f.evidence.expected_output == "NEGATIVE_YIELD reported=-5"


def test_yield_within_bound_passes():
    f = YieldCheck().judge(_claim(), [_ev({"reported_yield_pct": 87.5})])
    assert f.status is Status.PASS


def test_yield_exactly_100_passes():
    f = YieldCheck().judge(_claim(), [_ev({"reported_yield_pct": 100.0})])
    assert f.status is Status.PASS


def test_yield_molar_recompute_mismatch_is_severity_b():
    # 0.50 mol product / (1.0 mol limiting * 1.0) -> 50%, not the claimed 80%.
    ev = _ev({
        "reported_yield_pct": 80.0,
        "mol_product": 0.50,
        "mol_limiting_reagent": 1.0,
        "stoich_coeff_ratio": 1.0,
    })
    f = YieldCheck().judge(_claim(), [ev])
    assert f.status is Status.FAIL
    assert f.severity is Severity.B
    assert f.evidence.expected_output == "YIELD_MISMATCH reported=80 computed=50"


def test_yield_molar_recompute_match_passes():
    ev = _ev({
        "reported_yield_pct": 85.0,
        "mol_product": 0.85,
        "mol_limiting_reagent": 1.0,
        "stoich_coeff_ratio": 1.0,
    })
    f = YieldCheck().judge(_claim(), [ev])
    assert f.status is Status.PASS


def test_yield_over_100_beats_recompute():
    """The T1 physical bound is checked before the T0 recompute: >100% is severity A even when
    molar quantities are also present."""
    ev = _ev({
        "reported_yield_pct": 142.0,
        "mol_product": 1.42,
        "mol_limiting_reagent": 1.0,
    })
    f = YieldCheck().judge(_claim(), [ev])
    assert f.status is Status.FAIL and f.severity is Severity.A


def test_yield_abstains_without_data():
    f = YieldCheck().judge(_claim(), [_ev({"unrelated": 1})])
    assert f.status is Status.INCONCLUSIVE


# =============================================================================
# 3. mass_balance — conservation of mass.
# =============================================================================
def test_mass_created_fails_severity_a_lists():
    ev = _ev({"reactant_masses": [3.0], "product_masses": [5.0]}, EvidenceKind.TABLE)
    f = MassBalance().judge(_claim(), [ev])
    assert f.status is Status.FAIL
    assert f.severity is Severity.A
    assert f.evidence.expected_output == "MASS_CREATED input=3 output=5"


def test_mass_created_fails_pair():
    ev = _ev({"input_mass": 3.0, "recovered_mass": 5.0}, EvidenceKind.TABLE)
    f = MassBalance().judge(_claim(), [ev])
    assert f.status is Status.FAIL and f.severity is Severity.A
    assert f.evidence.expected_output == "MASS_CREATED input=3 output=5"


def test_mass_loss_passes():
    ev = _ev({"reactant_masses": [10.0, 5.0], "product_masses": [12.0]}, EvidenceKind.TABLE)
    assert MassBalance().judge(_claim(), [ev]).status is Status.PASS


def test_mass_exact_equality_passes():
    ev = _ev({"input_mass": 4.0, "recovered_mass": 4.0}, EvidenceKind.TABLE)
    assert MassBalance().judge(_claim(), [ev]).status is Status.PASS


def test_mass_abstains_without_pair():
    # A lone input with no recovered mass is not a checkable pair.
    ev = _ev({"input_mass": 4.0}, EvidenceKind.TABLE)
    assert MassBalance().judge(_claim(), [ev]).status is Status.INCONCLUSIVE


# =============================================================================
# 4. unit_consistency — dual-unit equivalence.
# =============================================================================
def test_unit_mismatch_fails_severity_b():
    # "5.0 mg (0.5 g)" — off by 100x.
    ev = _ev({"value_a": 5.0, "unit_a": "mg", "value_b": 0.5, "unit_b": "g"})
    f = UnitConsistency().judge(_claim(), [ev])
    assert f.status is Status.FAIL
    assert f.severity is Severity.B
    assert f.validate() == []
    assert f.evidence.expected_output == "UNIT_MISMATCH a=0.005 b=0.5"


def test_unit_match_passes():
    ev = _ev({"value_a": 5.0, "unit_a": "mg", "value_b": 0.005, "unit_b": "g"})
    assert UnitConsistency().judge(_claim(), [ev]).status is Status.PASS


def test_unit_micro_sign_match_passes():
    ev = _ev({"value_a": 1000.0, "unit_a": "µg", "value_b": 1.0, "unit_b": "mg"})
    assert UnitConsistency().judge(_claim(), [ev]).status is Status.PASS


def test_unit_unknown_abstains():
    ev = _ev({"value_a": 5.0, "unit_a": "furlong", "value_b": 0.005, "unit_b": "g"})
    assert UnitConsistency().judge(_claim(), [ev]).status is Status.INCONCLUSIVE


def test_unit_cross_dimension_abstains():
    # mass vs length — a malformed equivalence, not a finding.
    ev = _ev({"value_a": 5.0, "unit_a": "mg", "value_b": 5.0, "unit_b": "mm"})
    assert UnitConsistency().judge(_claim(), [ev]).status is Status.INCONCLUSIVE


def test_unit_time_mismatch_fails():
    # 120 min is 2 h, not 1 h.
    ev = _ev({"value_a": 120.0, "unit_a": "min", "value_b": 1.0, "unit_b": "h"})
    f = UnitConsistency().judge(_claim(), [ev])
    assert f.status is Status.FAIL and f.severity is Severity.B
    assert f.evidence.expected_output == "UNIT_MISMATCH a=7200 b=3600"
