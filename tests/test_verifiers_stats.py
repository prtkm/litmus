"""Tests for the first-party statistics verifiers (DESIGN §5 T0, §19 WS-D).

Covers ``statcheck.v1`` (recompute a two-tailed p from a test statistic + df) and ``grim.v1``
(GRIM granularity check on a reported mean of integer responses). Both are pure, deterministic
T0 verifiers; these tests pin (a) the math against the standard anchors, (b) the PASS / FAIL /
ABSTAIN contract, (c) byte-identical recompute-script reproduction in the sandbox, and (d) that
each is admitted SCORING by the calibration kernel (DESIGN §7).
"""

from __future__ import annotations

import math

import pytest

from litmus.core import sandbox
from litmus.core.calibration import AdmissionStatus, calibrate
from litmus.core.claim import Claim, Evidence, EvidenceKind, Location
from litmus.core.finding import Severity, Status
from litmus.verifiers import grim as grim_mod
from litmus.verifiers import statcheck as statcheck_mod
from litmus.verifiers.grim import Grim
from litmus.verifiers.statcheck import StatCheck, _p_two_tailed


# --- helpers ----------------------------------------------------------------
def _claim(cid: str = "c") -> Claim:
    return Claim(id=cid, text="x", location=Location(section="t"))


def _stat_ev(**vals) -> Evidence:
    return Evidence(
        id="e",
        kind=EvidenceKind.STATISTIC,
        location=Location(section="t", quote="stat"),
        extracted_values=dict(vals),
    )


def _grim_ev(**vals) -> Evidence:
    return Evidence(
        id="e",
        kind=EvidenceKind.NUMBER,
        location=Location(section="t", quote="mean"),
        extracted_values=dict(vals),
    )


def _judge_stat(**vals):
    return StatCheck().judge(_claim(), [_stat_ev(**vals)])


def _judge_grim(**vals):
    return Grim().judge(_claim(), [_grim_ev(**vals)])


# ===========================================================================
# statcheck.v1 — the math (anchors)
# ===========================================================================
class TestStatcheckMath:
    """The pure-stdlib CDFs must hit the standard two-tailed anchors (DESIGN §5 T0)."""

    @pytest.mark.parametrize(
        "test, stat, df, df1, df2, expected",
        [
            ("z", 1.95996, None, None, None, 0.05),       # standard normal 0.05 anchor
            ("t", 2.085963, 20, None, None, 0.05),        # t(20) 0.05 anchor
            ("chi2", 3.84146, 1, None, None, 0.05),       # chi2(1) 0.05 anchor
            ("F", 4.35125, None, 1, 20, 0.05),            # F(1,20) 0.05 anchor
            ("t", 2.0, 20, None, None, 0.0593),           # the "looks-significant-but-isn't" case
        ],
    )
    def test_anchor_p_values(self, test, stat, df, df1, df2, expected):
        p = _p_two_tailed(test, stat, df, df1, df2)
        assert p == pytest.approx(expected, abs=5e-4), f"{test} anchor off: {p}"

    def test_chi2_one_df_equals_z_squared(self):
        """chi2 with 1 df is the square of a standard normal: identical two-tailed p."""
        z = 1.95996
        p_z = _p_two_tailed("z", z, None, None, None)
        p_chi2 = _p_two_tailed("chi2", z * z, 1, None, None)
        assert p_chi2 == pytest.approx(p_z, abs=1e-9)

    def test_F_one_numerator_df_equals_t_squared(self):
        """F(1, k) is t(k) squared: identical two-tailed p."""
        p_t = _p_two_tailed("t", 2.0, 20, None, None)
        p_F = _p_two_tailed("F", 4.0, None, 1, 20)
        assert p_F == pytest.approx(p_t, abs=1e-9)

    def test_r_maps_through_t(self):
        """r is recomputed via t = r*sqrt(df/(1-r^2)); check it agrees with the explicit t."""
        r, df = 0.5, 18
        t = r * math.sqrt(df / (1.0 - r * r))
        p_r = _p_two_tailed("r", r, df, None, None)
        p_t = _p_two_tailed("t", t, df, None, None)
        assert p_r == pytest.approx(p_t, abs=1e-12)

    @pytest.mark.parametrize(
        "test, stat, df, df1, df2",
        [
            ("r", 1.0, 18, None, None),    # |r| == 1 -> undefined
            ("r", 1.5, 18, None, None),    # |r| > 1
            ("t", 2.0, 0, None, None),     # df <= 0
            ("chi2", -1.0, 1, None, None),  # negative chi2
            ("F", -1.0, None, 1, 20),      # negative F
        ],
    )
    def test_out_of_domain_returns_none(self, test, stat, df, df1, df2):
        assert _p_two_tailed(test, stat, df, df1, df2) is None


# ===========================================================================
# statcheck.v1 — the verdict contract
# ===========================================================================
class TestStatcheckJudge:
    def test_correct_rounding_passes(self):
        # t(20)=2.085963 -> p≈0.0500, reported as 0.05 (2 dp): a correct rounding.
        f = _judge_stat(test="t", statistic=2.085963, df=20, reported_p=0.05, decimals=2)
        assert f.status is Status.PASS

    def test_flagship_decision_flip_fails(self):
        # t(20)=2.0 -> p≈0.0593 (ns), reported significant 0.04: a decision error.
        f = _judge_stat(test="t", statistic=2.0, df=20, reported_p=0.04, decimals=2)
        assert f.status is Status.FAIL
        assert f.severity is Severity.B
        assert f.details["decision_flip"] is True
        assert f.evidence.recompute_script and f.evidence.expected_output
        assert "INCONSISTENT" in f.evidence.expected_output
        assert not f.validate()  # a valid FAIL ships script + expected_output + severity

    def test_large_gap_without_flip_fails_severity_c(self):
        # The real-paper material case: t(41)=3.6 -> p≈0.00085; reported 0.013. Both are
        # "significant" (no .05 decision flip), but the reported p is ~15x the achievable p — no
        # rounding of the statistic can produce it. That is a genuine (severity C) inconsistency,
        # graded below a decision error but well above the rounding-tail nitpicks (DESIGN §6.3).
        f = _judge_stat(test="t", statistic=3.6, df=41, reported_p=0.013)
        assert f.status is Status.FAIL
        assert f.severity is Severity.C
        assert f.details["decision_flip"] is False
        assert f.details["rel_err"] > 0.25
        assert not f.validate()

    def test_statistic_rounding_tail_passes(self):
        # The owner's nitpick: F(2,41)=0.02 printed p=0.974. The 2-dp statistic admits true F in
        # [0.015, 0.025], whose achievable p covers ~[0.975, 0.985]; the printed 0.974 is the same
        # value to within presentation rounding (~0.6% off, decision intact). The OLD point-
        # recompute flagged this as p=0.980; the rounding-aware + impact-graded check must PASS it.
        f = _judge_stat(test="F", statistic=0.02, df1=2, df2=41, reported_p=0.974)
        assert f.status is Status.PASS
        assert f.details["rel_err"] < 0.25  # tiny gap -> below the flag bar
        lo, hi = f.details["achievable_p_range"]
        assert lo > 0.9 and hi < 1.0  # the achievable p-range is reported for the reader

    def test_small_gap_without_flip_passes(self):
        # F(1,41)=0.86 -> point p≈0.359 but printed p=0.375 — outside the statistic's rounding band
        # yet only ~4.4% off with the decision intact. Below the severity-C relative-error bar, so
        # PASS: a trivial gap is not worth a reader's time (DESIGN §3.4: abstain/PASS > guess).
        f = _judge_stat(test="F", statistic=0.86, df1=1, df2=41, reported_p=0.375)
        assert f.status is Status.PASS

    def test_z_needs_no_df(self):
        f = _judge_stat(test="z", statistic=1.95996, reported_p=0.05, decimals=2)
        assert f.status is Status.PASS

    def test_F_uses_df1_df2(self):
        f = _judge_stat(test="F", statistic=4.35125, df1=1, df2=20, reported_p=0.05, decimals=2)
        assert f.status is Status.PASS

    def test_decimals_inferred_from_reported_p(self):
        # No explicit decimals: 0.001 implies 3 dp. z=3.0 -> p≈0.0027 -> rounds to 0.003.
        f = _judge_stat(test="z", statistic=3.0, reported_p=0.003)
        assert f.status is Status.PASS
        assert f.details["rep_decimals"] == 3

    def test_binds_variant_extracted_value_keys(self):
        """Lenient key matching: p / pval and stat / bare distribution letter still bind
        (DESIGN §11 key-alignment; robustness fixed in the verifier, not the extraction prompt)."""
        # reported_p arrives as 'p', statistic as 'stat'.
        f = _judge_stat(test="t", stat=2.0, df=20, p=0.04)
        assert f.status is Status.FAIL and f.details["decision_flip"] is True
        # statistic under the bare distribution-letter key, p under 'p', no explicit 'test'.
        f = StatCheck().judge(_claim(), [_stat_ev(F=4.35125, df1=1, df2=20, p_value=0.05)])
        assert f.status is Status.PASS
        # 'pval' spelling.
        f = _judge_stat(test="chi2", statistic=3.0, df=1, pval=0.04)
        assert f.status is Status.FAIL and f.severity is Severity.B

    @pytest.mark.parametrize(
        "vals",
        [
            {},  # nothing
            {"test": "t", "statistic": 2.0, "reported_p": 0.05},  # missing df for t
            {"test": "wilcoxon", "statistic": 2.0, "df": 10, "reported_p": 0.05},  # unsupported
            {"test": "F", "statistic": 4.0, "df1": 1, "reported_p": 0.05},  # F missing df2
            {"test": "t", "statistic": 2.0, "df": 18, "reported_p": 0.05, "tail": "one"},  # one-tailed
            {"test": "r", "statistic": 1.5, "df": 18, "reported_p": 0.05},  # |r|>1 -> domain abstain
        ],
    )
    def test_abstains_when_unbindable_or_out_of_domain(self, vals):
        f = StatCheck().judge(_claim(), [_stat_ev(**vals)])
        assert f.status is Status.INCONCLUSIVE
        assert not f.is_flag

    def test_fail_script_reproduces_byte_identical(self):
        f = _judge_stat(test="t", statistic=2.5, df=18, reported_p=0.20, decimals=2)
        assert f.status is Status.FAIL
        reproduced, res = sandbox.reproduces(
            f.evidence.recompute_script, f.evidence.expected_output
        )
        assert reproduced, f"stdout={res.stdout!r} stderr={res.stderr!r}"
        # The design's flagship statcheck line shape.
        assert f.evidence.expected_output.startswith("INCONSISTENT test=t stat=2.5 df=18")

    def test_pass_script_prints_ok(self):
        # Even on a PASS the (no) flag is consistent; here we synthesize the script via a FAIL,
        # then confirm a clean input drives the embedded logic to OK.
        f = _judge_stat(test="t", statistic=2.0, df=20, reported_p=0.04, decimals=2)
        # Mutate the hardcoded REPORTED_P in the script to the true (correctly-rounded) p:
        script = f.evidence.recompute_script.replace("REPORTED_P = 0.04", "REPORTED_P = 0.06")
        res = sandbox.run_script(script)
        assert res.ok and res.stdout.strip() == "OK"


# ===========================================================================
# grim.v1 — the verdict contract
# ===========================================================================
class TestGrimJudge:
    def test_classic_impossible_mean_fails(self):
        # The canonical GRIM example: mean 3.46 is unreachable for n=20 (69/20=3.45, 70/20=3.50).
        f = _judge_grim(reported_mean=3.46, n=20, decimals=2)
        assert f.status is Status.FAIL
        assert f.severity is Severity.B
        assert f.evidence.expected_output == "GRIM-INCONSISTENT mean=3.46 n=20 nearest=3.45(=69/20)"
        assert f.details["nearest_total"] == 69
        assert not f.validate()

    @pytest.mark.parametrize(
        "mean, n, total",
        [(3.45, 20, 69), (2.50, 4, 10), (3.33, 3, 10), (1.25, 8, 10), (2.70, 10, 27)],
    )
    def test_achievable_means_pass(self, mean, n, total):
        f = _judge_grim(reported_mean=mean, n=n, decimals=2)
        assert f.status is Status.PASS
        assert f.details["witness_total"] == total

    @pytest.mark.parametrize(
        "mean, n",
        [(2.60, 4), (3.30, 3), (1.20, 8), (3.45, 5), (0.50, 7)],
    )
    def test_impossible_means_fail(self, mean, n):
        f = _judge_grim(reported_mean=mean, n=n, decimals=2)
        assert f.status is Status.FAIL

    def test_multi_item_granularity(self):
        # n=5, n_items=2 -> granularity 10. 33/10 = 3.30 achievable; 3.35 is not.
        ok = _judge_grim(reported_mean=3.30, n=5, n_items=2, decimals=2)
        assert ok.status is Status.PASS
        bad = _judge_grim(reported_mean=3.35, n=5, n_items=2, decimals=2)
        assert bad.status is Status.FAIL
        assert "items=2" in bad.evidence.expected_output

    def test_abstains_when_no_power(self):
        # granularity n*items must be <= 2*10^decimals to have power. n=500, 2dp -> grid finer
        # than precision -> abstain (every 2-dp mean is reachable).
        f = _judge_grim(reported_mean=3.46, n=500, decimals=2)
        assert f.status is Status.INCONCLUSIVE
        assert "power" in f.message

    @pytest.mark.parametrize(
        "vals",
        [
            {},  # nothing
            {"reported_mean": 3.46},  # missing n
            {"reported_mean": 3.46, "n": 0},  # n <= 0
            {"reported_mean": 3.46, "n": 20.5},  # non-integer n
            {"n": 20},  # missing mean
        ],
    )
    def test_abstains_when_unbindable(self, vals):
        f = Grim().judge(_claim(), [_grim_ev(**vals)])
        assert f.status is Status.INCONCLUSIVE
        assert not f.is_flag

    def test_fail_script_reproduces_byte_identical(self):
        f = _judge_grim(reported_mean=2.60, n=4, decimals=2)
        assert f.status is Status.FAIL
        reproduced, res = sandbox.reproduces(
            f.evidence.recompute_script, f.evidence.expected_output
        )
        assert reproduced, f"stdout={res.stdout!r} stderr={res.stderr!r}"

    def test_achievable_script_prints_ok(self):
        f = _judge_grim(reported_mean=2.60, n=4, decimals=2)  # FAIL, to get a script
        script = f.evidence.recompute_script.replace(
            "REPORTED_MEAN = 2.6", "REPORTED_MEAN = 2.5"
        )
        res = sandbox.run_script(script)
        assert res.ok and res.stdout.strip() == "OK"


# ===========================================================================
# determinism (DESIGN §7 G4) — judge is a pure function
# ===========================================================================
class TestDeterminism:
    def test_statcheck_judge_is_deterministic(self):
        ev = [_stat_ev(test="t", statistic=2.0, df=20, reported_p=0.04, decimals=2)]
        outs = {StatCheck().judge(_claim(), ev).to_dict()["discrepancy"] for _ in range(5)}
        assert len(outs) == 1

    def test_grim_judge_is_deterministic(self):
        ev = [_grim_ev(reported_mean=3.46, n=20, decimals=2)]
        outs = {Grim().judge(_claim(), ev).evidence.expected_output for _ in range(5)}
        assert len(outs) == 1


# ===========================================================================
# self_test fuel + calibration admission (DESIGN §6.3, §7)
# ===========================================================================
class TestSelfTestAndCalibration:
    @pytest.mark.parametrize("verifier_cls", [StatCheck, Grim])
    def test_self_test_has_enough_clean_and_planted(self, verifier_cls):
        cases = verifier_cls().self_test()
        clean = [c for c in cases if c.kind == "clean"]
        planted = [c for c in cases if c.kind == "planted"]
        assert len(clean) >= 6, f"{verifier_cls.__name__}: only {len(clean)} clean"
        assert len(planted) >= 6, f"{verifier_cls.__name__}: only {len(planted)} planted"

    @pytest.mark.parametrize("verifier_cls", [StatCheck, Grim])
    def test_self_test_labels_are_honest(self, verifier_cls):
        """Every clean case PASSes its own judge; every planted case FAILs it (DESIGN §6.3)."""
        v = verifier_cls()
        for case in v.self_test():
            f = v.judge(case.claim, case.evidence)
            assert f.status is case.expected_status, (
                f"{case.name}: judged {f.status} but labelled {case.kind}"
            )

    @pytest.mark.parametrize("verifier_cls", [StatCheck, Grim])
    def test_admitted_scoring(self, verifier_cls):
        card = calibrate(verifier_cls())
        assert card.admission is AdmissionStatus.SCORING, (
            f"{verifier_cls.__name__} not SCORING: {card.reasons} {card.details}"
        )
        assert card.recall >= 0.90
        assert card.fpr_overall <= card.declared_fpr_ceiling
        assert card.deterministic is True
        assert card.reproducibility == 1.0

    def test_statcheck_spans_multiple_distributions(self):
        """The self_test must exercise t, F, chi2, z (DESIGN §5 T0) so G6 covers distributions."""
        tests = {
            c.evidence[0].extracted_values["test"] for c in StatCheck().self_test()
        }
        assert {"t", "F", "chi2", "z"}.issubset(tests)


# ===========================================================================
# discovery — both modules export VERIFIERS for the registry (DESIGN §9, §19 WS-D)
# ===========================================================================
def test_modules_export_verifiers():
    assert [type(v).__name__ for v in statcheck_mod.VERIFIERS] == ["StatCheck"]
    assert [type(v).__name__ for v in grim_mod.VERIFIERS] == ["Grim"]
    assert statcheck_mod.VERIFIERS[0].manifest.id == "statcheck.v1"
    assert grim_mod.VERIFIERS[0].manifest.id == "grim.v1"
