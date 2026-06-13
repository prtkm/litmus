"""``unit_consistency.v1`` — a real T0 verifier: does a stated dual-unit equivalence hold?

Papers routinely restate a quantity in two units — "a 5.0 mg (0.5 g) dose", "120 min (2 h)".
When the two disagree (5.0 mg is 0.005 g, not 0.5 g) it is a pure unit-conversion error, caught
by deterministic arithmetic on a fixed factor table (T0: internal arithmetic, no external
knowledge beyond the SI definitions baked into the table). A close cousin of the T0 recompute core.

Contract with the extractor (DESIGN §11): a claim of type ``unit_consistency`` / ``dual_unit`` /
``conversion`` rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values`` carries
the two stated (value, unit) pairs::

    {"value_a": <number>, "unit_a": "<unit>", "value_b": <number>, "unit_b": "<unit>"}

``judge`` looks both units up in a per-dimension factor table (unit -> base-unit multiplier),
converts ``value_a`` into ``unit_b``'s base, and compares to ``value_b`` converted to that same
base:

  * either unit unknown, or the two units belong to different dimensions  -> ABSTAIN
        (DESIGN §3.4: abstain > guess — a cross-dimension "equivalence" is malformed, not a finding).
  * ``|a_in_base - b_in_base| <= rel_tol * max(|b_in_base|, 1e-12)``       -> PASS.
  * otherwise                                                              -> FAIL **severity B**
        shipping an EvidencePacket whose stdlib-only ``recompute_script`` reprints the discrepancy
        line, so a skeptical reader reruns it (DESIGN §3.2: no script, no flag).

Temperature is deliberately excluded (offset scales, not pure multipliers). Deterministic
throughout: no RNG, clock, or network (DESIGN §7 G4). The kernel verifies that empirically.
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

# Relative tolerance on the converted comparison (presentation rounding, DESIGN §6.3). A stated
# "5.0 mg (0.005 g)" matches; "5.0 mg (0.5 g)" — off by 100x — does not.
REL_TOL = 1e-6

VALUE_A_KEY = "value_a"
UNIT_A_KEY = "unit_a"
VALUE_B_KEY = "value_b"
UNIT_B_KEY = "unit_b"

# Per-dimension unit -> base-unit multiplier (factor to convert a value in `unit` into the
# dimension's base unit). Exact SI definitions; no temperature (offset scales). Both the ASCII
# "u" and the micro sign "µ" spellings of micro are accepted. (DESIGN §7 G3: this table is the
# only "knowledge", and it is embedded verbatim in the recompute script.)
UNIT_FACTORS: dict[str, dict[str, float]] = {
    "mass": {  # base: gram
        "ng": 1e-9,
        "ug": 1e-6,
        "µg": 1e-6,
        "mg": 1e-3,
        "g": 1.0,
        "kg": 1e3,
    },
    "length": {  # base: metre
        "nm": 1e-9,
        "um": 1e-6,
        "µm": 1e-6,
        "mm": 1e-3,
        "cm": 1e-2,
        "m": 1.0,
        "km": 1e3,
    },
    "energy": {  # base: joule
        "J": 1.0,
        "kJ": 1e3,
        "cal": 4.184,
        "kcal": 4184.0,
        "eV": 1.602176634e-19,
    },
    "volume": {  # base: litre
        "uL": 1e-6,
        "µL": 1e-6,
        "mL": 1e-3,
        "L": 1.0,
    },
    "time": {  # base: second
        "s": 1.0,
        "min": 60.0,
        "h": 3600.0,
    },
    "pressure": {  # base: pascal
        "Pa": 1.0,
        "kPa": 1e3,
        "bar": 1e5,
        "atm": 101325.0,
    },
}


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point; genuinely fractional values print via ``repr``.
    The SAME function is embedded verbatim in the recompute script so ``judge``'s
    ``expected_output`` and the script's stdout are byte-identical (DESIGN §7 G3).
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


def _dimension_of(unit: str) -> Optional[str]:
    """The dimension whose factor table contains ``unit``, or None if unknown."""
    for dim, table in UNIT_FACTORS.items():
        if unit in table:
            return dim
    return None


def _mismatch_line(a_in_base: float, b_in_base: float) -> str:
    """The single canonical discrepancy line both sides print (values in the shared base unit)."""
    return f"UNIT_MISMATCH a={_fmt(a_in_base)} b={_fmt(b_in_base)}"


def _find_bound_values(
    evidence: list[Evidence],
) -> Optional[tuple[Evidence, float, str, float, str]]:
    """First evidence carrying value_a, unit_a, value_b, unit_b (values numeric, units strings).

    Returns ``(ev, value_a, unit_a, value_b, unit_b)``.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        if all(k in vals for k in (VALUE_A_KEY, UNIT_A_KEY, VALUE_B_KEY, UNIT_B_KEY)):
            va, ua, vb, ub = (
                vals[VALUE_A_KEY],
                vals[UNIT_A_KEY],
                vals[VALUE_B_KEY],
                vals[UNIT_B_KEY],
            )
            if _is_number(va) and _is_number(vb) and isinstance(ua, str) and isinstance(ub, str):
                return ev, va, ua, vb, ub
    return None


def _build_recompute_script(
    value_a: float, unit_a: str, value_b: float, unit_b: str, dim: str
) -> str:
    """A self-contained stdlib program that re-converts both sides and prints the verdict line.

    Embeds *only the relevant dimension's* factor sub-table (the sole "knowledge"), converts both
    stated quantities into that dimension's base unit, and prints exactly one line:
      * ``OK`` if they agree (within relative tolerance),
      * ``UNIT_MISMATCH a=<a> b=<b>`` otherwise (both in the shared base unit).
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    sub_table = UNIT_FACTORS[dim]
    return (
        "# LITMUS recompute script for unit_consistency.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"FACTORS = {sub_table!r}\n"
        f"VALUE_A = {value_a!r}\n"
        f"UNIT_A = {unit_a!r}\n"
        f"VALUE_B = {value_b!r}\n"
        f"UNIT_B = {unit_b!r}\n"
        f"REL_TOL = {REL_TOL!r}\n"
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
        "a_in_base = VALUE_A * FACTORS[UNIT_A]\n"
        "b_in_base = VALUE_B * FACTORS[UNIT_B]\n"
        "if abs(a_in_base - b_in_base) <= REL_TOL * max(abs(b_in_base), 1e-12):\n"
        "    print('OK')\n"
        "else:\n"
        "    print('UNIT_MISMATCH a=' + _fmt(a_in_base) + ' b=' + _fmt(b_in_base))\n"
    )


class UnitConsistency(Verifier):
    """A stated dual-unit equivalence holds under exact conversion (DESIGN §6.1, T0)."""

    manifest = VerifierManifest(
        id="unit_consistency.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["unit_consistency", "dual_unit", "conversion"],
        capability_tags=["units", "conversion"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["unit factor table + conversion check"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a stated dual-unit equivalence (value_a unit_a == value_b unit_b) holds "
            "under exact conversion within a dimension (mass/length/energy/volume/time/pressure; "
            "no temperature). T0 arithmetic on a fixed factor table. Binds to evidence carrying "
            "extracted_values {'value_a','unit_a','value_b','unit_b'}. Abstains on an unknown unit "
            "or a cross-dimension pair."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_values(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries value_a, unit_a, value_b, and unit_b; "
                "cannot check a unit equivalence (DESIGN §3.4: abstain > guess)",
            )
        ev, value_a, unit_a, value_b, unit_b = bound

        dim_a = _dimension_of(unit_a)
        dim_b = _dimension_of(unit_b)
        if dim_a is None or dim_b is None:
            unknown = unit_a if dim_a is None else unit_b
            return self.abstain(
                claim,
                f"unit {unknown!r} is not in the factor table; cannot convert "
                "(DESIGN §3.4: abstain > guess)",
            )
        if dim_a != dim_b:
            return self.abstain(
                claim,
                f"unit_a ({unit_a}, {dim_a}) and unit_b ({unit_b}, {dim_b}) are different "
                "dimensions; a cross-dimension equivalence is malformed, not a finding (DESIGN §3.4)",
            )

        factors = UNIT_FACTORS[dim_a]
        a_in_base = value_a * factors[unit_a]
        b_in_base = value_b * factors[unit_b]

        if abs(a_in_base - b_in_base) <= REL_TOL * max(abs(b_in_base), 1e-12):
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="the stated dual-unit equivalence holds under exact conversion",
                reported=value_b,
                computed=a_in_base / factors[unit_b],  # value_a expressed in unit_b
                details={
                    "dimension": dim_a,
                    "value_a": value_a,
                    "unit_a": unit_a,
                    "value_b": value_b,
                    "unit_b": unit_b,
                },
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        expected = _mismatch_line(a_in_base, b_in_base)
        script = _build_recompute_script(value_a, unit_a, value_b, unit_b, dim_a)
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
            severity=Severity.B,
            message="the stated dual-unit equivalence does not hold under exact conversion",
            discrepancy=(
                f"{_fmt(value_a)} {unit_a} = {_fmt(a_in_base)} {dim_a}-base, but "
                f"{_fmt(value_b)} {unit_b} = {_fmt(b_in_base)} {dim_a}-base"
            ),
            reported=value_b,
            computed=a_in_base / factors[unit_b],
            evidence=packet,
            details={
                "dimension": dim_a,
                "value_a": value_a,
                "unit_a": unit_a,
                "value_b": value_b,
                "unit_b": unit_b,
                "a_in_base": a_in_base,
                "b_in_base": b_in_base,
            },
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (correct equivalences) + planted (wrong equivalences) across >=2 dimensions.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted, with ``claim_type`` set to the *dimension* so per-claim-type FPR (G6) is exercised
        across mass/length/energy/volume/time/pressure. Exercises both micro spellings and several
        scale factors.
        """
        cases: list[SelfTestCase] = []

        # Clean: value_a unit_a really equals value_b unit_b.
        # (dimension/claim_type, suffix, value_a, unit_a, value_b, unit_b)
        clean_specs: list[tuple[str, str, float, str, float, str]] = [
            ("mass", "mg_to_g", 5.0, "mg", 0.005, "g"),
            ("mass", "kg_to_g", 2.0, "kg", 2000.0, "g"),
            ("length", "cm_to_m", 150.0, "cm", 1.5, "m"),
            ("time", "min_to_h", 120.0, "min", 2.0, "h"),
            ("volume", "mL_to_L", 250.0, "mL", 0.25, "L"),
            ("energy", "kcal_to_kJ", 1.0, "kcal", 4.184, "kJ"),
            ("pressure", "atm_to_kPa", 1.0, "atm", 101.325, "kPa"),
            ("mass", "micro_sign", 1000.0, "µg", 1.0, "mg"),
        ]
        for dim, suffix, va, ua, vb, ub in clean_specs:
            assert self._spec_is_clean(va, ua, vb, ub), f"clean spec not clean: {suffix}"
            cases.append(self._case(f"clean_{dim}_{suffix}", "clean", dim, va, ua, vb, ub))

        # Planted: the stated equivalence is wrong. judge must FAIL these.
        planted_specs: list[tuple[str, str, float, str, float, str]] = [
            # The archetype: "5.0 mg (0.5 g)" — off by 100x.
            ("mass", "mg_g_100x", 5.0, "mg", 0.5, "g"),
            ("mass", "kg_g_wrong", 2.0, "kg", 200.0, "g"),  # should be 2000
            ("length", "cm_m_10x", 150.0, "cm", 15.0, "m"),  # should be 1.5
            ("time", "min_h_wrong", 120.0, "min", 1.0, "h"),  # should be 2
            ("volume", "mL_L_wrong", 250.0, "mL", 2.5, "L"),  # should be 0.25
            ("energy", "kcal_kJ_wrong", 1.0, "kcal", 41.84, "kJ"),  # should be 4.184
            ("pressure", "atm_kPa_wrong", 1.0, "atm", 10.1325, "kPa"),  # should be 101.325
            ("mass", "micro_wrong", 1000.0, "µg", 10.0, "mg"),  # should be 1
        ]
        for dim, suffix, va, ua, vb, ub in planted_specs:
            assert not self._spec_is_clean(va, ua, vb, ub), f"planted spec is actually clean: {suffix}"
            cases.append(self._case(f"planted_{dim}_{suffix}", "planted", dim, va, ua, vb, ub))

        return cases

    @staticmethod
    def _spec_is_clean(value_a: float, unit_a: str, value_b: float, unit_b: str) -> bool:
        """Mirror of ``judge``'s conversion test, used to assert the self_test specs are labelled
        correctly. Asserts the units are same-dimension and known (a malformed self_test spec is a
        bug, not an abstain)."""
        dim_a = _dimension_of(unit_a)
        dim_b = _dimension_of(unit_b)
        assert dim_a is not None and dim_a == dim_b, (
            f"self_test spec uses unknown or cross-dimension units: {unit_a}, {unit_b}"
        )
        factors = UNIT_FACTORS[dim_a]
        a_in_base = value_a * factors[unit_a]
        b_in_base = value_b * factors[unit_b]
        return abs(a_in_base - b_in_base) <= REL_TOL * max(abs(b_in_base), 1e-12)

    @staticmethod
    def _case(
        name: str, kind: str, claim_type: str,
        value_a: float, unit_a: str, value_b: float, unit_b: str,
    ) -> SelfTestCase:
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.NUMBER,
            location=Location(
                section="self_test", quote=f"{value_a} {unit_a} ({value_b} {unit_b})"
            ),
            extracted_values={
                VALUE_A_KEY: value_a,
                UNIT_A_KEY: unit_a,
                VALUE_B_KEY: value_b,
                UNIT_B_KEY: unit_b,
            },
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"{value_a} {unit_a} equals {value_b} {unit_b}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T0,
            predicate="value_a*factor(unit_a) == value_b*factor(unit_b)",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [UnitConsistency()]
