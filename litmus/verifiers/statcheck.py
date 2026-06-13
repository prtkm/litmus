"""``statcheck.v1`` — a real T0 verifier: does a reported p-value match its test statistic?

The flagship "p reported as 0.04 but t=2.0, df=18 gives p=0.058" archetype (DESIGN §5 T0,
§6.1 class A, §19 WS-D). This is the canonical statcheck move (DESIGN §1: "statcheck for every
field"), reimplemented from first principles, not wrapped (DESIGN §3.7): given a reported test
statistic, its degrees of freedom, and a reported two-tailed p, we *recompute* p from the
statistic with our own pure-Python CDFs and flag when the reported p is not a correct rounding
of the truth. Pure arithmetic on the paper's own transcribed numbers — no external knowledge,
no model in the loop (DESIGN §3.1). A close cousin of the T0 recompute core (``sum_check``,
``percent_change``).

The distributions are implemented in **pure stdlib ``math`` only** (no scipy/numpy, DESIGN §3.7):

  * **z**     two-tailed p = ``2*(1 - Phi(|z|))`` with ``Phi`` via ``math.erf``.
  * **t**     p = ``I_x(df/2, 1/2)`` with ``x = df/(df + t^2)``  (regularized incomplete beta).
  * **F**     p = ``I_x(df2/2, df1/2)`` with ``x = df2/(df2 + df1*F)``.
  * **chi2**  p = ``Q(df/2, x/2)`` (regularized upper incomplete gamma).
  * **r**     mapped to t via ``t = r*sqrt(df/(1-r^2))`` then the t branch.

The regularized incomplete beta uses the Numerical-Recipes ``betacf`` continued fraction plus
``math.lgamma``; the regularized incomplete gamma uses the series (P) / continued fraction (Q)
split at ``x < a+1``. Verified against the standard two-tailed-0.05 anchors (z=1.95996,
t=2.085963/df=20, chi2=3.84146/df=1, F=4.35125/df1=1,df2=20) and t=2.0/df=20 -> 0.0593.

Contract with the extractor (DESIGN §11): a claim of type ``p_value`` / ``statistical_test`` /
``significance`` rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values``
carries::

    {"test": "t"|"F"|"r"|"chi2"|"z", "statistic": <number>, "df": <number>,
     "reported_p": <number>, "decimals"?: <int, inferred from reported_p else 2>,
     "tail"?: "two"}
    # F needs df1 AND df2 instead of df:
    {"test": "F", "statistic": <number>, "df1": <number>, "df2": <number>, "reported_p": ...}

``judge`` recomputes the two-tailed p and compares to ``reported_p``:

  * required fields missing, or an unsupported ``test`` / non-"two" ``tail`` / out-of-domain
    statistic (e.g. ``|r| >= 1``, ``df <= 0``) -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``reported_p`` is a correct rounding of the recomputed p at ``decimals``  -> PASS.
  * ``reported_p`` is NOT a correct rounding                                   -> FAIL (severity
        B) shipping an EvidencePacket whose stdlib-only ``recompute_script`` embeds the SAME CDF
        code and reprints the discrepancy line, so a skeptical reader reruns it (DESIGN §3.2:
        no script, no flag). The message is sharper when the error flips the 0.05 decision.

Flag rule (DESIGN §6.3): FAIL iff ``|reported_p - recomputed_p| > 0.5*10^(-decimals) + 1e-9`` —
i.e. the reported p does not round to the recomputed truth at the precision it was printed.

Everything here is deterministic: no RNG, clock, or network in ``judge`` or in the emitted
script (DESIGN §7 G4). The calibration kernel verifies that empirically.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from litmus.core.claim import (
    Claim,
    Evidence,
    EvidenceKind,
    EpistemicTier,
    Location,
)
from litmus.core.finding import (
    EvidencePacket,
    Finding,
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

# Margin on the comparison so a recomputed p sitting exactly on a rounding boundary is not
# flagged by floating-point noise (DESIGN §6.3).
EPS = 1e-9

SIGNIFICANCE = 0.05  # the canonical decision threshold a mis-reported p can flip.

SUPPORTED_TESTS = ("t", "F", "r", "chi2", "z")

TEST_KEY = "test"
STAT_KEY = "statistic"
DF_KEY = "df"
DF1_KEY = "df1"
DF2_KEY = "df2"
REPORTED_P_KEY = "reported_p"
DECIMALS_KEY = "decimals"
TAIL_KEY = "tail"


# ---------------------------------------------------------------------------
# The CDF code. This SAME source string is exec'd into this module (so the live
# verdict uses it) AND embedded verbatim into every recompute_script (so the script's
# stdout is byte-identical to judge's expected_output, DESIGN §7 G3). Pure stdlib ``math``
# only — no scipy/numpy (DESIGN §3.7: reimplement, don't wrap-and-claim).
# ---------------------------------------------------------------------------
_CDF_SOURCE = r'''
import math


def _betacf(a, b, x):
    """Numerical-Recipes continued fraction for the incomplete beta (Lentz's method)."""
    MAXIT = 400
    FPMIN = 1.0e-300
    CF_EPS = 3.0e-14
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < CF_EPS:
            break
    return h


def _betai(a, b, x):
    """Regularized incomplete beta I_x(a, b) via lgamma + the betacf continued fraction."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _gammq(a, x):
    """Regularized upper incomplete gamma Q(a, x): series for P then Q=1-P below x<a+1,
    continued fraction for Q above. (Numerical Recipes gammp/gammq.)"""
    if x < 0.0 or a <= 0.0:
        raise ValueError("bad args to _gammq")
    if x == 0.0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        ap = a
        s = 1.0 / a
        delta = s
        for _ in range(1000):
            ap += 1.0
            delta *= x / ap
            s += delta
            if abs(delta) < abs(s) * 3.0e-15:
                break
        p = s * math.exp(-x + a * math.log(x) - gln)
        return 1.0 - p
    FPMIN = 1.0e-300
    b = x + 1.0 - a
    c = 1.0 / FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3.0e-15:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def _p_two_tailed(test, statistic, df, df1, df2):
    """Two-tailed p-value for a reported test statistic. Pure stdlib math.

    Returns None when the inputs are out of the distribution's domain (so the caller
    abstains rather than inventing a verdict, DESIGN §3.4).
    """
    if test == "z":
        return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(statistic) / math.sqrt(2.0))))
    if test == "t":
        if df is None or df <= 0:
            return None
        x = df / (df + statistic * statistic)
        return _betai(df / 2.0, 0.5, x)
    if test == "r":
        if df is None or df <= 0:
            return None
        if abs(statistic) >= 1.0:
            return None  # r out of (-1, 1): t-transform is undefined
        t = statistic * math.sqrt(df / (1.0 - statistic * statistic))
        x = df / (df + t * t)
        return _betai(df / 2.0, 0.5, x)
    if test == "chi2":
        if df is None or df <= 0 or statistic < 0:
            return None
        return _gammq(df / 2.0, statistic / 2.0)
    if test == "F":
        if df1 is None or df2 is None or df1 <= 0 or df2 <= 0 or statistic < 0:
            return None
        x = df2 / (df2 + df1 * statistic)
        return _betai(df2 / 2.0, df1 / 2.0, x)
    return None
'''

# Exec the CDF source into this module's globals so ``judge`` calls the IDENTICAL code that the
# recompute script embeds. (Mirrors how sum_check inlines _fmt verbatim, but the body is large.)
_cdf_ns: dict[str, Any] = {}
exec(_CDF_SOURCE, _cdf_ns)  # noqa: S102 — first-party constant source, not user input
_p_two_tailed = _cdf_ns["_p_two_tailed"]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _infer_decimals(reported_p: float | int, override: Any) -> int:
    """How many decimal places the reported p was printed to.

    If the extractor passes ``decimals`` explicitly we trust it; otherwise we infer from the
    textual form of ``reported_p`` (``0.04`` -> 2, ``0.001`` -> 3), defaulting to 2 (DESIGN §6.3).
    """
    if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
        return override
    if isinstance(reported_p, float):
        s = repr(reported_p)
        if "e" in s or "E" in s:  # scientific notation: count from the exponent
            try:
                mant, exp = s.lower().split("e")
                frac = len(mant.split(".")[1]) if "." in mant else 0
                return max(0, frac - int(exp))
            except Exception:
                return 2
        if "." in s:
            return len(s.split(".")[1])
    return 2


def _round_p(p: float) -> float:
    """Round a recomputed p to a fixed 6 places for display. The SAME rounding is applied on
    both sides (live + script) so ``expected_output`` and the script's stdout match byte-for-byte
    (DESIGN §7 G3). 6 places is finer than any plausible ``decimals`` so it never hides an error.
    """
    return round(p, 6)


def _fmt_num(value: float | int) -> str:
    """Render statistic / df / reported_p identically on both sides (integral -> no decimal)."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _df_field(test: str, df: Any, df1: Any, df2: Any) -> str:
    """The df portion of the verdict line: ``df=20`` for most, ``df1=1,df2=20`` for F."""
    if test == "F":
        return f"df1={_fmt_num(df1)},df2={_fmt_num(df2)}"
    return f"df={_fmt_num(df)}"


def _inconsistent_line(
    test: str, statistic: Any, df: Any, df1: Any, df2: Any, reported_p: Any, recomputed_p: float
) -> str:
    """The single canonical discrepancy line both ``judge`` and the script print."""
    return (
        f"INCONSISTENT test={test} stat={_fmt_num(statistic)} "
        f"{_df_field(test, df, df1, df2)} reported_p={_fmt_num(reported_p)} "
        f"recomputed_p={_fmt_num(_round_p(recomputed_p))}"
    )


def _find_bound(evidence: list[Evidence]) -> Optional[tuple[Evidence, dict]]:
    """First evidence carrying a supported ``test``, a numeric ``statistic`` and ``reported_p``,
    and the df fields that test needs. Returns ``(ev, normalized_fields)`` or None.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        test = vals.get(TEST_KEY)
        stat = vals.get(STAT_KEY)
        rep = vals.get(REPORTED_P_KEY)
        if not isinstance(test, str) or test not in SUPPORTED_TESTS:
            continue
        if not (_is_number(stat) and _is_number(rep)):
            continue
        tail = vals.get(TAIL_KEY, "two")
        if tail not in (None, "two"):
            continue  # only two-tailed is supported; abstain otherwise
        df = vals.get(DF_KEY)
        df1 = vals.get(DF1_KEY)
        df2 = vals.get(DF2_KEY)
        if test == "F":
            if not (_is_number(df1) and _is_number(df2)):
                continue
        elif test == "z":
            pass  # the standard normal has no degrees of freedom
        else:  # t, r, chi2 each need a single df
            if not _is_number(df):
                continue
        return ev, {
            "test": test,
            "statistic": stat,
            "df": df,
            "df1": df1,
            "df2": df2,
            "reported_p": rep,
            "decimals": _infer_decimals(rep, vals.get(DECIMALS_KEY)),
        }
    return None


def _build_recompute_script(f: dict, recomputed_p: float) -> str:
    """A self-contained, stdlib-only program that re-derives p from the statistic and prints the
    verdict. Embeds ``_CDF_SOURCE`` verbatim, hardcodes the inputs (no stdin/network/clock), and
    prints exactly one line: ``OK`` if the reported p is a correct rounding, else the
    ``INCONSISTENT ...`` line. ``_round_p`` / ``_fmt_num`` bodies are identical to the module's so
    stdout is byte-exact (DESIGN §7 G3).
    """
    return (
        "# LITMUS recompute script for statcheck.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only (math), network-less, deterministic.\n"
        + _CDF_SOURCE
        + "\n"
        f"TEST = {f['test']!r}\n"
        f"STATISTIC = {f['statistic']!r}\n"
        f"DF = {f['df']!r}\n"
        f"DF1 = {f['df1']!r}\n"
        f"DF2 = {f['df2']!r}\n"
        f"REPORTED_P = {f['reported_p']!r}\n"
        f"DECIMALS = {f['decimals']!r}\n"
        "EPS = 1e-9\n"
        "\n"
        "\n"
        "def _round_p(p):\n"
        "    return round(p, 6)\n"
        "\n"
        "\n"
        "def _fmt_num(value):\n"
        "    if isinstance(value, bool):\n"
        "        value = int(value)\n"
        "    if isinstance(value, int):\n"
        "        return str(value)\n"
        "    if isinstance(value, float) and value.is_integer():\n"
        "        return str(int(value))\n"
        "    return repr(value)\n"
        "\n"
        "\n"
        "def _df_field():\n"
        "    if TEST == 'F':\n"
        "        return 'df1=' + _fmt_num(DF1) + ',df2=' + _fmt_num(DF2)\n"
        "    return 'df=' + _fmt_num(DF)\n"
        "\n"
        "\n"
        "p = _p_two_tailed(TEST, STATISTIC, DF, DF1, DF2)\n"
        "threshold = 0.5 * 10 ** (-DECIMALS) + EPS\n"
        "if p is not None and abs(REPORTED_P - p) > threshold:\n"
        "    print(\n"
        "        'INCONSISTENT test=' + TEST + ' stat=' + _fmt_num(STATISTIC) + ' '\n"
        "        + _df_field() + ' reported_p=' + _fmt_num(REPORTED_P)\n"
        "        + ' recomputed_p=' + _fmt_num(_round_p(p))\n"
        "    )\n"
        "else:\n"
        "    print('OK')\n"
    )


class StatCheck(Verifier):
    """Reported two-tailed p matches the p recomputed from the test statistic (DESIGN §5 T0)."""

    manifest = VerifierManifest(
        id="statcheck.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["p_value", "statistical_test", "significance", "test_statistic"],
        capability_tags=["statistics", "p_value", "null_hypothesis_test"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={
            "ours": [
                "pure-stdlib normal/t/F/chi2 CDFs (erf, incomplete beta via betacf+lgamma, "
                "incomplete gamma)",
                "two-tailed p recompute + correct-rounding flag rule",
            ],
            "libs": [],  # explicitly NOT scipy/numpy (DESIGN §3.7)
        },
        dependencies=[],
        description=(
            "Recomputes a two-tailed p-value from a reported test statistic and df (t/F/r/chi2/z) "
            "using pure-stdlib CDFs and flags when the reported p is not a correct rounding of the "
            "truth (T0). Binds to evidence carrying extracted_values "
            "{'test','statistic','df'(or df1,df2 for F),'reported_p','decimals'?,'tail'?}."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries a supported two-tailed test with a numeric statistic, "
                "df, and reported_p; cannot recompute a p-value (DESIGN §3.4: abstain > guess)",
            )
        ev, f = bound
        recomputed_p = _p_two_tailed(f["test"], f["statistic"], f["df"], f["df1"], f["df2"])
        if recomputed_p is None:
            return self.abstain(
                claim,
                f"statistic out of domain for test={f['test']} "
                f"(e.g. |r|>=1, df<=0, or negative chi2/F); abstaining (DESIGN §3.4)",
            )

        threshold = 0.5 * 10 ** (-f["decimals"]) + EPS
        reported_p = f["reported_p"]

        if abs(reported_p - recomputed_p) <= threshold:
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="reported p-value is a correct rounding of the p recomputed from the statistic",
                reported=reported_p,
                computed=_round_p(recomputed_p),
                details={
                    "test": f["test"],
                    "statistic": f["statistic"],
                    "df": f["df"],
                    "df1": f["df1"],
                    "df2": f["df2"],
                    "decimals": f["decimals"],
                },
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        # A decision error (one side significant, the other not at 0.05) is the sharper finding.
        decision_flip = (reported_p < SIGNIFICANCE) != (recomputed_p < SIGNIFICANCE)
        if decision_flip:
            message = (
                "reported p-value disagrees with the statistic AND flips the .05 significance "
                "decision"
            )
            severity = Severity.B
        else:
            message = "reported p-value is not a correct rounding of the p recomputed from the statistic"
            severity = Severity.B

        expected = _inconsistent_line(
            f["test"], f["statistic"], f["df"], f["df1"], f["df2"], reported_p, recomputed_p
        )
        script = _build_recompute_script(f, recomputed_p)
        packet = EvidencePacket(
            quote=ev.location.quote,
            location=ev.location,
            recompute_script=script,
            expected_output=expected,
            script_dependencies=[],  # stdlib-only (DESIGN §3.8 P8)
        )
        return self.make_finding(
            claim=claim,
            status=Status.FAIL,
            severity=severity,
            message=message,
            discrepancy=(
                f"reported p={_fmt_num(reported_p)} but {f['test']} statistic "
                f"{_fmt_num(f['statistic'])} ({_df_field(f['test'], f['df'], f['df1'], f['df2'])}) "
                f"gives p={_fmt_num(_round_p(recomputed_p))}"
                + (" — crosses .05" if decision_flip else "")
            ),
            reported=reported_p,
            computed=_round_p(recomputed_p),
            evidence=packet,
            details={
                "test": f["test"],
                "statistic": f["statistic"],
                "df": f["df"],
                "df1": f["df1"],
                "df2": f["df2"],
                "decimals": f["decimals"],
                "decision_flip": decision_flip,
            },
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (correctly-rounded p triples) + planted (mis-rounded / decision-error) cases.

        Fixed, hand-written statistics — deterministic, no RNG (DESIGN §7 G4). For each clean case
        the *true* p is computed here (by the same CDFs) and rounded to the case's ``decimals``, so
        the reported p is correct by construction. Planted cases report a p that does not round to
        the truth (several flip the .05 decision). Spans t, F, chi2, z (and r) so per-claim-type
        FPR (G6) is exercised across distributions. >=6 clean and >=6 planted.
        """
        cases: list[SelfTestCase] = []

        # Clean: (claim_type, suffix, test, statistic, df, df1, df2, decimals)
        # reported_p is filled in as the correctly-rounded true p.
        clean_specs: list[tuple[str, str, str, float, Any, Any, Any, int]] = [
            ("p_value", "z_at_05", "z", 1.95996, None, None, None, 2),         # ~0.0500 -> 0.05
            ("p_value", "z_big", "z", 3.0, None, None, None, 3),               # ~0.0027
            ("p_value", "t_at_05", "t", 2.085963, 20, None, None, 2),         # ~0.0500 -> 0.05
            ("p_value", "t_ns", "t", 2.0, 20, None, None, 2),                  # ~0.0593 -> 0.06
            ("statistical_test", "t_sig", "t", 2.5, 18, None, None, 2),       # ~0.0223 -> 0.02
            ("statistical_test", "chi2_at_05", "chi2", 3.84146, 1, None, None, 2),  # ~0.0500
            ("statistical_test", "F_at_05", "F", 4.35125, None, 1, 20, 2),    # ~0.0500
            ("significance", "F_big", "F", 10.0, None, 2, 30, 3),             # small p
            ("significance", "r_mid", "r", 0.5, 18, None, None, 3),           # ~0.0248
        ]
        for ctype, suffix, test, stat, df, df1, df2, dec in clean_specs:
            true_p = _p_two_tailed(test, stat, df, df1, df2)
            assert true_p is not None, f"clean spec {suffix} is out of domain"
            reported = round(true_p, dec)
            # By construction this must satisfy the PASS rule; assert it (DESIGN §6.3).
            assert abs(reported - true_p) <= 0.5 * 10 ** (-dec) + EPS, (
                f"clean spec not actually clean: {suffix} (true={true_p}, reported={reported})"
            )
            cases.append(
                self._case(
                    f"clean_{suffix}", "clean", ctype, test, stat, df, df1, df2, reported, dec
                )
            )

        # Planted: report a p that is NOT a correct rounding of the truth.
        # (claim_type, suffix, test, statistic, df, df1, df2, reported_p, decimals)
        planted_specs: list[tuple[str, str, str, float, Any, Any, Any, float, int]] = [
            # The flagship: t=2.0, df=20 is really p≈0.0593 (ns), reported as significant .04.
            ("p_value", "t_flip_to_sig", "t", 2.0, 20, None, None, 0.04, 2),
            # t=2.5, df=18 is ≈0.0223; reported a (wrong) ns .20.
            ("statistical_test", "t_wildly_off", "t", 2.5, 18, None, None, 0.20, 2),
            # z=1.5 is ≈0.1336; reported significant .03 (decision flip).
            ("p_value", "z_flip_to_sig", "z", 1.5, None, None, None, 0.03, 2),
            # z=2.5 is ≈0.0124; reported .05 — mis-rounded by far more than half a unit.
            ("p_value", "z_misrounded", "z", 2.5, None, None, None, 0.05, 2),
            # chi2=3.0, df=1 is ≈0.0833 (ns); reported significant .04 (decision flip).
            ("statistical_test", "chi2_flip", "chi2", 3.0, 1, None, None, 0.04, 2),
            # F=2.0, df1=1, df2=20 is ≈0.1726; reported .03 (decision flip).
            ("significance", "F_flip", "F", 2.0, None, 1, 20, 0.03, 2),
            # r=0.3, df=18 is ≈0.2278; reported significant .04 (decision flip).
            ("significance", "r_flip", "r", 0.3, 18, None, None, 0.04, 2),
        ]
        for ctype, suffix, test, stat, df, df1, df2, reported, dec in planted_specs:
            true_p = _p_two_tailed(test, stat, df, df1, df2)
            assert true_p is not None, f"planted spec {suffix} is out of domain"
            assert abs(reported - true_p) > 0.5 * 10 ** (-dec) + EPS, (
                f"planted spec is actually clean: {suffix} (true={true_p}, reported={reported})"
            )
            cases.append(
                self._case(
                    f"planted_{suffix}", "planted", ctype, test, stat, df, df1, df2, reported, dec
                )
            )

        return cases

    @staticmethod
    def _case(
        name: str,
        kind: str,
        claim_type: str,
        test: str,
        statistic: float,
        df: Any,
        df1: Any,
        df2: Any,
        reported_p: float,
        decimals: int,
    ) -> SelfTestCase:
        vals: dict[str, Any] = {
            TEST_KEY: test,
            STAT_KEY: statistic,
            REPORTED_P_KEY: reported_p,
            DECIMALS_KEY: decimals,
        }
        if test == "F":
            vals[DF1_KEY] = df1
            vals[DF2_KEY] = df2
            df_desc = f"df1={df1}, df2={df2}"
        else:
            vals[DF_KEY] = df
            df_desc = f"df={df}"
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.STATISTIC,
            location=Location(
                section="self_test", quote=f"{test}={statistic}, {df_desc}, p={reported_p}"
            ),
            extracted_values=vals,
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"{test}({df_desc}) = {statistic}, p = {reported_p}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T0,
            predicate="reported_p is a correct rounding of p recomputed from the statistic",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [StatCheck()]
