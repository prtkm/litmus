"""``yield_check.v1`` — a real T1 verifier: is a reported reaction yield physically possible?

The flagship "impossible-yield" wedge (DESIGN §6.1 class A, §17, §19 WS-D): a paper reports a
percent yield above 100% (more product than stoichiometry permits) or below 0%. That is not an
inconsistency you argue about — it is a hard physical bound a deterministic check nails (T1: fixed
external knowledge, the stoichiometric ceiling). A close cousin of the T0 recompute core, but the
bound it enforces (``0 ≤ yield ≤ 100``) comes from chemistry, not from the paper's own arithmetic.

Contract with the extractor (DESIGN §11): a claim of type ``yield`` / ``reaction_yield`` /
``percent_yield`` rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values``
carries a reported yield, and *optionally* the molar quantities to recompute it::

    {"reported_yield_pct": <number>}
    # one row reporting SEVERAL product yields disambiguates them per product; each is still a
    # percent yield owing 0<=y<=100, so the physical bound is checked on ALL of them:
    {"reported_ipl_yield_pct": <number>, "reported_gvl_yield_pct": <number>, ...}
    # optionally, to recompute the theoretical yield (T0 layer) — paired with the bare key only:
    {"mol_product": <number>, "mol_limiting_reagent": <number>, "stoich_coeff_ratio": <number>}

``stoich_coeff_ratio`` (default ``1.0``) is mol product expected per mol limiting reagent for a
quantitative reaction; ``theoretical = mol_limiting_reagent * stoich_coeff_ratio`` and
``recomputed_yield = 100 * mol_product / theoretical``.

``judge`` applies two layers, hardest first:

  * **T1 physical bound** (always, on EVERY reported yield — the bare ``reported_yield_pct`` and
    any disambiguated ``*_yield_pct`` a multi-product row carries):
      * ``reported_yield_pct > 100 + tol``  -> FAIL **severity A** ("yield exceeds 100%
            stoichiometric maximum — impossible").
      * ``reported_yield_pct < -tol``       -> FAIL **severity A** ("negative yield").
  * **T0 recompute** (only if the mol pair is present *and* the bound passed):
      * ``|recomputed_yield - reported_yield_pct| > tol`` -> FAIL **severity B** (the reported
            yield disagrees with what the molar quantities imply).

Abstain (DESIGN §3.4: abstain > guess) when neither any ``*_yield_pct`` key nor the mol pair is
present, or when a recompute is asked for but the theoretical yield is zero (undefined).

Every ``FAIL`` ships an :class:`~litmus.core.finding.EvidencePacket` whose stdlib-only
``recompute_script`` reprints the exact verdict line, so a skeptical reader reruns it (DESIGN §3.2:
no script, no flag). No RNG, clock, or network anywhere (DESIGN §7 G4); the kernel verifies that.
"""

from __future__ import annotations

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

# Absolute tolerance, in percentage points, on both the physical bound and the recompute
# comparison (presentation rounding, DESIGN §6.3). 100.4% is rounding noise; 142% is impossible.
TOLERANCE = 0.5

REPORTED_KEY = "reported_yield_pct"
# When ONE reaction row reports several product yields, the extractor disambiguates them per
# product — ``reported_ipl_yield_pct``, ``reported_gvl_yield_pct``, ... — instead of the bare
# canonical key, so it can keep them apart in a single evidence record (DESIGN §11). Every such
# key is still a percent yield bound by 0<=y<=100, so the T1 physical check must see all of them;
# otherwise a multi-product row binds nothing and the whole reaction goes unchecked. Any top-level
# key ending in this suffix counts (the bare ``reported_yield_pct`` included); the bare key stays
# the primary — it alone pairs with the molar recompute.
YIELD_PCT_SUFFIX = "_yield_pct"
MOL_PRODUCT_KEY = "mol_product"
MOL_LIMITING_KEY = "mol_limiting_reagent"
STOICH_RATIO_KEY = "stoich_coeff_ratio"

MAX_YIELD = 100.0  # the stoichiometric ceiling (T1 physical bound)


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point (``142``); genuinely fractional values print
    via ``repr`` (``36.36363636363637``). The SAME function is embedded verbatim in the recompute
    script, so ``judge``'s ``expected_output`` and the script's stdout are byte-identical
    (DESIGN §7 G3).
    """
    if isinstance(value, bool):  # bool is an int subclass; keep it numeric-looking
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --- the canonical verdict lines both sides print ---------------------------
def _over_line(reported: float | int) -> str:
    return f"IMPOSSIBLE_YIELD reported={_fmt(reported)} max={_fmt(MAX_YIELD)}"


def _negative_line(reported: float | int) -> str:
    return f"NEGATIVE_YIELD reported={_fmt(reported)}"


def _mismatch_line(reported: float | int, computed: float) -> str:
    return f"YIELD_MISMATCH reported={_fmt(reported)} computed={_fmt(computed)}"


def _find_reported(evidence: list[Evidence]) -> Optional[tuple[Evidence, float | int]]:
    """The bare canonical ``reported_yield_pct`` — the *primary* yield, the one the molar
    recompute pairs with (a disambiguated per-product key can't be matched to a single mol pair)."""
    for ev in evidence:
        vals = ev.extracted_values or {}
        if REPORTED_KEY in vals and _is_number(vals[REPORTED_KEY]):
            return ev, vals[REPORTED_KEY]
    return None


def _find_all_reported(
    evidence: list[Evidence],
) -> list[tuple[Evidence, str, float | int]]:
    """Every top-level reported-yield percentage across all evidence, canonical key first.

    Beyond the bare ``reported_yield_pct`` this also picks up the disambiguated per-product
    spellings (``reported_gvl_yield_pct``, ``isolated_ipl_yield_pct``, ...) the extractor uses
    when one reaction row reports several product yields at once. Each is still a percent yield
    owing the 0<=y<=100 bound, so ``judge`` runs the T1 physical check over all of them —
    otherwise a multi-product row binds nothing and the whole reaction goes unchecked (DESIGN §11
    key-alignment). Returns ``(ev, key, value)`` triples with the bare canonical key first so it
    stays the primary, the rest in transcription order. Numeric values only.
    """
    primary: list[tuple[Evidence, str, float | int]] = []
    extra: list[tuple[Evidence, str, float | int]] = []
    for ev in evidence:
        vals = ev.extracted_values or {}
        for key, value in vals.items():
            if not _is_number(value):
                continue
            if key == REPORTED_KEY:
                primary.append((ev, key, value))
            elif key.endswith(YIELD_PCT_SUFFIX):
                extra.append((ev, key, value))
    return primary + extra


def _find_mol_pair(
    evidence: list[Evidence],
) -> Optional[tuple[Evidence, float, float, float]]:
    """First evidence carrying mol_product AND mol_limiting_reagent (both numeric).

    Returns ``(ev, mol_product, mol_limiting_reagent, stoich_coeff_ratio)`` with the ratio
    defaulting to 1.0 when absent.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        if MOL_PRODUCT_KEY in vals and MOL_LIMITING_KEY in vals:
            mp, ml = vals[MOL_PRODUCT_KEY], vals[MOL_LIMITING_KEY]
            ratio = vals.get(STOICH_RATIO_KEY, 1.0)
            if _is_number(mp) and _is_number(ml) and _is_number(ratio):
                return ev, mp, ml, ratio
    return None


# --- recompute scripts (self-contained, stdlib-only, deterministic) ----------
def _bound_script(reported: float | int, negative: bool) -> str:
    """Script for a T1 physical-bound FAIL (>100% or <0%).

    It hardcodes the reported yield and the ceiling, re-derives the violation, and prints exactly
    the matching ``IMPOSSIBLE_YIELD`` / ``NEGATIVE_YIELD`` line — or ``OK`` if (counterfactually)
    the bound held. The ``_fmt`` body is identical to the module-level one so output is byte-exact.
    """
    return (
        "# LITMUS recompute script for yield_check.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"REPORTED_YIELD_PCT = {reported!r}\n"
        f"MAX_YIELD = {MAX_YIELD!r}\n"
        f"TOLERANCE = {TOLERANCE!r}\n"
        "\n"
        "\n"
        "def _fmt(value):\n"
        "    if isinstance(value, bool):\n"
        "        value = int(value)\n"
        "    if isinstance(value, int):\n"
        "        return str(value)\n"
        "    if isinstance(value, float) and value.is_integer():\n"
        "        return str(int(value))\n"
        "    return repr(value)\n"
        "\n"
        "\n"
        "if REPORTED_YIELD_PCT > MAX_YIELD + TOLERANCE:\n"
        "    print('IMPOSSIBLE_YIELD reported=' + _fmt(REPORTED_YIELD_PCT) + ' max=' + _fmt(MAX_YIELD))\n"
        "elif REPORTED_YIELD_PCT < -TOLERANCE:\n"
        "    print('NEGATIVE_YIELD reported=' + _fmt(REPORTED_YIELD_PCT))\n"
        "else:\n"
        "    print('OK')\n"
    )


def _recompute_script(
    reported: float | int, mol_product: float, mol_limiting: float, ratio: float
) -> str:
    """Script for a T0 recompute FAIL (reported yield disagrees with the molar quantities).

    Hardcodes the molar quantities, recomputes ``100*mol_product/(mol_limiting*ratio)``, and prints
    the matching ``YIELD_MISMATCH`` line (or ``OK``). ``_fmt`` body identical to module-level.
    """
    return (
        "# LITMUS recompute script for yield_check.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"REPORTED_YIELD_PCT = {reported!r}\n"
        f"MOL_PRODUCT = {mol_product!r}\n"
        f"MOL_LIMITING_REAGENT = {mol_limiting!r}\n"
        f"STOICH_COEFF_RATIO = {ratio!r}\n"
        f"TOLERANCE = {TOLERANCE!r}\n"
        "\n"
        "\n"
        "def _fmt(value):\n"
        "    if isinstance(value, bool):\n"
        "        value = int(value)\n"
        "    if isinstance(value, int):\n"
        "        return str(value)\n"
        "    if isinstance(value, float) and value.is_integer():\n"
        "        return str(int(value))\n"
        "    return repr(value)\n"
        "\n"
        "\n"
        "theoretical = MOL_LIMITING_REAGENT * STOICH_COEFF_RATIO\n"
        "computed = 100.0 * MOL_PRODUCT / theoretical\n"
        "if abs(computed - REPORTED_YIELD_PCT) <= TOLERANCE:\n"
        "    print('OK')\n"
        "else:\n"
        "    print('YIELD_MISMATCH reported=' + _fmt(REPORTED_YIELD_PCT) + ' computed=' + _fmt(computed))\n"
    )


class YieldCheck(Verifier):
    """Reported reaction yield is physically possible (0 ≤ y ≤ 100) and matches its molar
    quantities (DESIGN §6.1, §17, T1)."""

    manifest = VerifierManifest(
        id="yield_check.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T1,
        determinism=Determinism.DETERMINISTIC,
        consumes=["yield", "reaction_yield", "percent_yield"],
        capability_tags=["chemistry", "yield", "stoichiometry"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["yield bound + theoretical-yield recompute"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a reported reaction yield obeys the stoichiometric bound 0<=y<=100 (T1 "
            "physical) and, when the molar quantities are present, equals 100*mol_product/"
            "(mol_limiting_reagent*stoich_coeff_ratio) (T0 recompute). Binds to evidence carrying "
            "extracted_values {'reported_yield_pct': n} — or, for a row reporting several product "
            "yields, the disambiguated per-product keys {'reported_<product>_yield_pct': n, ...} — "
            "and optionally {'mol_product': x, 'mol_limiting_reagent': y, 'stoich_coeff_ratio': r}."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        # Every reported yield (bare canonical key + any disambiguated per-product *_yield_pct),
        # primary first; plus the bare key alone, which is the only one the molar recompute pairs
        # with (a per-product key can't be matched to a single mol pair).
        reported_all = _find_all_reported(evidence)
        reported_bound = _find_reported(evidence)
        mol_bound = _find_mol_pair(evidence)

        if not reported_all and mol_bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries 'reported_yield_pct' (or a disambiguated "
                "'*_yield_pct') or the (mol_product, mol_limiting_reagent) pair; cannot "
                "check a yield (DESIGN §3.4: abstain > guess)",
            )

        # --- T1 physical bound (hardest first), over EVERY reported yield ----
        # A row reporting several product yields packs them under disambiguated keys
        # (reported_gvl_yield_pct, ...); each still owes 0<=y<=100, so check them all or the whole
        # multi-product reaction goes unchecked (DESIGN §11 key-alignment). First violator wins —
        # deterministic: bare key first, then transcription order.
        for ev, key, reported in reported_all:
            label = "" if key == REPORTED_KEY else f" ({key})"

            if reported > MAX_YIELD + TOLERANCE:
                expected = _over_line(reported)
                script = _bound_script(reported, negative=False)
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
                    severity=Severity.A,
                    message="yield exceeds 100% stoichiometric maximum — impossible",
                    discrepancy=(
                        f"reported {_fmt(reported)}%{label} yield exceeds the 100% "
                        "stoichiometric maximum"
                    ),
                    reported=reported,
                    computed=MAX_YIELD,
                    evidence=packet,
                    details={"bound": "yield <= 100%", "violation": "over", "key": key},
                )

            if reported < -TOLERANCE:
                expected = _negative_line(reported)
                script = _bound_script(reported, negative=True)
                packet = EvidencePacket(
                    quote=ev.location.quote,
                    location=ev.location,
                    recompute_script=script,
                    expected_output=expected,
                    script_dependencies=[],
                )
                return self.make_finding(
                    claim=claim,
                    status=Status.FAIL,
                    severity=Severity.A,
                    message="negative yield — impossible",
                    discrepancy=f"reported {_fmt(reported)}%{label} yield is negative",
                    reported=reported,
                    computed=0,
                    evidence=packet,
                    details={"bound": "yield >= 0%", "violation": "negative", "key": key},
                )

        # --- T0 recompute (only if the molar quantities are present) ---------
        if mol_bound is not None:
            ev, mol_product, mol_limiting, ratio = mol_bound
            theoretical = mol_limiting * ratio
            if theoretical == 0:
                return self.abstain(
                    claim,
                    "theoretical yield is zero (mol_limiting_reagent*stoich_coeff_ratio == 0): "
                    "percent yield is undefined; abstaining (DESIGN §3.4)",
                )
            computed = 100.0 * mol_product / theoretical

            # Only meaningful to recompute against a *reported* yield.
            if reported_bound is not None:
                reported = reported_bound[1]
                if abs(computed - reported) > TOLERANCE:
                    expected = _mismatch_line(reported, computed)
                    script = _recompute_script(reported, mol_product, mol_limiting, ratio)
                    packet = EvidencePacket(
                        quote=ev.location.quote,
                        location=ev.location,
                        recompute_script=script,
                        expected_output=expected,
                        script_dependencies=[],
                    )
                    return self.make_finding(
                        claim=claim,
                        status=Status.FAIL,
                        severity=Severity.B,
                        message="reported yield does not match the recomputed theoretical yield",
                        discrepancy=(
                            f"reported {_fmt(reported)}% but molar quantities imply "
                            f"{_fmt(computed)}%"
                        ),
                        reported=reported,
                        computed=computed,
                        evidence=packet,
                        details={
                            "mol_product": mol_product,
                            "mol_limiting_reagent": mol_limiting,
                            "stoich_coeff_ratio": ratio,
                            "theoretical": theoretical,
                        },
                    )

        # Everything checks out (every bound held; recompute, if any, agreed).
        reported_for_pass = reported_all[0][2] if reported_all else None
        return self.make_finding(
            claim=claim,
            status=Status.PASS,
            message="reported yield is within the stoichiometric bound and matches its quantities",
            reported=reported_for_pass,
            details={"checked_bound": bool(reported_all), "recomputed": mol_bound is not None},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (possible, consistent yields) + planted (impossible/negative/inconsistent) cases.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted across claim types ``percent_yield`` and ``reaction_yield`` so per-claim-type FPR
        (G6) is exercised. Planted set spans every failure mode: >100% (A), <0% (A), a
        molar-recompute mismatch (B), and a multi-product row whose disambiguated ``*_yield_pct``
        keys carry one impossible yield (A). Clean set includes an all-in-bounds multi-product row.
        """
        cases: list[SelfTestCase] = []

        # Clean: (claim_type, suffix, extracted_values dict)
        clean_specs: list[tuple[str, str, dict]] = [
            ("percent_yield", "modest", {REPORTED_KEY: 72.0}),
            ("percent_yield", "high_ok", {REPORTED_KEY: 98.5}),
            ("percent_yield", "exactly_100", {REPORTED_KEY: 100.0}),
            ("reaction_yield", "low", {REPORTED_KEY: 12.0}),
            ("reaction_yield", "zero_ok", {REPORTED_KEY: 0.0}),
            # Reported yield consistent with the molar quantities: 0.85/1.0 -> 85%.
            (
                "reaction_yield",
                "recompute_match",
                {
                    REPORTED_KEY: 85.0,
                    MOL_PRODUCT_KEY: 0.85,
                    MOL_LIMITING_KEY: 1.0,
                    STOICH_RATIO_KEY: 1.0,
                },
            ),
            # Multi-product row (the roy2025 shape): several disambiguated *_yield_pct keys, no
            # bare reported_yield_pct, all within bound -> must bind and PASS (not abstain).
            (
                "reaction_yield",
                "multi_product_ok",
                {"reported_ipl_yield_pct": 50.0, "reported_gvl_yield_pct": 15.0},
            ),
        ]
        for ctype, suffix, vals in clean_specs:
            assert self._spec_is_clean(vals), f"clean spec not clean: {suffix}"
            cases.append(self._case(f"clean_{ctype}_{suffix}", "clean", ctype, vals))

        # Planted: known-wrong instances judge must FAIL.
        planted_specs: list[tuple[str, str, dict]] = [
            # The flagship impossible-yield wedge: 142% > 100% (severity A).
            ("percent_yield", "over_142", {REPORTED_KEY: 142.0}),
            ("percent_yield", "over_barely", {REPORTED_KEY: 103.0}),
            ("reaction_yield", "over_120", {REPORTED_KEY: 120.0}),
            # Negative yields (severity A).
            ("percent_yield", "negative", {REPORTED_KEY: -5.0}),
            ("reaction_yield", "negative_small", {REPORTED_KEY: -0.5 - TOLERANCE}),
            # Molar-recompute mismatch (severity B): 0.50/1.0 -> 50%, not the claimed 80%.
            (
                "reaction_yield",
                "recompute_mismatch",
                {
                    REPORTED_KEY: 80.0,
                    MOL_PRODUCT_KEY: 0.50,
                    MOL_LIMITING_KEY: 1.0,
                    STOICH_RATIO_KEY: 1.0,
                },
            ),
            # Multi-product row where ONE product yield is impossible (>100%) — the coverage gap
            # this verifier now closes: gvl 142% must FAIL even with no bare reported_yield_pct.
            (
                "reaction_yield",
                "multi_product_over",
                {"reported_ipl_yield_pct": 61.0, "reported_gvl_yield_pct": 142.0},
            ),
        ]
        for ctype, suffix, vals in planted_specs:
            assert not self._spec_is_clean(vals), f"planted spec is actually clean: {suffix}"
            cases.append(self._case(f"planted_{ctype}_{suffix}", "planted", ctype, vals))

        return cases

    @staticmethod
    def _spec_is_clean(vals: dict) -> bool:
        """Mirror of ``judge``'s verdict logic, used only to assert the self_test specs are
        labelled correctly (clean specs really pass; planted really fail)."""
        # T1 physical bound over EVERY reported yield (bare + disambiguated *_yield_pct).
        for key, value in vals.items():
            if key.endswith(YIELD_PCT_SUFFIX) and _is_number(value):
                if value > MAX_YIELD + TOLERANCE or value < -TOLERANCE:
                    return False
        # T0 recompute pairs only with the bare canonical key.
        reported = vals.get(REPORTED_KEY)
        if MOL_PRODUCT_KEY in vals and MOL_LIMITING_KEY in vals:
            theoretical = vals[MOL_LIMITING_KEY] * vals.get(STOICH_RATIO_KEY, 1.0)
            if theoretical != 0 and _is_number(reported):
                computed = 100.0 * vals[MOL_PRODUCT_KEY] / theoretical
                if abs(computed - reported) > TOLERANCE:
                    return False
        return True

    @staticmethod
    def _case(name: str, kind: str, claim_type: str, vals: dict) -> SelfTestCase:
        reported = vals.get(REPORTED_KEY)
        if reported is None:
            # Multi-product specs carry only disambiguated *_yield_pct keys; surface one for the
            # synthetic quote/text (cosmetic — judge scans them all regardless).
            reported = next(
                (v for k, v in vals.items() if k.endswith(YIELD_PCT_SUFFIX) and _is_number(v)),
                None,
            )
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.NUMBER,
            location=Location(section="self_test", quote=f"yield {reported}%"),
            extracted_values=dict(vals),
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The reaction proceeded in {reported}% yield.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T1,
            predicate="0 <= reported_yield_pct <= 100 and matches molar quantities",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [YieldCheck()]
