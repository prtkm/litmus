"""``statcheck.v1`` — a real T0 verifier: does a reported p-value match its test statistic?

The flagship "p reported as 0.04 but t=2.0, df=18 gives p=0.058" archetype (DESIGN §5 T0,
§6.1 class A, §19 WS-D). This is the canonical statcheck move (DESIGN §1: "statcheck for every
field"), reimplemented from first principles, not wrapped (DESIGN §3.7): given a reported test
statistic, its degrees of freedom, and a reported two-tailed p, we *recompute* p from the
statistic with our own pure-Python CDFs and flag when the reported p is genuinely inconsistent.
Pure arithmetic on the paper's own transcribed numbers — no external knowledge, no model in the
loop (DESIGN §3.1). A close cousin of the T0 recompute core (``sum_check``, ``percent_change``).

**Statistic-rounding-aware (the real statcheck move).** A statistic is printed to finite
precision: ``F = 0.02`` means the true F is anywhere in ``[0.015, 0.025]``. Re-deriving p from a
single point would manufacture rounding-tail false positives — e.g. the author printing
``F(2,41)=0.02`` and ``p=0.974`` is *consistent*: the achievable p over ``[0.015, 0.025]`` covers
roughly ``[0.975, 0.985]`` and the printed ``0.974`` (itself rounded) is the same value to within
presentation noise. So we compute the p-value over the WHOLE interval the printed statistic admits
and only object when the reported p (at its own printed precision) cannot be produced by ANY
rounding of the statistic. This eliminates the rounding-tail nitpicks the owner flagged while
keeping the genuinely wrong ones (a t=3.6/df=41 reported as p=0.013 recomputes to ~0.0009 — a 15x
discrepancy no rounding can explain).

**Severity by impact (DESIGN §6.3).** A flag only fires when the reported p is outside the
achievable range *and* it matters:

  * the reported p and the recomputed p fall on OPPOSITE sides of 0.05 (a *decision* error: a
    result claimed significant that is not, or vice versa) -> **FAIL severity B** (material).
  * otherwise the significance decision is unchanged; emit only when the gap is genuinely large
    (relative error > 25%) -> **FAIL severity C**. A small over-the-line gap with the decision
    intact -> **PASS** (presentation noise, not an error worth a reader's time).

Net effect: far fewer, higher-value flags. Prefer PASS over a trivial flag (DESIGN §3.4).

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
carries (key names are matched *leniently* — see ``_find_bound`` — so extraction variance like
``p`` / ``p_value`` / ``pval`` or ``stat`` / ``test_statistic`` still binds)::

    {"test": "t"|"F"|"r"|"chi2"|"z", "statistic": <number>, "df": <number>,
     "reported_p": <number>, "stat_decimals"?: <int>, "decimals"?: <int>, "tail"?: "two"}
    # F needs df1 AND df2 instead of df:
    {"test": "F", "statistic": <number>, "df1": <number>, "df2": <number>, "reported_p": ...}

``judge`` recomputes the achievable two-tailed p-range from the statistic's printed precision and
compares to ``reported_p``:

  * required fields missing, or an unsupported ``test`` / non-"two" ``tail`` / out-of-domain
    statistic (e.g. ``|r| >= 1``, ``df <= 0``) -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``reported_p`` (at its printed precision) is reachable by SOME rounding of the statistic, OR
    the gap is small and the .05 decision is unchanged                       -> PASS.
  * ``reported_p`` is unreachable AND it flips the .05 decision                -> FAIL severity B.
  * ``reported_p`` is unreachable, decision unchanged, but relative error >25% -> FAIL severity C.

Each FAIL ships an EvidencePacket whose stdlib-only ``recompute_script`` embeds the SAME CDF code
and the SAME rounding-band logic, and reprints the discrepancy line, so a skeptical reader reruns
it (DESIGN §3.2: no script, no flag).

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

# Margin so a recomputed p sitting exactly on a rounding boundary is not flagged by floating-point
# noise (DESIGN §6.3).
EPS = 1e-9

SIGNIFICANCE = 0.05  # the canonical decision threshold a mis-reported p can flip.

# Minimum relative error for a NON-decision-flipping discrepancy to be worth a (severity-C) flag.
# Below this, an out-of-rounding-range gap is presentation noise, not an error (DESIGN §3.4, §6.3).
MIN_REL_ERR_FOR_C = 0.25

SUPPORTED_TESTS = ("t", "F", "r", "chi2", "z")

# How many points to sample across the statistic's rounding interval when bracketing the
# achievable p-range. p is monotone in |statistic| for every distribution here, so the extremes
# land at the interval endpoints; we sample a handful of interior points too as cheap insurance
# against float drift / a sign-straddling interval. The SAME count is used in the script.
N_SAMPLES = 9

# --- lenient key matching (robustness; fix in the verifier, not the prompt, per repo guidance) ---
# Extraction varies: a p-value might arrive as p / p_value / pval / reported_p; a statistic as
# stat / statistic / test_statistic / the bare distribution letter. Bind despite that variance
# (DESIGN §11 key-alignment) rather than abstaining on a cosmetic key mismatch.
TEST_KEYS = ("test", "test_type", "statistic_test", "stat_test")
REPORTED_P_KEYS = ("reported_p", "p", "p_value", "pval", "p_val", "reported_p_value", "p_reported")
STAT_KEYS = ("statistic", "stat", "statistic_value", "test_statistic", "value")
DF_KEYS = ("df", "dof", "df_error", "df_resid", "df_denominator")
DF1_KEYS = ("df1", "df_num", "df_numerator", "df_between")
DF2_KEYS = ("df2", "df_den", "df_denom", "df_denominator", "df_within", "df_error")
TAIL_KEYS = ("tail", "tails", "sided")
# Decimals override for the STATISTIC's printed precision, and (legacy) for the reported p.
STAT_DECIMALS_KEYS = ("stat_decimals", "statistic_decimals")
DECIMALS_KEYS = ("decimals", "p_decimals")
# Bare distribution-letter keys: if no explicit ``test`` is given but the statistic arrives under
# one of these, infer the test from the key (e.g. ``{"F": 0.86, "df1":1, "df2":41, ...}``).
LETTER_TEST_KEYS = {"t": "t", "f": "F", "F": "F", "z": "z", "chi2": "chi2", "chisq": "chi2", "r": "r"}


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


def _p_range(test, statistic, df, df1, df2, half, n_samples):
    """The achievable two-tailed p-range over the statistic's rounding interval.

    The printed statistic stands for any true value in ``[statistic-half, statistic+half]`` (half
    = 0.5*10^-stat_decimals). We sample that interval (endpoints + interior), clamp a negative
    sample up to 0 for the one-sided F/chi2 supports, skip out-of-domain samples, and return
    ``(p_lo, p_hi, p_point)`` — the min/max achievable p and the p at the printed value itself.
    Returns ``(None, None, None)`` if the printed value itself is out of domain.
    """
    p_point = _p_two_tailed(test, statistic, df, df1, df2)
    if p_point is None:
        return None, None, None
    lo = statistic - half
    hi = statistic + half
    ps = []
    n = n_samples if n_samples > 1 else 2
    for i in range(n):
        s = lo + (hi - lo) * i / (n - 1)
        if test in ("F", "chi2") and s < 0.0:
            s = 0.0
        p = _p_two_tailed(test, s, df, df1, df2)
        if p is not None:
            ps.append(p)
    if not ps:
        ps = [p_point]
    return min(ps), max(ps), p_point
'''

# Exec the CDF source into this module's globals so ``judge`` calls the IDENTICAL code that the
# recompute script embeds. (Mirrors how sum_check inlines _fmt verbatim, but the body is large.)
_cdf_ns: dict[str, Any] = {}
exec(_CDF_SOURCE, _cdf_ns)  # noqa: S102 — first-party constant source, not user input
_p_two_tailed = _cdf_ns["_p_two_tailed"]
_p_range = _cdf_ns["_p_range"]


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _decimals_of(value: float | int) -> int:
    """How many decimal places a number was printed to (``0.02`` -> 2, ``3.6`` -> 1), default 2."""
    if isinstance(value, float):
        s = repr(value)
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


def _infer_decimals(value: float | int, override: Any) -> int:
    """Decimals an explicit override declares, else inferred from the printed form (default 2)."""
    if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
        return override
    return _decimals_of(value)


def _round_p(p: float) -> float:
    """Round a recomputed p to a fixed 6 places for display. The SAME rounding is applied on
    both sides (live + script) so ``expected_output`` and the script's stdout match byte-for-byte
    (DESIGN §7 G3). 6 places is finer than any plausible reported precision so it never hides an
    error.
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
    test: str, statistic: Any, df: Any, df1: Any, df2: Any, reported_p: Any,
    p_lo: float, p_hi: float,
) -> str:
    """The single canonical discrepancy line both ``judge`` and the script print.

    Reports the achievable p *range* (so the reader sees the statistic-rounding band the reported
    p fell outside of), not a single point.
    """
    return (
        f"INCONSISTENT test={test} stat={_fmt_num(statistic)} "
        f"{_df_field(test, df, df1, df2)} reported_p={_fmt_num(reported_p)} "
        f"achievable_p=[{_fmt_num(_round_p(p_lo))},{_fmt_num(_round_p(p_hi))}]"
    )


def _first_present(vals: dict, keys: tuple[str, ...]) -> Any:
    """The value under the first of ``keys`` present in ``vals`` (None if none present)."""
    for k in keys:
        if k in vals:
            return vals[k]
    return None


def _resolve_test_and_stat(vals: dict) -> tuple[Optional[str], Any, Optional[str]]:
    """Pull the test name and statistic out of an evidence's values, leniently.

    First honours an explicit ``test``/``test_type`` (matched case-insensitively against the
    supported set, with ``chisq``/``chi-square`` folded to ``chi2``); the statistic then comes from
    any of the canonical stat keys, or — if absent — the bare distribution-letter key the test
    names. If there is no explicit test, infer it from a bare distribution-letter key that carries
    a number (``{"F": 0.86, ...}`` -> test "F"). Returns ``(test|None, statistic, stat_key|None)``;
    ``stat_key`` is the key the statistic came from (used to infer its printed precision).
    """
    raw_test = _first_present(vals, TEST_KEYS)
    test: Optional[str] = None
    if isinstance(raw_test, str):
        t = raw_test.strip().lower()
        if t in ("chisq", "chi-square", "chi^2", "x2", "χ2"):
            t = "chi2"
        if t == "f":
            t = "F"
        if t in [s.lower() for s in SUPPORTED_TESTS] or t == "F":
            test = "F" if t == "f" or t == "F" else t

    if test is not None:
        stat = _first_present(vals, STAT_KEYS)
        stat_key = next((k for k in STAT_KEYS if k in vals), None)
        if not _is_number(stat):
            # statistic may be under the bare distribution letter (e.g. {"test":"F","F":0.86})
            for k, mapped in LETTER_TEST_KEYS.items():
                if mapped == test and k in vals and _is_number(vals[k]):
                    return test, vals[k], k
        return test, stat, stat_key

    # No explicit test: infer from a bare distribution-letter key carrying a number.
    for k, mapped in LETTER_TEST_KEYS.items():
        if k in vals and _is_number(vals[k]):
            return mapped, vals[k], k
    # Last resort: a generic stat key with no test is unbindable (we can't pick a distribution).
    return None, _first_present(vals, STAT_KEYS), next((k for k in STAT_KEYS if k in vals), None)


def _find_bound(evidence: list[Evidence]) -> Optional[tuple[Evidence, dict]]:
    """First evidence carrying a supported ``test``, a numeric ``statistic`` and ``reported_p``,
    and the df fields that test needs. Returns ``(ev, normalized_fields)`` or None.

    Key matching is lenient (DESIGN §11): the test / statistic / p / df may each arrive under any
    of several spellings (see the ``*_KEYS`` tuples), so ordinary extraction variance still binds
    instead of forcing an abstain.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        test, stat, stat_key = _resolve_test_and_stat(vals)
        rep = _first_present(vals, REPORTED_P_KEYS)
        if test is None or test not in SUPPORTED_TESTS:
            continue
        if not (_is_number(stat) and _is_number(rep)):
            continue
        tail = _first_present(vals, TAIL_KEYS)
        if tail not in (None, "two", "two-tailed", "2", 2, "both"):
            continue  # only two-tailed is supported; abstain otherwise
        df = _first_present(vals, DF_KEYS)
        df1 = _first_present(vals, DF1_KEYS)
        df2 = _first_present(vals, DF2_KEYS)
        if test == "F":
            if not (_is_number(df1) and _is_number(df2)):
                continue
        elif test == "z":
            pass  # the standard normal has no degrees of freedom
        else:  # t, r, chi2 each need a single df
            if not _is_number(df):
                continue
        # The statistic's printed precision drives the rounding interval. Prefer an explicit
        # override; else infer from the transcribed statistic value's own decimals.
        stat_decimals = _infer_decimals(stat, _first_present(vals, STAT_DECIMALS_KEYS))
        # The reported p's printed precision drives the tolerance band it is allowed to round into.
        rep_decimals = _infer_decimals(rep, _first_present(vals, DECIMALS_KEYS))
        return ev, {
            "test": test,
            "statistic": stat,
            "df": df,
            "df1": df1,
            "df2": df2,
            "reported_p": rep,
            "stat_decimals": stat_decimals,
            "rep_decimals": rep_decimals,
        }
    return None


def _classify(f: dict) -> dict:
    """Decide PASS / FAIL(B) / FAIL(C) for a bound stat triple, the rounding-aware way.

    Returns a dict with: ``p_lo``, ``p_hi``, ``p_point`` (the achievable range + the point p),
    ``in_range`` (reported p reachable by some rounding of the statistic), ``decision_flip``,
    ``rel_err``, and ``verdict`` in {"pass", "flag_B", "flag_C"}.
    """
    stat_half = 0.5 * 10 ** (-f["stat_decimals"])
    rep_half = 0.5 * 10 ** (-f["rep_decimals"])
    p_lo, p_hi, p_point = _p_range(
        f["test"], f["statistic"], f["df"], f["df1"], f["df2"], stat_half, N_SAMPLES
    )
    if p_point is None:
        # Out of the distribution's domain (e.g. |r|>=1, df<=0, negative chi2/F). The caller
        # (judge / self_test) turns this into an ABSTAIN; don't manufacture a comparison.
        return {
            "p_lo": None,
            "p_hi": None,
            "p_point": None,
            "in_range": False,
            "decision_flip": False,
            "rel_err": 0.0,
            "verdict": "pass",
        }
    reported_p = f["reported_p"]
    # Reachable if the reported p's own rounding band overlaps the achievable p band: some true
    # statistic in the rounding interval produces a p that rounds to the reported value.
    in_range = (reported_p + rep_half >= p_lo - EPS) and (reported_p - rep_half <= p_hi + EPS)
    decision_flip = (reported_p < SIGNIFICANCE) != (p_point < SIGNIFICANCE)
    rel_err = abs(reported_p - p_point) / max(abs(p_point), 1e-12)

    if in_range:
        verdict = "pass"
    elif decision_flip:
        verdict = "flag_B"  # a significance-decision error: always material
    elif rel_err > MIN_REL_ERR_FOR_C:
        verdict = "flag_C"  # decision intact but a large, unexplainable gap
    else:
        verdict = "pass"  # outside the band but tiny + decision intact: presentation noise

    return {
        "p_lo": p_lo,
        "p_hi": p_hi,
        "p_point": p_point,
        "in_range": in_range,
        "decision_flip": decision_flip,
        "rel_err": rel_err,
        "verdict": verdict,
    }


def _build_recompute_script(f: dict, verdict: str) -> str:
    """A self-contained, stdlib-only program that re-derives the achievable p-range from the
    statistic's printed precision and prints the verdict. Embeds ``_CDF_SOURCE`` verbatim (including
    ``_p_range``), hardcodes the inputs (no stdin/network/clock), and prints exactly one line:
    ``OK`` if the reported p is consistent (reachable by some rounding, or a small decision-intact
    gap), else the ``INCONSISTENT ...`` line. The classification logic mirrors ``_classify`` so
    stdout is byte-exact (DESIGN §7 G3).
    """
    return (
        "# LITMUS recompute script for statcheck.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only (math), network-less, deterministic.\n"
        "# Statistic-rounding-aware: re-derives p over the interval the printed statistic admits.\n"
        + _CDF_SOURCE
        + "\n"
        f"TEST = {f['test']!r}\n"
        f"STATISTIC = {f['statistic']!r}\n"
        f"DF = {f['df']!r}\n"
        f"DF1 = {f['df1']!r}\n"
        f"DF2 = {f['df2']!r}\n"
        f"REPORTED_P = {f['reported_p']!r}\n"
        f"STAT_DECIMALS = {f['stat_decimals']!r}\n"
        f"REP_DECIMALS = {f['rep_decimals']!r}\n"
        f"N_SAMPLES = {N_SAMPLES!r}\n"
        f"SIGNIFICANCE = {SIGNIFICANCE!r}\n"
        f"MIN_REL_ERR_FOR_C = {MIN_REL_ERR_FOR_C!r}\n"
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
        "stat_half = 0.5 * 10 ** (-STAT_DECIMALS)\n"
        "rep_half = 0.5 * 10 ** (-REP_DECIMALS)\n"
        "p_lo, p_hi, p_point = _p_range(TEST, STATISTIC, DF, DF1, DF2, stat_half, N_SAMPLES)\n"
        "if p_point is None:\n"
        "    print('OK')\n"
        "else:\n"
        "    in_range = (REPORTED_P + rep_half >= p_lo - EPS) and (REPORTED_P - rep_half <= p_hi + EPS)\n"
        "    decision_flip = (REPORTED_P < SIGNIFICANCE) != (p_point < SIGNIFICANCE)\n"
        "    rel_err = abs(REPORTED_P - p_point) / max(abs(p_point), 1e-12)\n"
        "    flag = (not in_range) and (decision_flip or rel_err > MIN_REL_ERR_FOR_C)\n"
        "    if flag:\n"
        "        print(\n"
        "            'INCONSISTENT test=' + TEST + ' stat=' + _fmt_num(STATISTIC) + ' '\n"
        "            + _df_field() + ' reported_p=' + _fmt_num(REPORTED_P)\n"
        "            + ' achievable_p=[' + _fmt_num(_round_p(p_lo)) + ',' + _fmt_num(_round_p(p_hi)) + ']'\n"
        "        )\n"
        "    else:\n"
        "        print('OK')\n"
    )


class StatCheck(Verifier):
    """Reported two-tailed p is consistent with the p recomputed from the test statistic, allowing
    for the statistic's printed precision (DESIGN §5 T0)."""

    manifest = VerifierManifest(
        id="statcheck.v1",
        version="1.1",
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
                "statistic-rounding-aware achievable-p-range + impact-graded flag rule",
            ],
            "libs": [],  # explicitly NOT scipy/numpy (DESIGN §3.7)
        },
        dependencies=[],
        description=(
            "Recomputes the achievable two-tailed p-range from a reported test statistic and df "
            "(t/F/r/chi2/z) using pure-stdlib CDFs, accounting for the statistic's printed "
            "precision, and flags when the reported p cannot be produced by any rounding of the "
            "statistic AND it matters (severity B if it flips the .05 decision; severity C only if "
            "the relative gap > 25%; otherwise PASS). Binds leniently to evidence carrying "
            "extracted_values {test, statistic, df(or df1,df2 for F), reported_p, stat_decimals?}."
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
        c = _classify(f)
        if c["p_point"] is None:
            return self.abstain(
                claim,
                f"statistic out of domain for test={f['test']} "
                f"(e.g. |r|>=1, df<=0, or negative chi2/F); abstaining (DESIGN §3.4)",
            )

        reported_p = f["reported_p"]

        if c["verdict"] == "pass":
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message=(
                    "reported p-value is consistent with the achievable p-range from the statistic "
                    "(allowing for the statistic's printed precision)"
                ),
                reported=reported_p,
                computed=_round_p(c["p_point"]),
                details={
                    "test": f["test"],
                    "statistic": f["statistic"],
                    "df": f["df"],
                    "df1": f["df1"],
                    "df2": f["df2"],
                    "stat_decimals": f["stat_decimals"],
                    "rep_decimals": f["rep_decimals"],
                    "achievable_p_range": [_round_p(c["p_lo"]), _round_p(c["p_hi"])],
                    "in_range": c["in_range"],
                    "rel_err": c["rel_err"],
                },
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        decision_flip = c["decision_flip"]
        if c["verdict"] == "flag_B":
            severity = Severity.B
            message = (
                "reported p-value disagrees with the statistic AND flips the .05 significance "
                "decision"
            )
        else:  # flag_C
            severity = Severity.C
            message = (
                "reported p-value is not achievable from the statistic at any rounding "
                "(decision unchanged, but the relative gap is large)"
            )

        expected = _inconsistent_line(
            f["test"], f["statistic"], f["df"], f["df1"], f["df2"], reported_p,
            c["p_lo"], c["p_hi"],
        )
        script = _build_recompute_script(f, c["verdict"])
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
                f"admits p in [{_fmt_num(_round_p(c['p_lo']))},{_fmt_num(_round_p(c['p_hi']))}]"
                + (" — crosses .05" if decision_flip else "")
            ),
            reported=reported_p,
            computed=_round_p(c["p_point"]),
            evidence=packet,
            details={
                "test": f["test"],
                "statistic": f["statistic"],
                "df": f["df"],
                "df1": f["df1"],
                "df2": f["df2"],
                "stat_decimals": f["stat_decimals"],
                "rep_decimals": f["rep_decimals"],
                "achievable_p_range": [_round_p(c["p_lo"]), _round_p(c["p_hi"])],
                "decision_flip": decision_flip,
                "rel_err": c["rel_err"],
            },
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (p consistent with the statistic's rounding band) + planted (materially wrong).

        Fixed, hand-written statistics — deterministic, no RNG (DESIGN §7 G4). Clean cases include
        REALISTIC statistic-rounding tails: a p that disagrees with the *point* recompute but is
        reachable once you account for the 2-dp statistic (e.g. F(2,41)=0.02 reported p=0.974 — the
        owner's example), so they must PASS. Planted cases are MATERIALLY wrong — every one either
        flips the .05 decision or is grossly off (>>25% relative error) — so they must FAIL. Spans
        t, F, chi2, z (and r) so per-claim-type FPR (G6) is exercised across distributions.
        >=6 clean and >=6 planted.
        """
        cases: list[SelfTestCase] = []

        # Clean: (claim_type, suffix, test, statistic, df, df1, df2, reported_p)
        # reported_p is what a paper would print — correct to within statistic+p rounding.
        clean_specs: list[tuple[str, str, str, float, Any, Any, Any, float]] = [
            # Exact / near-exact correct roundings.
            ("p_value", "z_at_05", "z", 1.96, None, None, None, 0.05),          # ~0.0500
            ("p_value", "z_big", "z", 3.0, None, None, None, 0.003),            # ~0.0027
            ("statistical_test", "t_sig", "t", 2.5, 18, None, None, 0.02),      # ~0.0223
            ("statistical_test", "chi2_at_05", "chi2", 3.84, 1, None, None, 0.05),  # ~0.0500
            # Rounding-tail cases the OLD point-recompute wrongly flagged (the owner's complaint):
            # F(2,41)=0.02 admits p∈~[0.975,0.985]; printed p=0.974 is consistent -> must PASS.
            ("p_value", "F_round_974", "F", 0.02, None, 2, 41, 0.974),
            # F(2,41)=0.09 admits p∈~[0.910,0.919]; printed 0.912 consistent -> PASS.
            ("p_value", "F_round_912", "F", 0.09, None, 2, 41, 0.912),
            # F(1,41)=0.86 -> point p≈0.359 but printed 0.375 differs by only ~4.4% (no flip) -> PASS.
            ("statistical_test", "F_small_gap", "F", 0.86, None, 1, 41, 0.375),
            # r(18)=0.5 -> p≈0.0248, printed 0.025 -> PASS.
            ("significance", "r_mid", "r", 0.5, 18, None, None, 0.025),
            # t(41)=2.56 -> p≈0.0143, printed 0.014 -> PASS.
            ("statistical_test", "t_rt", "t", 2.56, 41, None, None, 0.014),
        ]
        for ctype, suffix, test, stat, df, df1, df2, reported in clean_specs:
            f = self._fields(test, stat, df, df1, df2, reported)
            c = _classify(f)
            assert c["p_point"] is not None, f"clean spec {suffix} is out of domain"
            assert c["verdict"] == "pass", (
                f"clean spec not actually clean: {suffix} "
                f"(verdict={c['verdict']}, range=[{c['p_lo']:.4f},{c['p_hi']:.4f}], "
                f"point={c['p_point']:.4f}, rel_err={c['rel_err']:.2%})"
            )
            cases.append(
                self._case(f"clean_{suffix}", "clean", ctype, test, stat, df, df1, df2, reported)
            )

        # Planted: report a p that NO rounding of the statistic can produce AND that matters.
        # (claim_type, suffix, test, statistic, df, df1, df2, reported_p)
        planted_specs: list[tuple[str, str, str, float, Any, Any, Any, float]] = [
            # The flagship: t=2.0, df=20 is really p≈0.0593 (ns), reported significant .04 -> flip.
            ("p_value", "t_flip_to_sig", "t", 2.0, 20, None, None, 0.04),
            # t=2.5, df=18 is ≈0.0223; reported a (wrong) ns .20 — ~9x off, decision flip.
            ("statistical_test", "t_wildly_off", "t", 2.5, 18, None, None, 0.20),
            # z=1.5 is ≈0.1336; reported significant .03 (decision flip).
            ("p_value", "z_flip_to_sig", "z", 1.5, None, None, None, 0.03),
            # chi2=3.0, df=1 is ≈0.0833 (ns); reported significant .04 (decision flip).
            ("statistical_test", "chi2_flip", "chi2", 3.0, 1, None, None, 0.04),
            # F=2.0, df1=1, df2=20 is ≈0.1726; reported .03 (decision flip).
            ("significance", "F_flip", "F", 2.0, None, 1, 20, 0.03),
            # r=0.3, df=18 is ≈0.2278; reported significant .04 (decision flip).
            ("significance", "r_flip", "r", 0.3, 18, None, None, 0.04),
            # The real-paper material one: t(41)=3.6 is p≈0.00085; reported .013 — ~15x off (no flip,
            # both significant) but a huge relative gap -> severity C flag must fire.
            ("statistical_test", "t_15x_off", "t", 3.6, 41, None, None, 0.013),
        ]
        for ctype, suffix, test, stat, df, df1, df2, reported in planted_specs:
            f = self._fields(test, stat, df, df1, df2, reported)
            c = _classify(f)
            assert c["p_point"] is not None, f"planted spec {suffix} is out of domain"
            assert c["verdict"] in ("flag_B", "flag_C"), (
                f"planted spec is not materially wrong: {suffix} "
                f"(verdict={c['verdict']}, range=[{c['p_lo']:.4f},{c['p_hi']:.4f}], "
                f"point={c['p_point']:.4f}, rel_err={c['rel_err']:.2%})"
            )
            cases.append(
                self._case(f"planted_{suffix}", "planted", ctype, test, stat, df, df1, df2, reported)
            )

        return cases

    @staticmethod
    def _fields(
        test: str, stat: float, df: Any, df1: Any, df2: Any, reported: float
    ) -> dict:
        """Build the normalized field dict ``_classify`` consumes (precision inferred from the
        printed forms, exactly as ``_find_bound`` would)."""
        return {
            "test": test,
            "statistic": stat,
            "df": df,
            "df1": df1,
            "df2": df2,
            "reported_p": reported,
            "stat_decimals": _decimals_of(stat),
            "rep_decimals": _decimals_of(reported),
        }

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
    ) -> SelfTestCase:
        vals: dict[str, Any] = {
            "test": test,
            "statistic": statistic,
            "reported_p": reported_p,
        }
        if test == "F":
            vals["df1"] = df1
            vals["df2"] = df2
            df_desc = f"df1={df1}, df2={df2}"
        else:
            vals["df"] = df
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
            predicate="reported_p is achievable from the statistic at its printed precision",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [StatCheck()]
