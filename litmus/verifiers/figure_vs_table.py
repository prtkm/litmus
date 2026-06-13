"""``figure_vs_table.v1`` — a real T2 verifier: does a plotted figure value match the table?

The other half of the T2 cross-consistency frontier (DESIGN §5, §19 WS-E). A value read off a
figure (a plotted bar height, a point on a curve, an error-bar magnitude) is compared against the
value the paper's own table reports for the same quantity. This is the deterministic *judge* half
of the "read numbers off the figure -> T0/T2 deterministic check" collapse (DESIGN §5): the vision
reasoner (``litmus.vision.figure_reader``) reads the figure value; THIS code decides whether it
agrees with the table. No model in the loop here (DESIGN §3.1).

Contract with the extractor / figure reader (DESIGN §11): a claim of type ``figure_vs_table``
rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values`` carries::

    {"figure_value": <number>, "table_value": <number>}
    # optionally:
    {"quantity": "<what is being compared>", "rel_tol": <relative tolerance, default 0.02>}

``figure_value`` is the value read from the plot; ``table_value`` is the value the table reports
for the same series/condition. The default tolerance is looser than ``prose_vs_table.v1`` (2% vs
1%) because reading a value off a rendered axis is inherently less precise than transcribing a
table cell (DESIGN §5, §6.3). ``judge`` compares:

  * either value absent on every bound evidence  -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``|figure - table| <= rel_tol*max(|table|, 1e-12)``  -> PASS.
  * otherwise                                            -> FAIL (severity B) shipping an
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

# Default relative tolerance: a value read off a figure is allowed 2% slack against the table
# (axis-reading imprecision, DESIGN §5, §6.3). Looser than prose_vs_table's 1%.
DEFAULT_REL_TOL = 0.02

FIGURE_KEY = "figure_value"
TABLE_KEY = "table_value"
QUANTITY_KEY = "quantity"
REL_TOL_KEY = "rel_tol"

# Floor on the denominator so a table_value of 0 doesn't make the relative test divide by zero.
_DENOM_FLOOR = 1e-12


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point (``42``); genuinely fractional values print via
    ``repr`` (``0.42``). The SAME function is embedded verbatim in the recompute script so
    ``judge``'s ``expected_output`` and the script's stdout are byte-identical (DESIGN §7 G3).
    """
    if isinstance(value, bool):  # bool is an int subclass; keep it numeric-looking
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _mismatch_line(quantity: str, figure: float | int, table: float | int) -> str:
    """The single canonical discrepancy line both sides print."""
    return f"FIGURE-TABLE MISMATCH quantity={quantity} figure={_fmt(figure)} table={_fmt(table)}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_bound_values(
    evidence: list[Evidence],
) -> Optional[tuple[Evidence, float, float, str, float]]:
    """First evidence carrying BOTH figure_value and table_value (both numeric).

    Returns ``(ev, figure, table, quantity, rel_tol)`` with ``quantity`` defaulting to ``"value"``
    and ``rel_tol`` to :data:`DEFAULT_REL_TOL` when absent/invalid.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        if FIGURE_KEY in vals and TABLE_KEY in vals:
            figure, table = vals[FIGURE_KEY], vals[TABLE_KEY]
            if _is_number(figure) and _is_number(table):
                quantity = vals.get(QUANTITY_KEY)
                quantity = str(quantity) if quantity is not None else "value"
                rel_tol = vals.get(REL_TOL_KEY, DEFAULT_REL_TOL)
                if not _is_number(rel_tol) or rel_tol < 0:
                    rel_tol = DEFAULT_REL_TOL
                return ev, figure, table, quantity, rel_tol
    return None


def _disagree(figure: float | int, table: float | int, rel_tol: float) -> bool:
    """The single comparison both ``judge`` and ``_spec_is_clean`` use."""
    return abs(figure - table) > rel_tol * max(abs(table), _DENOM_FLOOR)


def _build_recompute_script(
    quantity: str, figure: float | int, table: float | int, rel_tol: float
) -> str:
    """A self-contained stdlib program that re-derives the disagreement and prints the verdict.

    Hardcodes figure/table/rel_tol (no input, no network, no clock), recomputes the relative-
    tolerance comparison, and prints exactly one line:
      * ``OK`` if they agree (within tolerance),
      * ``FIGURE-TABLE MISMATCH quantity=<q> figure=<f> table=<t>`` otherwise.
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    return (
        "# LITMUS recompute script for figure_vs_table.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"QUANTITY = {quantity!r}\n"
        f"FIGURE_VALUE = {figure!r}\n"
        f"TABLE_VALUE = {table!r}\n"
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
        "if abs(FIGURE_VALUE - TABLE_VALUE) <= REL_TOL * max(abs(TABLE_VALUE), DENOM_FLOOR):\n"
        "    print('OK')\n"
        "else:\n"
        "    print('FIGURE-TABLE MISMATCH quantity=' + QUANTITY + ' figure=' + _fmt(FIGURE_VALUE)"
        " + ' table=' + _fmt(TABLE_VALUE))\n"
    )


class FigureVsTable(Verifier):
    """A plotted figure value agrees with the table value for the same quantity (DESIGN §5, T2)."""

    manifest = VerifierManifest(
        id="figure_vs_table.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T2,
        determinism=Determinism.DETERMINISTIC,
        consumes=["figure_vs_table"],
        capability_tags=["consistency", "t2", "figure"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["figure-vs-table comparison"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a value read off a figure agrees with the value the table reports for the "
            "same quantity (T2 cross-consistency). The vision reader extracts the figure value; "
            "this code judges. Binds to evidence carrying extracted_values "
            "{'figure_value': f, 'table_value': t} and optionally {'quantity': q, 'rel_tol': r}. "
            "FAILs (severity B) when |figure - table| > rel_tol*max(|table|, 1e-12)."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_values(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries both 'figure_value' and 'table_value'; "
                "cannot compare the figure against the table (DESIGN §3.4: abstain > guess)",
            )
        ev, figure, table, quantity, rel_tol = bound

        if not _disagree(figure, table, rel_tol):
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="the figure value agrees with the value the table reports",
                reported=figure,
                computed=table,
                details={"quantity": quantity, "rel_tol": rel_tol},
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        expected = _mismatch_line(quantity, figure, table)
        script = _build_recompute_script(quantity, figure, table, rel_tol)
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
            message="the figure value disagrees with the value the table reports",
            discrepancy=(
                f"figure shows {_fmt(figure)} for {quantity} but the table reports {_fmt(table)}"
            ),
            reported=figure,
            computed=table,
            evidence=packet,
            details={"quantity": quantity, "rel_tol": rel_tol},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (figure agrees with table) + planted (figure contradicts table) cases.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted across claim types ``figure_vs_table`` and ``figure_bar`` so per-claim-type FPR
        (G6) is exercised. Clean cases include small within-2%-tolerance reading slack; planted
        cases are unambiguous over-/under-reads (e.g. a bar plotted at 60 the table says is 50).
        """
        cases: list[SelfTestCase] = []

        # (claim_type, suffix, figure, table, quantity, rel_tol|None)
        clean_specs: list[tuple[str, str, float, float, str, Optional[float]]] = [
            ("figure_vs_table", "exact_bar", 50.0, 50.0, "bar_height", None),
            # 1% reading slack on a bar the table says is 50 -> within the 2% default.
            ("figure_vs_table", "slack_within_tol", 50.5, 50.0, "bar_height", None),
            ("figure_vs_table", "point_match", 0.82, 0.82, "curve_point", None),
            ("figure_bar", "errorbar_match", 3.0, 3.0, "error_bar_sd", None),
            ("figure_bar", "mean_match", 142.0, 142.0, "group_mean", None),
            # A deliberately loose 5% tolerance the reading comfortably satisfies.
            ("figure_bar", "loose_tol_ok", 98.0, 100.0, "yield_pct", 0.05),
        ]
        for ctype, suffix, figure, table, quantity, rel_tol in clean_specs:
            tol = DEFAULT_REL_TOL if rel_tol is None else rel_tol
            assert not _disagree(figure, table, tol), f"clean spec not clean: {suffix}"
            cases.append(
                self._case(f"clean_{ctype}_{suffix}", "clean", ctype, figure, table, quantity, rel_tol)
            )

        planted_specs: list[tuple[str, str, float, float, str, Optional[float]]] = [
            # A bar plotted at 60 against a table that says 50 (the flagship figure-vs-table wedge).
            ("figure_vs_table", "bar_60_vs_50", 60.0, 50.0, "bar_height", None),
            ("figure_vs_table", "point_off", 0.82, 0.62, "curve_point", None),
            ("figure_vs_table", "underplotted", 120.0, 142.0, "group_mean", None),
            ("figure_bar", "errorbar_shrunk", 1.0, 3.0, "error_bar_sd", None),
            ("figure_bar", "axis_truncation", 88.0, 100.0, "yield_pct", None),
            ("figure_bar", "doubled_bar", 100.0, 50.0, "bar_height", None),
        ]
        for ctype, suffix, figure, table, quantity, rel_tol in planted_specs:
            tol = DEFAULT_REL_TOL if rel_tol is None else rel_tol
            assert _disagree(figure, table, tol), f"planted spec is actually clean: {suffix}"
            cases.append(
                self._case(f"planted_{ctype}_{suffix}", "planted", ctype, figure, table, quantity, rel_tol)
            )

        return cases

    @staticmethod
    def _case(
        name: str,
        kind: str,
        claim_type: str,
        figure: float,
        table: float,
        quantity: str,
        rel_tol: Optional[float],
    ) -> SelfTestCase:
        vals: dict[str, Any] = {FIGURE_KEY: figure, TABLE_KEY: table, QUANTITY_KEY: quantity}
        if rel_tol is not None:
            vals[REL_TOL_KEY] = rel_tol
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.FIGURE,
            location=Location(section="self_test", quote=f"{quantity}: figure {figure}, table {table}"),
            extracted_values=vals,
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The figure shows {figure} for {quantity}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T2,
            predicate="figure_value == table_value (within rel_tol)",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [FigureVsTable()]
