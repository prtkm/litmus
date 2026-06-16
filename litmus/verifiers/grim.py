"""``grim.v1`` — a real T0 verifier: is a reported mean of integer responses even achievable?

The GRIM test (Granularity-Related Inconsistency of Means; Brown & Heathers 2017), reimplemented
from first principles (DESIGN §5 T0, §6.1 class A, §19 WS-D; the design lists "GRIM/GRIMMER" in the
T0 row). When ``N`` participants each give an integer-scored response (a Likert item, a count),
the sum of those responses is an integer, so the mean can only land on a multiple of
``1/(N*items)``. A reported mean that no integer total can produce — at the precision it was
printed — is arithmetically impossible. Pure arithmetic on the paper's own transcribed numbers,
no external knowledge, no model in the loop (DESIGN §3.1). A close cousin of the T0 recompute core.

Contract with the extractor (DESIGN §11): a claim of type ``mean`` / ``descriptive_mean`` /
``grim`` rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values`` carries::

    {"reported_mean": <number>, "n": <int>, "n_items"?: <int, default 1>,
     "decimals"?: <int, inferred from reported_mean else 2>}

``granularity = n * n_items`` is how many integer "steps" the total spans. ``judge`` asks: does
ANY non-negative integer total reproduce ``reported_mean`` when rounded to ``decimals``?

  * ``reported_mean`` / ``n`` missing or non-numeric, ``n <= 0``  -> ABSTAIN (DESIGN §3.4).
  * ``granularity > 2*10^decimals`` -> ABSTAIN: the grid is finer than the reported precision,
        so GRIM has no power (every printed value is reachable) — abstaining beats a vacuous PASS.
  * some integer total reproduces the mean                       -> PASS.
  * no integer total reproduces it                                -> FAIL (severity B) shipping an
        EvidencePacket whose stdlib-only ``recompute_script`` reconstructs the nearest achievable
        mean and reprints the discrepancy line, so a skeptical reader reruns it (DESIGN §3.2:
        no script, no flag).

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

# Margin so an achievable mean sitting exactly on a rounding boundary is not flagged by
# floating-point noise (DESIGN §6.3).
EPS = 1e-9

MEAN_KEY = "reported_mean"
N_KEY = "n"
N_ITEMS_KEY = "n_items"
DECIMALS_KEY = "decimals"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _infer_decimals(reported_mean: float | int, override: Any) -> int:
    """How many decimal places the reported mean was printed to (``3.46`` -> 2), default 2."""
    if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
        return override
    if isinstance(reported_mean, float):
        s = repr(reported_mean)
        if "e" in s or "E" in s:
            try:
                mant, exp = s.lower().split("e")
                frac = len(mant.split(".")[1]) if "." in mant else 0
                return max(0, frac - int(exp))
            except Exception:
                return 2
        if "." in s:
            return len(s.split(".")[1])
    return 2


def _nearest_achievable(reported_mean: float, granularity: int, decimals: int) -> tuple[bool, int, float]:
    """Is ``reported_mean`` reproduced by some integer total k as ``k/granularity`` at ``decimals``?

    Returns ``(consistent, k_nearest, mean_nearest)`` where ``k_nearest`` is the integer total
    whose mean ``k/granularity`` is closest to ``reported_mean``. On a consistent mean, that k is
    one that rounds to the reported value. The SAME logic is embedded in the recompute script so
    the verdict line is byte-identical (DESIGN §7 G3).
    """
    approx = reported_mean * granularity
    target_rounded = round(reported_mean, decimals)
    best_k = int(round(approx))
    best_dist = abs(best_k / granularity - reported_mean)
    consistent = False
    consistent_k = best_k
    consistent_mean = best_k / granularity
    # The achievable means bracketing ``reported_mean`` are at most one step apart; scan a small
    # symmetric window to be safe against float drift at the boundary.
    lo = max(0, int(math.floor(approx)) - 2)
    hi = int(math.ceil(approx)) + 2
    for k in range(lo, hi + 1):
        m = k / granularity
        dist = abs(m - reported_mean)
        if dist < best_dist - EPS or (k == best_k):
            if dist < best_dist:
                best_dist = dist
                best_k = k
        if round(m, decimals) == target_rounded:
            # Prefer the closest reproducing total as the witness.
            if not consistent or dist < abs(consistent_mean - reported_mean):
                consistent = True
                consistent_k = k
                consistent_mean = m
    # Recompute the true nearest over the window (robust).
    best_k = min(range(lo, hi + 1), key=lambda k: (abs(k / granularity - reported_mean), k))
    if consistent:
        return True, consistent_k, consistent_mean
    return False, best_k, best_k / granularity


def _reproduces_under_convention(
    reported_mean: float, granularity: int, decimals: int
) -> tuple[bool, Optional[str]]:
    """Would the reported mean arise from some integer total ``k/granularity`` under a COMMON rounding
    convention — round-half-even (Python's ``round``), round-half-up, or truncation? Such a mean is a
    rounding-convention artifact, not a genuine impossibility (e.g. just2014 6.62 = trunc(411/62)).
    Returns ``(reproducible, convention_name)``.

    Deliberately EXCLUDES ``ceil``: the critique verified ceil reproduces all of just2014 AND two
    Wansink means, so admitting it would excuse documented fraud. ``half_even`` can never fire inside
    the FAIL branch (it is the very rule GRIM already failed), but is kept for documentation and to
    stay correct if the base convention ever changes. Used to soften severity AND — with the grid
    distance — to decide whether a GRIM inconsistency is a relevant hard flag or a screening note."""
    target = round(reported_mean, decimals)
    scale = 10 ** decimals
    approx = reported_mean * granularity
    lo = max(0, int(math.floor(approx)) - 2)
    hi = int(math.ceil(approx)) + 2
    for k in range(lo, hi + 1):
        m = k / granularity
        x = m * scale
        for name, val in (
            ("half_even", round(m, decimals)),
            ("half_up", math.floor(x + 0.5) / scale),
            ("truncation", math.floor(x + EPS) / scale),
        ):
            if abs(val - target) < EPS:
                return True, name
    return False, None


def _fmt_mean(value: float | int, decimals: int) -> str:
    """Render a mean to its reported precision identically on both sides (live + script)."""
    return f"{value:.{decimals}f}"


def _inconsistent_line(
    reported_mean: float | int, n: int, n_items: int, granularity: int, decimals: int,
    nearest_k: int, nearest_mean: float,
) -> str:
    """The single canonical discrepancy line both ``judge`` and the script print.

    e.g. ``GRIM-INCONSISTENT mean=3.46 n=20 nearest=3.45(=69/20)`` (n_items omitted when 1).
    """
    n_part = f"n={n}" if n_items == 1 else f"n={n} items={n_items}"
    return (
        f"GRIM-INCONSISTENT mean={_fmt_mean(reported_mean, decimals)} {n_part} "
        f"nearest={_fmt_mean(nearest_mean, decimals)}(={nearest_k}/{granularity})"
    )


def _find_bound(evidence: list[Evidence]) -> Optional[tuple[Evidence, dict]]:
    """First evidence carrying a numeric ``reported_mean`` and a positive integer ``n``."""
    for ev in evidence:
        vals = ev.extracted_values or {}
        mean = vals.get(MEAN_KEY)
        n = vals.get(N_KEY)
        if not _is_number(mean):
            continue
        # n must be a positive integer count of participants.
        if not _is_number(n) or n <= 0 or float(n) != int(n):
            continue
        n_items = vals.get(N_ITEMS_KEY, 1)
        if not _is_number(n_items) or n_items <= 0 or float(n_items) != int(n_items):
            continue
        return ev, {
            "reported_mean": mean,
            "n": int(n),
            "n_items": int(n_items),
            "decimals": _infer_decimals(mean, vals.get(DECIMALS_KEY)),
        }
    return None


def _build_recompute_script(f: dict, granularity: int) -> str:
    """A self-contained, stdlib-only program that reconstructs the nearest achievable mean and
    prints the verdict. Hardcodes the inputs (no stdin/network/clock) and prints exactly one line:
    ``OK`` if some integer total reproduces the mean, else the ``GRIM-INCONSISTENT ...`` line.
    The reconstruction + formatting bodies match the module's so stdout is byte-exact (DESIGN §7 G3).
    """
    n_items = f["n_items"]
    n_part_expr = (
        "'n=' + str(N)" if n_items == 1 else "'n=' + str(N) + ' items=' + str(N_ITEMS)"
    )
    return (
        "# LITMUS recompute script for grim.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only (math), network-less, deterministic.\n"
        "import math\n"
        "\n"
        f"REPORTED_MEAN = {f['reported_mean']!r}\n"
        f"N = {f['n']!r}\n"
        f"N_ITEMS = {n_items!r}\n"
        f"DECIMALS = {f['decimals']!r}\n"
        f"GRANULARITY = {granularity!r}\n"
        "EPS = 1e-9\n"
        "\n"
        "\n"
        "def _fmt_mean(value):\n"
        "    return format(value, '.' + str(DECIMALS) + 'f')\n"
        "\n"
        "\n"
        "approx = REPORTED_MEAN * GRANULARITY\n"
        "target_rounded = round(REPORTED_MEAN, DECIMALS)\n"
        "lo = max(0, int(math.floor(approx)) - 2)\n"
        "hi = int(math.ceil(approx)) + 2\n"
        "consistent = False\n"
        "for k in range(lo, hi + 1):\n"
        "    if round(k / GRANULARITY, DECIMALS) == target_rounded:\n"
        "        consistent = True\n"
        "        break\n"
        "nearest_k = min(range(lo, hi + 1), key=lambda k: (abs(k / GRANULARITY - REPORTED_MEAN), k))\n"
        "nearest_mean = nearest_k / GRANULARITY\n"
        "if consistent:\n"
        "    print('OK')\n"
        "else:\n"
        "    print(\n"
        "        'GRIM-INCONSISTENT mean=' + _fmt_mean(REPORTED_MEAN) + ' '\n"
        f"        + ({n_part_expr}) + ' '\n"
        "        + 'nearest=' + _fmt_mean(nearest_mean) + '(=' + str(nearest_k) + '/' + str(GRANULARITY) + ')'\n"
        "    )\n"
    )


class Grim(Verifier):
    """A reported mean of N integer responses must equal integer_total/(N*items) (DESIGN §5 T0)."""

    manifest = VerifierManifest(
        id="grim.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["mean", "descriptive_mean", "grim", "reported_mean"],
        capability_tags=["statistics", "granularity", "descriptives"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["GRIM granularity check + nearest-achievable reconstruction"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a reported mean of integer-scored responses is achievable as "
            "integer_total/(n*n_items) at the reported precision (GRIM; T0). Binds to evidence "
            "carrying extracted_values {'reported_mean','n','n_items'?,'decimals'?}. Abstains when "
            "n*n_items is too large to have power (> 2*10^decimals)."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries a numeric 'reported_mean' with a positive integer 'n'; "
                "cannot run GRIM (DESIGN §3.4: abstain > guess)",
            )
        ev, f = bound
        granularity = f["n"] * f["n_items"]
        decimals = f["decimals"]

        # GRIM only has power when the achievable grid (1/granularity) is coarser than the
        # reported precision (10^-decimals). Past that, every printed value is reachable.
        if granularity > 2 * 10 ** decimals:
            return self.abstain(
                claim,
                f"granularity n*items={granularity} exceeds 2*10^{decimals}: the GRIM grid is "
                f"finer than the reported precision, so the test has no power; abstaining "
                f"(DESIGN §3.4)",
            )

        consistent, nearest_k, nearest_mean = _nearest_achievable(
            f["reported_mean"], granularity, decimals
        )

        if consistent:
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="reported mean is achievable as integer_total/(n*items) at its precision",
                reported=f["reported_mean"],
                computed=nearest_mean,
                details={
                    "n": f["n"],
                    "n_items": f["n_items"],
                    "granularity": granularity,
                    "decimals": decimals,
                    "witness_total": nearest_k,
                },
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        expected = _inconsistent_line(
            f["reported_mean"], f["n"], f["n_items"], granularity, decimals, nearest_k, nearest_mean
        )
        script = _build_recompute_script(f, granularity)
        packet = EvidencePacket(
            quote=ev.location.quote,
            location=ev.location,
            recompute_script=script,
            expected_output=expected,
            script_dependencies=[],  # stdlib-only (DESIGN §3.8 P8)
        )

        # Fragility probe (owner feedback, DESIGN §3.6). A SINGLE GRIM-impossible mean is sensitive
        # to the exact N: if dropping ONE response (n-1) makes the mean achievable, the flag is
        # "fragile" — plausibly explained by one missing/excluded item (e.g. 6.62 is impossible at
        # n=62 but 397/61... here 6.62 is achievable at n=61). The FAIL and its executable evidence
        # are unchanged (the arithmetic is true at the reported N, so calibration — which tests the
        # judge verdict, not the trust tier — and G3 reproducibility are untouched). Downstream, the
        # report assembler softens a LONE fragile flag to advisory/review; a PATTERN of GRIM hits or
        # any robust one still stands as deterministic (gate_fragile_grim). k=1 only: it cleanly
        # separates the borderline owner case from the gold-standard catches, none of which are
        # rescued by dropping a single response. Single-item scales only (n_items==1) — a multi-item
        # grid does not map to dropping one respondent.
        fragile = False
        rescued_at_n: Optional[int] = None
        if f["n_items"] == 1 and f["n"] - 1 >= 1:
            rescued_ok, _, _ = _nearest_achievable(
                f["reported_mean"], (f["n"] - 1) * f["n_items"], decimals
            )
            if rescued_ok:
                fragile = True
                rescued_at_n = f["n"] - 1

        # Magnitude grade (owner feedback). How many PRINTED display units the reported mean sits
        # from the nearest achievable value: a one-unit gap (kniffin 2.11 vs 2.10) — or a value the
        # authors would have produced by TRUNCATING rather than rounding (just2014 6.62 = trunc 411/62)
        # — is a marginal inconsistency that reads as a rounding nit on its own, so it is graded Minor
        # (C). A larger gap (Wansink/Festinger, >=1.9 units) stays Major (B). The cluster of them is
        # what carries the data-integrity weight (gate_fragile_grim stamps grim_cluster_size). Severity
        # is display-only — the calibration kernel branches on Status, never severity — so this cannot
        # move recall / FPR / G3. We compare against the true nearest_mean, not its rounded display.
        d_disp = abs(f["reported_mean"] - nearest_mean) / (10 ** -decimals)
        conv_ok, conv_name = (
            _reproduces_under_convention(f["reported_mean"], granularity, decimals)
            if f["n_items"] == 1
            else (False, None)
        )
        convention_reproducible = conv_ok
        marginal = d_disp <= 1.0 + EPS or convention_reproducible
        grim_severity = Severity.C if marginal else Severity.B
        # RELEVANCE anchor (critique, DESIGN §3.6). A GRIM inconsistency is a relevant HARD flag only
        # when it is a genuine impossibility of real magnitude: the gap exceeds ~1.5 printed display
        # units AND is not reproducible by a normal rounding convention. Otherwise it is a screening
        # signal, not a confirmed error. The paper-level gate (gate_grim_relevance) keeps a cluster
        # hard iff it has >=1 anchor (Wansink 1.89-2.88, Festinger 2.00), and routes an anchor-less
        # cluster (kniffin 1.00, just2014 0.67-0.85) to a single human-review note. 1.5 (not 1.0) is
        # the safe midpoint: Festinger's 2.0 clears it, kniffin's 1.0 does not.
        grim_anchor = (d_disp > 1.5 + EPS) and not convention_reproducible

        return self.make_finding(
            claim=claim,
            status=Status.FAIL,
            severity=grim_severity,
            message="reported mean is not achievable as a mean of integer responses (GRIM-inconsistent)",
            discrepancy=(
                f"mean {_fmt_mean(f['reported_mean'], decimals)} is unreachable for n={f['n']}"
                + (f"*{f['n_items']} items" if f["n_items"] != 1 else "")
                + f"; nearest achievable is {_fmt_mean(nearest_mean, decimals)} "
                f"(={nearest_k}/{granularity})"
            ),
            reported=f["reported_mean"],
            computed=nearest_mean,
            evidence=packet,
            details={
                "n": f["n"],
                "n_items": f["n_items"],
                "granularity": granularity,
                "decimals": decimals,
                "nearest_total": nearest_k,
                "nearest_mean": nearest_mean,
                # Presentation: the formatted nearest-achievable mean + its fraction, so the UI shows
                # "6.61 (410/62)" rather than a raw 16-digit float that reads like a rounding nit.
                "nearest_mean_str": _fmt_mean(nearest_mean, decimals),
                "nearest_fraction": f"{nearest_k}/{granularity}",
                # Fragility (see probe above): is this lone-mean inconsistency sensitive to N?
                "fragile": fragile,
                "rescued_at_n": rescued_at_n,
                # Magnitude grade inputs (see severity computation above): how many printed display
                # units off the nearest achievable mean, and whether a rounding convention reproduces it.
                "grid_distance_display_units": d_disp,
                "convention_reproducible": convention_reproducible,
                "convention": conv_name,
                # Relevance anchor: a genuine, substantive impossibility (vs a screening signal). The
                # paper-level gate keeps a cluster hard iff >=1 member is an anchor.
                "grim_anchor": grim_anchor,
            },
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (achievable means) + planted (impossible means) cases at small N.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). Small N keeps the grid
        coarse enough that GRIM has power; each clean mean is a real ``k/(n*items)`` rounded to its
        precision, each planted mean falls strictly between adjacent achievable values. Spans
        claim types ``mean`` and ``descriptive_mean`` (incl. a multi-item case) so per-claim-type
        FPR (G6) is exercised. >=6 clean and >=6 planted.
        """
        cases: list[SelfTestCase] = []

        # Clean: (claim_type, suffix, mean, n, n_items, decimals) — each reproduced by some total.
        clean_specs: list[tuple[str, str, float, int, int, int]] = [
            ("mean", "n20_345", 3.45, 20, 1, 2),     # 69/20 = 3.45
            ("mean", "n4_250", 2.50, 4, 1, 2),       # 10/4 = 2.50
            ("mean", "n3_333", 3.33, 3, 1, 2),       # 10/3 = 3.333 -> 3.33
            ("mean", "n8_125", 1.25, 8, 1, 2),       # 10/8 = 1.25
            ("descriptive_mean", "n5_340", 3.40, 5, 1, 2),  # 17/5 = 3.40
            ("descriptive_mean", "n10_270", 2.70, 10, 1, 2),  # 27/10 = 2.70
            ("descriptive_mean", "items_n5x2_330", 3.30, 5, 2, 2),  # 33/10 = 3.30 (n_items=2)
        ]
        for ctype, suffix, mean, n, n_items, dec in clean_specs:
            assert self._spec_is_clean(mean, n, n_items, dec), (
                f"clean spec not achievable: {suffix}"
            )
            cases.append(self._case(f"clean_{suffix}", "clean", ctype, mean, n, n_items, dec))

        # Planted: means that fall between adjacent achievable values (classic GRIM hits).
        planted_specs: list[tuple[str, str, float, int, int, int]] = [
            ("mean", "n20_346", 3.46, 20, 1, 2),     # 69/20=3.45, 70/20=3.50 -> impossible
            ("mean", "n4_260", 2.60, 4, 1, 2),       # 10/4=2.50, 11/4=2.75 -> impossible
            ("mean", "n3_330", 3.30, 3, 1, 2),       # 9/3=3.00, 10/3=3.33 -> 3.30 impossible
            ("mean", "n8_120", 1.20, 8, 1, 2),       # 9/8=1.125, 10/8=1.25 -> 1.20 impossible
            ("descriptive_mean", "n5_345", 3.45, 5, 1, 2),  # 17/5=3.40, 18/5=3.60 -> impossible
            ("descriptive_mean", "n7_500", 0.50, 7, 1, 2),  # 3/7=0.4286, 4/7=0.5714 -> impossible
            ("descriptive_mean", "items_n5x2_335", 3.35, 5, 2, 2),  # 33/10=3.30,34/10=3.40 -> impossible
        ]
        for ctype, suffix, mean, n, n_items, dec in planted_specs:
            assert not self._spec_is_clean(mean, n, n_items, dec), (
                f"planted spec is actually achievable: {suffix}"
            )
            # Also assert it has power (granularity not too large), else judge would abstain.
            assert n * n_items <= 2 * 10 ** dec, f"planted spec lacks GRIM power: {suffix}"
            cases.append(self._case(f"planted_{suffix}", "planted", ctype, mean, n, n_items, dec))

        return cases

    @staticmethod
    def _spec_is_clean(mean: float, n: int, n_items: int, decimals: int) -> bool:
        """Mirror of ``judge``'s achievability test, used to assert self_test specs are labelled
        correctly (clean specs really pass; planted really fail)."""
        granularity = n * n_items
        consistent, _k, _m = _nearest_achievable(mean, granularity, decimals)
        return consistent

    @staticmethod
    def _case(
        name: str, kind: str, claim_type: str, mean: float, n: int, n_items: int, decimals: int
    ) -> SelfTestCase:
        vals: dict[str, Any] = {MEAN_KEY: mean, N_KEY: n, DECIMALS_KEY: decimals}
        if n_items != 1:
            vals[N_ITEMS_KEY] = n_items
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.NUMBER,
            location=Location(section="self_test", quote=f"M = {mean} (n = {n})"),
            extracted_values=vals,
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The mean was {mean} (N = {n}).",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T0,
            predicate="reported_mean == integer_total/(n*n_items) at reported precision",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [Grim()]
