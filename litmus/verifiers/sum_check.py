"""``sum_check.v1`` — a real T0 verifier: does a reported total equal the sum of its parts?

This is the reference first-party verifier (DESIGN §6.1 class A, §19 WS-A/WS-D). It is the
simplest honest member of the T0 recompute core: pure arithmetic on the paper's own
transcribed numbers, no external knowledge, no model in the loop (DESIGN §3.1).

Contract with the extractor (DESIGN §11): a claim of type ``table_total`` / ``sum_claim``
rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values`` carries:

    {"parts": [<number>, ...], "reported_total": <number>}

``judge`` recomputes ``sum(parts)`` and compares to ``reported_total``:

  * both keys absent on every bound evidence  -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * computed == reported (float tolerance)    -> PASS.
  * computed != reported                       -> FAIL (severity B) shipping an EvidencePacket
        whose ``recompute_script`` is a self-contained stdlib program that reprints the
        discrepancy line, so a skeptical reader can rerun it (DESIGN §3.2: no script, no flag).

Everything here is deterministic: no RNG, clock, or network in ``judge`` or in the emitted
script (DESIGN §7 G4). The calibration kernel verifies that empirically.
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

# Floats within this absolute distance are treated as equal (presentation rounding, DESIGN §6.3).
TOLERANCE = 1e-9

PARTS_KEY = "parts"
TOTAL_KEY = "reported_total"


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integers (and integral floats like ``97.0``) print without a decimal point -> ``97``;
    genuinely fractional values print via ``repr`` -> ``3.5``. The SAME function is embedded
    verbatim in the recompute script, so ``judge``'s ``expected_output`` and the script's
    stdout are byte-identical (DESIGN §7 G3).
    """
    if isinstance(value, bool):  # bool is an int subclass; keep it numeric-looking
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _mismatch_line(reported: float | int, computed: float | int) -> str:
    """The single canonical discrepancy line both sides print."""
    return f"MISMATCH reported={_fmt(reported)} computed={_fmt(computed)}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_bound_values(evidence: list[Evidence]) -> Optional[tuple[Evidence, list, Any]]:
    """First evidence carrying BOTH parts and reported_total. Returns (ev, parts, total)."""
    for ev in evidence:
        vals = ev.extracted_values or {}
        if PARTS_KEY in vals and TOTAL_KEY in vals:
            parts = vals[PARTS_KEY]
            total = vals[TOTAL_KEY]
            if isinstance(parts, (list, tuple)) and _is_number(total):
                if all(_is_number(p) for p in parts):
                    return ev, list(parts), total
    return None


def _build_recompute_script(parts: list, reported_total: float | int) -> str:
    """A self-contained stdlib program that recomputes the sum and prints the verdict line.

    Hardcodes the parts + reported_total (no input, no network, no clock), recomputes
    ``sum(parts)``, and prints exactly one line:
      * ``OK`` if they match (within tolerance),
      * ``MISMATCH reported=<r> computed=<c>`` otherwise.
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    return (
        "# LITMUS recompute script for sum_check.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"PARTS = {parts!r}\n"
        f"REPORTED_TOTAL = {reported_total!r}\n"
        "TOLERANCE = 1e-9\n"
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
        "computed = sum(PARTS)\n"
        "if abs(computed - REPORTED_TOTAL) <= TOLERANCE:\n"
        "    print('OK')\n"
        "else:\n"
        "    print('MISMATCH reported=' + _fmt(REPORTED_TOTAL) + ' computed=' + _fmt(computed))\n"
    )


class SumCheck(Verifier):
    """Reported-total == sum-of-parts (DESIGN §6.1, T0)."""

    manifest = VerifierManifest(
        id="sum_check.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["table_total", "sum_claim"],
        capability_tags=["arithmetic", "table", "total"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["sum recompute"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a reported total equals the sum of its parts (T0 arithmetic). "
            "Binds to evidence carrying extracted_values {'parts': [...], 'reported_total': n}."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_values(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries both 'parts' and 'reported_total'; "
                "cannot recompute a total (DESIGN §3.4: abstain > guess)",
            )
        ev, parts, reported_total = bound
        computed = sum(parts)

        if abs(computed - reported_total) <= TOLERANCE:
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="reported total equals the sum of its parts",
                reported=reported_total,
                computed=computed,
                details={"parts": parts, "n_parts": len(parts)},
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        expected = _mismatch_line(reported_total, computed)
        script = _build_recompute_script(parts, reported_total)
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
            message="reported total does not equal the sum of its parts",
            discrepancy=(
                f"reported {_fmt(reported_total)} but parts sum to {_fmt(computed)}"
            ),
            reported=reported_total,
            computed=computed,
            evidence=packet,
            details={"parts": parts, "n_parts": len(parts)},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (correct totals) + planted (wrong totals) cases across >=2 claim types.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). At least 4 clean
        and 4 planted, spanning ``table_total`` and ``sum_claim`` so per-claim-type FPR (G6)
        is actually exercised. Includes int and float cases.
        """
        cases: list[SelfTestCase] = []

        # (claim_type, name_suffix, parts, reported_total)
        clean_specs: list[tuple[str, str, list, float | int]] = [
            ("table_total", "ints_small", [1, 2, 3, 4], 10),
            ("table_total", "ints_yields", [12, 25, 63], 100),
            ("sum_claim", "ints_mixed", [40, 35, 25], 100),
            ("sum_claim", "floats_exact", [0.1, 0.2, 0.7], 1.0),
            ("table_total", "floats_decimals", [2.5, 2.5, 5.0], 10.0),
            ("sum_claim", "single_part", [42], 42),
        ]
        for ctype, suffix, parts, total in clean_specs:
            assert abs(sum(parts) - total) <= TOLERANCE, f"clean spec not clean: {suffix}"
            cases.append(self._case(f"clean_{ctype}_{suffix}", "clean", ctype, parts, total))

        planted_specs: list[tuple[str, str, list, float | int]] = [
            ("table_total", "off_by_three", [12, 25, 60], 100),   # sums to 97
            ("table_total", "transposed", [10, 20, 30], 70),      # sums to 60
            ("sum_claim", "dropped_row", [40, 35], 100),          # sums to 75
            ("sum_claim", "inflated", [1, 1, 1], 5),              # sums to 3
            ("table_total", "float_drift", [2.5, 2.5, 5.0], 11.0),  # sums to 10.0
            ("sum_claim", "negative_typo", [50, -10, 40], 100),   # sums to 80
        ]
        for ctype, suffix, parts, total in planted_specs:
            assert abs(sum(parts) - total) > TOLERANCE, f"planted spec is actually clean: {suffix}"
            cases.append(self._case(f"planted_{ctype}_{suffix}", "planted", ctype, parts, total))

        return cases

    @staticmethod
    def _case(name: str, kind: str, claim_type: str, parts: list, total: float | int) -> SelfTestCase:
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.TABLE,
            location=Location(section="self_test", quote=f"total {total}"),
            extracted_values={PARTS_KEY: list(parts), TOTAL_KEY: total},
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The reported total is {total}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T0,
            predicate="reported_total == sum(parts)",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [SumCheck()]
