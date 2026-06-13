"""WS-E T2 cross-consistency verifiers (DESIGN §5 frontier, §19 WS-E).

Two deterministic T2 verifiers — ``prose_vs_table.v1`` and ``figure_vs_table.v1`` — that judge a
bound prose/figure value against the paper's own table value. The model binds claim↔number; this
code judges (DESIGN §5: "exact once bound"). These tests pin:

  * the WS-E GATE — each verifier calibrates to ``AdmissionStatus.SCORING`` (DESIGN §7, §19);
  * the verdict contract — PASS when values agree, FAIL (severity B) + executable evidence when
        they don't, ABSTAIN when a value is missing (DESIGN §3.4);
  * the archetypes (DESIGN §5) — "+40% vs 36%", "the majority vs n=180/400", "bar=60 vs table=50";
  * G3 reproducibility — every emitted FAIL's recompute_script reproduces its expected_output
        byte-for-byte in the real recompute sandbox (DESIGN §3.2: no script, no flag).

Everything here is offline (no API): these verifiers are pure deterministic code.
"""

from __future__ import annotations

import pytest

from litmus.core import sandbox
from litmus.core.calibration import AdmissionStatus, calibrate
from litmus.core.claim import Claim, Evidence, EvidenceKind, EpistemicTier, Location
from litmus.core.finding import Severity, Status
from litmus.verifiers.figure_vs_table import FigureVsTable
from litmus.verifiers.prose_vs_table import ProseVsTable

# The two verifiers under test, with the consume key each FAIL/PASS test drives them through.
_VERIFIERS = [ProseVsTable(), FigureVsTable()]


# --- tiny evidence/claim builders -------------------------------------------
def _claim(cid: str = "c1", tier: EpistemicTier = EpistemicTier.T2) -> Claim:
    return Claim(id=cid, text="t", location=Location(section="body"), epistemic_tier=tier)


def _prose_ev(prose, source, **extra) -> Evidence:
    vals = {"prose_value": prose, "source_value": source, **extra}
    return Evidence(
        id="ev_prose",
        kind=EvidenceKind.TABLE,
        location=Location(section="body", quote="prose vs table"),
        extracted_values=vals,
    )


def _figure_ev(figure, table, **extra) -> Evidence:
    vals = {"figure_value": figure, "table_value": table, **extra}
    return Evidence(
        id="ev_figure",
        kind=EvidenceKind.FIGURE,
        location=Location(section="Figure 2", quote="figure vs table"),
        extracted_values=vals,
    )


# =============================================================================
# 1. THE WS-E GATE — both verifiers calibrate to SCORING (DESIGN §7, §19).
# =============================================================================
@pytest.mark.parametrize("verifier", _VERIFIERS, ids=lambda v: v.manifest.id)
def test_verifier_calibrates_to_scoring(verifier):
    card = calibrate(verifier)
    assert card.admission == AdmissionStatus.SCORING, card.reasons
    assert card.recall is not None and card.recall >= 0.9
    assert card.fpr_overall is not None and card.fpr_overall <= card.declared_fpr_ceiling
    assert card.deterministic is True
    assert card.reproducibility == 1.0
    assert all(v <= card.declared_fpr_ceiling for v in card.fpr_by_claim_type.values())
    assert card.gates["G1"] and card.gates["G2"] and card.gates["G3"]
    assert card.gates["G4"] and card.gates["G6"]


@pytest.mark.parametrize("verifier", _VERIFIERS, ids=lambda v: v.manifest.id)
def test_verifier_exercises_two_claim_types(verifier):
    """G6 only means something with >1 claim_type in the clean set."""
    card = calibrate(verifier)
    assert len(card.fpr_by_claim_type) >= 2


@pytest.mark.parametrize("verifier", _VERIFIERS, ids=lambda v: v.manifest.id)
def test_self_test_has_enough_fuel(verifier):
    """>=6 clean and >=6 planted, as the build spec requires."""
    cases = verifier.self_test()
    clean = [c for c in cases if c.kind == "clean"]
    planted = [c for c in cases if c.kind == "planted"]
    assert len(clean) >= 6
    assert len(planted) >= 6


@pytest.mark.parametrize("verifier", _VERIFIERS, ids=lambda v: v.manifest.id)
def test_manifest_is_t2_deterministic(verifier):
    m = verifier.manifest
    assert m.epistemic_tier is EpistemicTier.T2
    assert "t2" in m.capability_tags
    assert "consistency" in m.capability_tags


# =============================================================================
# 2. prose_vs_table.v1 — verdict contract + archetypes (DESIGN §5).
# =============================================================================
def test_prose_pass_when_values_agree():
    f = ProseVsTable().judge(_claim(), [_prose_ev(36.0, 36.1, quantity="improvement_pct")])
    assert f.status is Status.PASS
    assert f.severity is None
    assert f.validate() == []


def test_prose_fail_forty_vs_thirtysix_archetype():
    """The flagship '+40% in prose vs 36% in the table' (DESIGN §5)."""
    ev = _prose_ev(40.0, 36.0, quantity="improvement_pct")
    f = ProseVsTable().judge(_claim(), [ev])
    assert f.status is Status.FAIL
    assert f.severity is Severity.B
    assert f.reported == 40.0 and f.computed == 36.0
    assert f.validate() == []  # FAIL ships script + expected_output + severity
    assert f.evidence.expected_output == "PROSE-TABLE MISMATCH quantity=improvement_pct prose=40 table=36"


def test_prose_fail_majority_vs_180_of_400_archetype():
    """'the majority' (operationalized 0.5) vs n=180/400 = 0.45 (DESIGN §5)."""
    ev = _prose_ev(0.5, 0.45, quantity="responder_fraction")
    f = ProseVsTable().judge(_claim(), [ev])
    assert f.status is Status.FAIL
    assert f.severity is Severity.B
    assert "responder_fraction" in (f.evidence.expected_output or "")


def test_prose_abstain_when_value_missing():
    ev = Evidence(
        id="ev",
        kind=EvidenceKind.TABLE,
        location=Location(section="body"),
        extracted_values={"prose_value": 40.0},  # no source_value
    )
    f = ProseVsTable().judge(_claim(), [ev])
    assert f.status is Status.INCONCLUSIVE
    assert f.severity is None


def test_prose_custom_rel_tol_respected():
    """A looser rel_tol turns a would-be FAIL into a PASS (and vice-versa)."""
    # 0.45 vs 0.46 disagrees under the default 1% but agrees under a 5% tol.
    strict = ProseVsTable().judge(_claim(), [_prose_ev(0.46, 0.45, quantity="frac")])
    loose = ProseVsTable().judge(_claim(), [_prose_ev(0.46, 0.45, quantity="frac", rel_tol=0.05)])
    assert strict.status is Status.FAIL
    assert loose.status is Status.PASS


# =============================================================================
# 3. figure_vs_table.v1 — verdict contract + archetype (DESIGN §5).
# =============================================================================
def test_figure_pass_within_reading_tolerance():
    """A 1% reading slack on a bar the table says is 50 -> PASS under the 2% default."""
    f = FigureVsTable().judge(_claim(), [_figure_ev(50.5, 50.0, quantity="bar_height")])
    assert f.status is Status.PASS
    assert f.validate() == []


def test_figure_fail_bar_60_vs_table_50_archetype():
    """A bar plotted at 60 against a table that reports 50 (DESIGN §5 figure frontier)."""
    f = FigureVsTable().judge(_claim(), [_figure_ev(60.0, 50.0, quantity="bar_height")])
    assert f.status is Status.FAIL
    assert f.severity is Severity.B
    assert f.reported == 60.0 and f.computed == 50.0
    assert f.evidence.expected_output == "FIGURE-TABLE MISMATCH quantity=bar_height figure=60 table=50"
    assert f.validate() == []


def test_figure_abstain_when_value_missing():
    ev = Evidence(
        id="ev",
        kind=EvidenceKind.FIGURE,
        location=Location(section="Figure 2"),
        extracted_values={"figure_value": 60.0},  # no table_value
    )
    f = FigureVsTable().judge(_claim(), [ev])
    assert f.status is Status.INCONCLUSIVE


def test_figure_looser_default_tolerance_than_prose():
    """A 1.5% reading deviation passes the figure verifier (2% tol) but fails prose (1% tol)."""
    fig = FigureVsTable().judge(_claim(), [_figure_ev(50.75, 50.0, quantity="x")])  # +1.5%
    pro = ProseVsTable().judge(_claim(), [_prose_ev(50.75, 50.0, quantity="x")])    # +1.5%
    assert fig.status is Status.PASS
    assert pro.status is Status.FAIL


# =============================================================================
# 4. G3 reproducibility — every FAIL's script reproduces byte-for-byte (DESIGN §3.2).
# =============================================================================
@pytest.mark.parametrize(
    "verifier, ev",
    [
        (ProseVsTable(), _prose_ev(40.0, 36.0, quantity="improvement_pct")),
        (ProseVsTable(), _prose_ev(0.5, 0.45, quantity="responder_fraction")),
        (FigureVsTable(), _figure_ev(60.0, 50.0, quantity="bar_height")),
        (FigureVsTable(), _figure_ev(1.0, 3.0, quantity="error_bar_sd")),
    ],
    ids=["prose_pct", "prose_majority", "figure_bar", "figure_errorbar"],
)
def test_fail_script_reproduces_in_sandbox(verifier, ev):
    f = verifier.judge(_claim(), [ev])
    assert f.status is Status.FAIL
    script = f.evidence.recompute_script or ""
    expected = f.evidence.expected_output or ""
    reproduced, result = sandbox.reproduces(script, expected)
    assert reproduced, f"script did not reproduce: stdout={result.stdout!r} stderr={result.stderr!r}"
    # Deterministic across runs (G4 on the script itself).
    again = sandbox.run_script(script)
    assert again.stdout.strip() == result.stdout.strip()


def test_pass_finding_has_no_script():
    """A PASS carries no executable evidence (only FAILs ship a script)."""
    f = ProseVsTable().judge(_claim(), [_prose_ev(36.0, 36.0, quantity="x")])
    assert f.status is Status.PASS
    assert not f.evidence.recompute_script


# =============================================================================
# 5. Registry discovery — both verifiers are auto-discovered as first-party.
# =============================================================================
def test_both_verifiers_discovered_by_registry():
    from litmus.commons.registry import build_default_registry

    reg = build_default_registry()
    ids = reg.ids()
    assert "prose_vs_table.v1" in ids
    assert "figure_vs_table.v1" in ids
    # Routing: each is reachable by its primary consume key (DESIGN §12).
    assert any(v.manifest.id == "prose_vs_table.v1" for v in reg.for_claim_type("prose_vs_table"))
    assert any(v.manifest.id == "figure_vs_table.v1" for v in reg.for_claim_type("figure_vs_table"))
