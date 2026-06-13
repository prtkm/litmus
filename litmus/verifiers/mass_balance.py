"""``mass_balance.v1`` — a real T1 verifier: was mass conserved, or created from nothing?

Conservation of mass is a hard physical bound (DESIGN §6.1 class A, T1: fixed external
knowledge): the mass coming out of a reaction or workup cannot exceed the mass that went in.
Recovering 5.0 g of product from 3.0 g of starting material is impossible — mass was created.
Recovering less than you put in is ordinary (incomplete reaction, transfer losses) and PASSes;
this verifier only flags the impossible direction.

Contract with the extractor (DESIGN §11): a claim of type ``mass_balance`` / ``mass_closure`` /
``conservation`` rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values``
carries one or both mass pairs, in grams::

    {"reactant_masses": [<g>, ...], "product_masses": [<g>, ...]}
    {"input_mass": <g>, "recovered_mass": <g>}

``judge`` sums each side it is given (preferring an explicit input/recovered pair, then the
reactant/product lists; if both are present it checks the tightest interpretation — total output
vs total input across whichever pairs exist) and compares:

  * neither pair present                          -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``total_output <= total_input + tol``         -> PASS (conserved, or ordinary loss).
  * ``total_output > total_input + tol``           -> FAIL **severity A** ("mass created: output
        exceeds input — violates conservation of mass") shipping an EvidencePacket whose
        stdlib-only ``recompute_script`` reprints the discrepancy line (DESIGN §3.2).

Deterministic throughout: no RNG, clock, or network in ``judge`` or the emitted script
(DESIGN §7 G4). The calibration kernel verifies that empirically.
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

# Absolute tolerance, in grams, on the closure comparison (presentation rounding / scale
# precision, DESIGN §6.3). 0.001 g over is rounding; 2 g over is mass from nowhere.
TOLERANCE = 1e-6

REACTANTS_KEY = "reactant_masses"
PRODUCTS_KEY = "product_masses"
INPUT_KEY = "input_mass"
RECOVERED_KEY = "recovered_mass"


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point (``3``); genuinely fractional values print via
    ``repr`` (``3.5``). The SAME function is embedded verbatim in the recompute script so
    ``judge``'s ``expected_output`` and the script's stdout are byte-identical (DESIGN §7 G3).
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


def _mass_created_line(total_input: float, total_output: float) -> str:
    """The single canonical discrepancy line both sides print."""
    return f"MASS_CREATED input={_fmt(total_input)} output={_fmt(total_output)}"


def _numbers(seq: Any) -> Optional[list[float]]:
    """Return ``seq`` as a list of numbers, or None if it is not a clean numeric sequence."""
    if not isinstance(seq, (list, tuple)):
        return None
    if not all(_is_number(x) for x in seq):
        return None
    return [float(x) for x in seq]


def _find_masses(
    evidence: list[Evidence],
) -> Optional[tuple[Evidence, float, float, dict]]:
    """First evidence from which a total input AND a total output mass can be derived.

    Accepts either the reactant/product lists, the input/recovered pair, or both (when both are
    present their sums are pooled into a single input total and output total). Returns
    ``(ev, total_input, total_output, components)`` where ``components`` records what was summed
    (for the script + details). Returns None if no usable input/output pair is found.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        inputs: list[float] = []
        outputs: list[float] = []
        comp: dict[str, Any] = {}

        reactants = _numbers(vals[REACTANTS_KEY]) if REACTANTS_KEY in vals else None
        products = _numbers(vals[PRODUCTS_KEY]) if PRODUCTS_KEY in vals else None
        if reactants is not None and products is not None:
            inputs.extend(reactants)
            outputs.extend(products)
            comp[REACTANTS_KEY] = reactants
            comp[PRODUCTS_KEY] = products

        if INPUT_KEY in vals and RECOVERED_KEY in vals:
            im, rm = vals[INPUT_KEY], vals[RECOVERED_KEY]
            if _is_number(im) and _is_number(rm):
                inputs.append(float(im))
                outputs.append(float(rm))
                comp[INPUT_KEY] = float(im)
                comp[RECOVERED_KEY] = float(rm)

        if inputs and outputs:
            return ev, sum(inputs), sum(outputs), comp
    return None


def _build_recompute_script(components: dict, total_input: float, total_output: float) -> str:
    """A self-contained stdlib program that re-sums the masses and prints the verdict line.

    Hardcodes whichever components were found (no input, no network, no clock), re-derives the
    input/output totals, and prints exactly one line:
      * ``OK`` if output <= input (within tolerance),
      * ``MASS_CREATED input=<i> output=<o>`` otherwise.
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    reactants = components.get(REACTANTS_KEY, [])
    products = components.get(PRODUCTS_KEY, [])
    input_mass = components.get(INPUT_KEY, 0.0)
    recovered_mass = components.get(RECOVERED_KEY, 0.0)
    return (
        "# LITMUS recompute script for mass_balance.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"REACTANT_MASSES = {reactants!r}\n"
        f"PRODUCT_MASSES = {products!r}\n"
        f"INPUT_MASS = {input_mass!r}\n"
        f"RECOVERED_MASS = {recovered_mass!r}\n"
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
        "total_input = sum(REACTANT_MASSES) + INPUT_MASS\n"
        "total_output = sum(PRODUCT_MASSES) + RECOVERED_MASS\n"
        "if total_output <= total_input + TOLERANCE:\n"
        "    print('OK')\n"
        "else:\n"
        "    print('MASS_CREATED input=' + _fmt(total_input) + ' output=' + _fmt(total_output))\n"
    )


class MassBalance(Verifier):
    """Output mass must not exceed input mass — conservation of mass (DESIGN §6.1, T1)."""

    manifest = VerifierManifest(
        id="mass_balance.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T1,
        determinism=Determinism.DETERMINISTIC,
        consumes=["mass_balance", "mass_closure", "conservation"],
        capability_tags=["chemistry", "conservation"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["mass-closure bound"], "libs": []},
        dependencies=[],
        description=(
            "Checks conservation of mass: total product/recovered mass must not exceed total "
            "reactant/input mass (T1 physical bound). Output below input (loss) PASSes; output "
            "above input (mass created) FAILs severity A. Binds to evidence carrying "
            "extracted_values {'reactant_masses': [...], 'product_masses': [...]} and/or "
            "{'input_mass': a, 'recovered_mass': b} (grams)."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_masses(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries a reactant/product mass pair or an "
                "input/recovered mass pair; cannot check mass balance "
                "(DESIGN §3.4: abstain > guess)",
            )
        ev, total_input, total_output, components = bound

        if total_output <= total_input + TOLERANCE:
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="output mass does not exceed input mass (mass conserved)",
                reported=total_output,
                computed=total_input,
                details={"total_input": total_input, "total_output": total_output, **components},
            )

        # FAIL: mass was created. Ship executable evidence (DESIGN §3.2).
        expected = _mass_created_line(total_input, total_output)
        script = _build_recompute_script(components, total_input, total_output)
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
            message="mass created: output exceeds input — violates conservation of mass",
            discrepancy=(
                f"output {_fmt(total_output)} g exceeds input {_fmt(total_input)} g "
                f"(mass cannot be created)"
            ),
            reported=total_output,
            computed=total_input,
            evidence=packet,
            details={"total_input": total_input, "total_output": total_output, **components},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (conserved / lossy) + planted (mass-created) cases across >=2 claim types.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted across ``mass_balance`` and ``mass_closure`` so per-claim-type FPR (G6) is
        exercised, spanning both the list form and the input/recovered form. A clean case includes
        exact equality (output == input) to confirm the boundary is inclusive.
        """
        cases: list[SelfTestCase] = []

        # Clean: output <= input. (claim_type, suffix, extracted_values)
        clean_specs: list[tuple[str, str, dict]] = [
            ("mass_balance", "list_loss", {REACTANTS_KEY: [10.0, 5.0], PRODUCTS_KEY: [12.0]}),
            ("mass_balance", "list_equal", {REACTANTS_KEY: [3.0, 2.0], PRODUCTS_KEY: [5.0]}),
            ("mass_balance", "list_big_loss", {REACTANTS_KEY: [100.0], PRODUCTS_KEY: [40.0, 5.0]}),
            ("mass_closure", "pair_loss", {INPUT_KEY: 8.0, RECOVERED_KEY: 6.5}),
            ("mass_closure", "pair_equal", {INPUT_KEY: 4.0, RECOVERED_KEY: 4.0}),
            ("mass_closure", "pair_small_loss", {INPUT_KEY: 1.0, RECOVERED_KEY: 0.999}),
        ]
        for ctype, suffix, vals in clean_specs:
            assert self._spec_is_clean(vals), f"clean spec not clean: {suffix}"
            cases.append(self._case(f"clean_{ctype}_{suffix}", "clean", ctype, vals))

        # Planted: output > input (mass created). judge must FAIL these.
        planted_specs: list[tuple[str, str, dict]] = [
            ("mass_balance", "list_created", {REACTANTS_KEY: [3.0], PRODUCTS_KEY: [5.0]}),
            ("mass_balance", "list_created_multi", {REACTANTS_KEY: [2.0, 2.0], PRODUCTS_KEY: [3.0, 3.0]}),
            ("mass_balance", "list_created_big", {REACTANTS_KEY: [10.0], PRODUCTS_KEY: [25.0]}),
            ("mass_closure", "pair_created", {INPUT_KEY: 3.0, RECOVERED_KEY: 5.0}),
            ("mass_closure", "pair_created_small", {INPUT_KEY: 1.0, RECOVERED_KEY: 1.5}),
            ("mass_closure", "pair_created_tiny", {INPUT_KEY: 2.0, RECOVERED_KEY: 2.01}),
        ]
        for ctype, suffix, vals in planted_specs:
            assert not self._spec_is_clean(vals), f"planted spec is actually clean: {suffix}"
            cases.append(self._case(f"planted_{ctype}_{suffix}", "planted", ctype, vals))

        return cases

    @staticmethod
    def _spec_is_clean(vals: dict) -> bool:
        """Mirror of ``judge``'s closure test, used to assert the self_test specs are labelled
        correctly (clean really conserve; planted really create mass)."""
        total_input = 0.0
        total_output = 0.0
        if REACTANTS_KEY in vals and PRODUCTS_KEY in vals:
            total_input += sum(vals[REACTANTS_KEY])
            total_output += sum(vals[PRODUCTS_KEY])
        if INPUT_KEY in vals and RECOVERED_KEY in vals:
            total_input += vals[INPUT_KEY]
            total_output += vals[RECOVERED_KEY]
        return total_output <= total_input + TOLERANCE

    @staticmethod
    def _case(name: str, kind: str, claim_type: str, vals: dict) -> SelfTestCase:
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.TABLE,
            location=Location(section="self_test", quote="mass balance"),
            extracted_values=dict(vals),
        )
        claim = Claim(
            id=f"claim_{name}",
            text="Mass is conserved across the reaction.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T1,
            predicate="total_output_mass <= total_input_mass",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [MassBalance()]
