"""``prose_vs_table.v1`` — a real T2 verifier: does the prose agree with the paper's own table?

The T2 cross-consistency frontier (DESIGN §5, §19 WS-E): the body text asserts a number that
disagrees with the source it rests on — the paper's own table/figure. Two archetypes (DESIGN §5):

  * **"+40% in prose vs 36% in the table"** — a value the abstract/body states does not match
        the value the underlying table reports.
  * **"the majority" vs n=180/400** — a *prose word* ("the majority") whose operationalized value
        (here, a fraction > 0.5) contradicts the counts the table reports (180/400 = 0.45).

Unlike the T0 recompute core (``sum_check.v1``, ``percent_change.v1``), nothing is recomputed from
parts: the verifier compares the prose value to the source value the extractor *already bound* to
it (DESIGN §5: "LLM binds claim↔number; code judges — exact once bound"). The binding is the
model's job (WS-B); the *judgement* is deterministic code here (DESIGN §3.1).

Contract with the extractor (DESIGN §11): a claim of type ``prose_vs_table`` /
``internal_consistency`` / ``prose_number`` rests on an
:class:`~litmus.core.claim.Evidence` whose ``extracted_values`` carries::

    {"prose_value": <number>, "source_value": <number>}
    # optionally:
    {"quantity": "<what is being compared>", "rel_tol": <relative tolerance, default 0.01>}

``prose_value`` is the number as the prose states it; ``source_value`` is the number the bound
table/figure actually reports (both operationalized to comparable units by the extractor — e.g.
"the majority" → 0.5 as the prose threshold vs 0.45 as the reported fraction). ``judge`` compares:

  * either value absent on every bound evidence  -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``|prose - source| <= rel_tol*max(|source|, 1e-12)``  -> PASS.
  * otherwise                                              -> FAIL (severity B) shipping an
        EvidencePacket whose stdlib-only ``recompute_script`` reprints the discrepancy line, so a
        skeptical reader reruns it (DESIGN §3.2: no script, no flag).

Everything here is deterministic: no RNG, clock, or network in ``judge`` or the emitted script
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

# Default relative tolerance: prose and table agree within 1% of the source value
# (presentation rounding, DESIGN §6.3). "36% vs 36.1%" agrees; "40% vs 36%" does not.
DEFAULT_REL_TOL = 0.01

PROSE_KEY = "prose_value"
SOURCE_KEY = "source_value"
QUANTITY_KEY = "quantity"
REL_TOL_KEY = "rel_tol"

# Floor on the denominator so a source_value of 0 doesn't make the relative test divide by zero;
# at source==0 this degenerates to an absolute test against rel_tol*1e-12 (i.e. ~exact equality).
_DENOM_FLOOR = 1e-12


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point (``36``); genuinely fractional values print via
    ``repr`` (``0.45``). The SAME function is embedded verbatim in the recompute script so
    ``judge``'s ``expected_output`` and the script's stdout are byte-identical (DESIGN §7 G3).
    """
    if isinstance(value, bool):  # bool is an int subclass; keep it numeric-looking
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _mismatch_line(quantity: str, prose: float | int, source: float | int) -> str:
    """The single canonical discrepancy line both sides print."""
    return f"PROSE-TABLE MISMATCH quantity={quantity} prose={_fmt(prose)} table={_fmt(source)}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_bound_values(
    evidence: list[Evidence],
) -> Optional[tuple[Evidence, float, float, str, float]]:
    """First evidence carrying BOTH prose_value and source_value (both numeric).

    Returns ``(ev, prose, source, quantity, rel_tol)`` with ``quantity`` defaulting to
    ``"value"`` and ``rel_tol`` to :data:`DEFAULT_REL_TOL` when absent/invalid.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        if PROSE_KEY in vals and SOURCE_KEY in vals:
            prose, source = vals[PROSE_KEY], vals[SOURCE_KEY]
            if _is_number(prose) and _is_number(source):
                quantity = vals.get(QUANTITY_KEY)
                quantity = str(quantity) if quantity is not None else "value"
                rel_tol = vals.get(REL_TOL_KEY, DEFAULT_REL_TOL)
                if not _is_number(rel_tol) or rel_tol < 0:
                    rel_tol = DEFAULT_REL_TOL
                return ev, prose, source, quantity, rel_tol
    return None


def _disagree(prose: float | int, source: float | int, rel_tol: float) -> bool:
    """The single comparison both ``judge`` and ``_spec_is_clean`` use."""
    return abs(prose - source) > rel_tol * max(abs(source), _DENOM_FLOOR)


def _build_recompute_script(
    quantity: str, prose: float | int, source: float | int, rel_tol: float
) -> str:
    """A self-contained stdlib program that re-derives the disagreement and prints the verdict.

    Hardcodes prose/source/rel_tol (no input, no network, no clock), recomputes the relative-
    tolerance comparison, and prints exactly one line:
      * ``OK`` if they agree (within tolerance),
      * ``PROSE-TABLE MISMATCH quantity=<q> prose=<p> table=<t>`` otherwise.
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    return (
        "# LITMUS recompute script for prose_vs_table.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"QUANTITY = {quantity!r}\n"
        f"PROSE_VALUE = {prose!r}\n"
        f"SOURCE_VALUE = {source!r}\n"
        f"REL_TOL = {rel_tol!r}\n"
        "DENOM_FLOOR = 1e-12\n"
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
        "if abs(PROSE_VALUE - SOURCE_VALUE) <= REL_TOL * max(abs(SOURCE_VALUE), DENOM_FLOOR):\n"
        "    print('OK')\n"
        "else:\n"
        "    print('PROSE-TABLE MISMATCH quantity=' + QUANTITY + ' prose=' + _fmt(PROSE_VALUE)"
        " + ' table=' + _fmt(SOURCE_VALUE))\n"
    )


class ProseVsTable(Verifier):
    """Prose value agrees with the source (table/figure) value it rests on (DESIGN §5, T2)."""

    manifest = VerifierManifest(
        id="prose_vs_table.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T2,
        determinism=Determinism.DETERMINISTIC,
        consumes=["prose_vs_table", "internal_consistency", "prose_number"],
        capability_tags=["consistency", "t2"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["prose-vs-source comparison"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a number stated in prose agrees with the value the bound table/figure "
            "actually reports (T2 cross-consistency). The model binds claim->number; this code "
            "judges. Binds to evidence carrying extracted_values "
            "{'prose_value': p, 'source_value': s} and optionally {'quantity': q, 'rel_tol': r}. "
            "FAILs (severity B) when |prose - source| > rel_tol*max(|source|, 1e-12)."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_values(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries both 'prose_value' and 'source_value'; "
                "cannot compare prose against the table (DESIGN §3.4: abstain > guess)",
            )
        ev, prose, source, quantity, rel_tol = bound

        if not _disagree(prose, source, rel_tol):
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="the prose value agrees with the value the bound source reports",
                reported=prose,
                computed=source,
                details={"quantity": quantity, "rel_tol": rel_tol},
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        expected = _mismatch_line(quantity, prose, source)
        script = _build_recompute_script(quantity, prose, source, rel_tol)
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
            message="the prose value disagrees with the value the bound source reports",
            discrepancy=(
                f"prose states {_fmt(prose)} for {quantity} but the table reports {_fmt(source)}"
            ),
            reported=prose,
            computed=source,
            evidence=packet,
            details={"quantity": quantity, "rel_tol": rel_tol},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (prose agrees with table) + planted (prose contradicts table) cases.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted across claim types ``prose_vs_table`` and ``internal_consistency`` so per-claim-type
        FPR (G6) is exercised. Planted set spans both archetypes (DESIGN §5): a numeric prose/table
        mismatch ("+40% vs 36%") and a prose-word mismatch ("the majority" 0.5 vs n=180/400 0.45).
        """
        cases: list[SelfTestCase] = []

        # (claim_type, suffix, prose, source, quantity, rel_tol|None)
        clean_specs: list[tuple[str, str, float, float, str, Optional[float]]] = [
            ("prose_vs_table", "exact_pct", 36.0, 36.0, "improvement_pct", None),
            ("prose_vs_table", "rounding_ok", 36.0, 36.1, "improvement_pct", None),
            ("prose_vs_table", "count_match", 180.0, 180.0, "responder_count", None),
            ("internal_consistency", "fraction_match", 0.45, 0.45, "responder_fraction", None),
            ("internal_consistency", "abstract_body", 12.3, 12.3, "mean_score", None),
            # "a minority" stated as <0.5, table reports 0.45 -> agrees within a loose 5% tol.
            ("internal_consistency", "minority_ok", 0.45, 0.46, "minority_fraction", 0.05),
        ]
        for ctype, suffix, prose, source, quantity, rel_tol in clean_specs:
            tol = DEFAULT_REL_TOL if rel_tol is None else rel_tol
            assert not _disagree(prose, source, tol), f"clean spec not clean: {suffix}"
            cases.append(
                self._case(f"clean_{ctype}_{suffix}", "clean", ctype, prose, source, quantity, rel_tol)
            )

        planted_specs: list[tuple[str, str, float, float, str, Optional[float]]] = [
            # The flagship archetype: prose claims +40% but the table reports 36%.
            ("prose_vs_table", "forty_vs_thirtysix", 40.0, 36.0, "improvement_pct", None),
            ("prose_vs_table", "overstated_count", 250.0, 180.0, "responder_count", None),
            ("prose_vs_table", "wrong_mean", 12.3, 21.3, "mean_score", None),
            # "the majority" (operationalized as the 0.5 threshold) vs n=180/400 = 0.45.
            ("internal_consistency", "majority_vs_180of400", 0.5, 0.45, "responder_fraction", None),
            ("internal_consistency", "abstract_inflation", 0.62, 0.45, "success_fraction", None),
            ("internal_consistency", "doubled", 72.0, 36.0, "improvement_pct", None),
        ]
        for ctype, suffix, prose, source, quantity, rel_tol in planted_specs:
            tol = DEFAULT_REL_TOL if rel_tol is None else rel_tol
            assert _disagree(prose, source, tol), f"planted spec is actually clean: {suffix}"
            cases.append(
                self._case(f"planted_{ctype}_{suffix}", "planted", ctype, prose, source, quantity, rel_tol)
            )

        return cases

    @staticmethod
    def _case(
        name: str,
        kind: str,
        claim_type: str,
        prose: float,
        source: float,
        quantity: str,
        rel_tol: Optional[float],
    ) -> SelfTestCase:
        vals: dict[str, Any] = {PROSE_KEY: prose, SOURCE_KEY: source, QUANTITY_KEY: quantity}
        if rel_tol is not None:
            vals[REL_TOL_KEY] = rel_tol
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.TABLE,
            location=Location(section="self_test", quote=f"{quantity}: prose {prose}, table {source}"),
            extracted_values=vals,
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The prose states {prose} for {quantity}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T2,
            predicate="prose_value == source_value (within rel_tol)",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [ProseVsTable()]
