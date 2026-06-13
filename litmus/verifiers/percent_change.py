"""``percent_change.v1`` — a real T0 verifier: does a reported percent change match old→new?

The flagship "+40% claimed but 50→68 is +36%" archetype (DESIGN §6.1 class A, §19 WS-D). Pure
arithmetic on the paper's own transcribed numbers — no external knowledge, no model in the loop
(DESIGN §3.1). A close cousin of ``sum_check.v1`` in the T0 recompute core.

Contract with the extractor (DESIGN §11): a claim of type ``percent_change`` / ``percent_increase``
/ ``percent_decrease`` rests on an :class:`~litmus.core.claim.Evidence` whose ``extracted_values``
carries::

    {"old_value": <number>, "new_value": <number>, "reported_pct_change": <signed number>}

``reported_pct_change`` is signed: +N for an increase, -N for a decrease (an extractor that reads
"a 40% increase" emits ``+40``; "a 12% drop" emits ``-12``). ``judge`` recomputes
``100 * (new - old) / old`` and compares:

  * any of the three keys absent on every bound evidence  -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``old_value == 0``                                     -> ABSTAIN (percent change undefined; a
        division by zero is not a finding — it is a non-binding).
  * computed == reported (rounding tolerance)             -> PASS.
  * computed != reported                                   -> FAIL (severity B) shipping an
        EvidencePacket whose stdlib-only ``recompute_script`` reprints the discrepancy line, so a
        skeptical reader can rerun it (DESIGN §3.2: no script, no flag).

**Rounding-aware tolerance (DESIGN §6.3, §3.4: prefer PASS over a trivial flag).** A reported
percent is printed to finite precision: "a 36% increase" means the true change rounds to 36, i.e.
lies in ``[35.5, 36.5]``. So ``50 -> 68.2`` (a true +36.4%) reported as "36%" is *correct* — the
author rounded. The tolerance is half a unit in the reported percent's last printed place (``0.5``
for an integer percent, ``0.05`` for one decimal), plus a small allowance for the inputs' own
rounding. The flagship "+40% claimed but 50->68 is +36%" gap (4 points) blows past it and still
FAILs.

Everything here is deterministic: no RNG, clock, or network in ``judge`` or in the emitted script
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

# Absolute floor on the percentage-point tolerance (float noise). The real tolerance is
# rounding-aware (see ``_tolerance``): a reported "+36%" must match a computed 36.4; "+40%" must
# not match a computed 36.0 (DESIGN §6.3).
TOLERANCE_FLOOR = 0.05

OLD_KEY = "old_value"
NEW_KEY = "new_value"
REPORTED_KEY = "reported_pct_change"


def _decimals_of(value: float | int) -> int:
    """How many decimal places ``value`` was printed to (``36`` -> 0, ``36.4`` -> 1)."""
    if isinstance(value, bool) or isinstance(value, int):
        return 0
    if isinstance(value, float):
        if value.is_integer():
            return 0
        s = repr(value)
        if "e" in s or "E" in s:
            try:
                mant, exp = s.lower().split("e")
                frac = len(mant.split(".")[1]) if "." in mant else 0
                return max(0, frac - int(exp))
            except Exception:
                return 0
        if "." in s:
            return len(s.split(".")[1])
    return 0


def _tolerance(old_value: float, new_value: float, reported: float) -> float:
    """Rounding-aware percentage-point tolerance (DESIGN §6.3).

    Dominant term: the reported percent is printed to some decimals, so the true change rounds
    into ``reported ± 0.5 * 10^-D_reported`` (``0.5`` for an integer percent). Second-order: the
    inputs are themselves rounded; propagating half-a-ULP of ``new`` and ``old`` through
    ``pct = 100*(new-old)/old`` adds ``100/|old| * half_ulp(new) + 100*|new|/old^2 * half_ulp(old)``.
    The sum (floored at ``TOLERANCE_FLOOR``) is the band within which a reported percent is a
    correct rounding of the recomputed one. A genuine over-claim exceeds it and still FAILs.
    """
    # The reported percent is a rounded CLAIM, so it carries half a ULP of its printed
    # precision. The SOURCE values do not: an integer is an exact count, and an integral
    # ratio baseline (old=1.0) is exact — only a genuinely fractional display carries
    # half-ULP uncertainty. (Mirrors sum_check's integer rule; without it a tiny baseline
    # like old=1.0 — treated as ±0.5 — balloons the tolerance and swallows a real gap.)
    def _src_half(v: float) -> float:
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
            return 0.0
        return 0.5 * 10 ** (-_decimals_of(v))

    rep_half = 0.5 * 10 ** (-_decimals_of(reported))
    new_half = _src_half(new_value)
    old_half = _src_half(old_value)
    input_term = 0.0
    if old_value != 0:
        input_term = 100.0 / abs(old_value) * new_half + 100.0 * abs(new_value) / (old_value * old_value) * old_half
    return max(TOLERANCE_FLOOR, rep_half + input_term)


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point (``36``); genuinely fractional values print via
    ``repr`` (``36.36363636363637``). The SAME function is embedded verbatim in the recompute
    script so ``judge``'s ``expected_output`` and the script's stdout are byte-identical
    (DESIGN §7 G3).
    """
    if isinstance(value, bool):  # bool is an int subclass; keep it numeric-looking
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _mismatch_line(reported: float | int, computed: float) -> str:
    """The single canonical discrepancy line both sides print."""
    return f"MISMATCH reported={_fmt(reported)} computed={_fmt(computed)}"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_bound_values(
    evidence: list[Evidence],
) -> Optional[tuple[Evidence, float, float, float]]:
    """First evidence carrying old_value, new_value, AND reported_pct_change (all numeric).

    Returns (ev, old, new, reported).
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        if OLD_KEY in vals and NEW_KEY in vals and REPORTED_KEY in vals:
            old, new, rep = vals[OLD_KEY], vals[NEW_KEY], vals[REPORTED_KEY]
            if _is_number(old) and _is_number(new) and _is_number(rep):
                return ev, old, new, rep
    return None


def _build_recompute_script(
    old_value: float, new_value: float, reported: float, tolerance: float
) -> str:
    """A self-contained stdlib program that recomputes the percent change and prints the verdict.

    Hardcodes old/new/reported + the rounding-aware tolerance (no input, no network, no clock),
    recomputes ``100*(new-old)/old``, and prints exactly one line:
      * ``OK`` if it matches the reported change (within tolerance),
      * ``MISMATCH reported=<r> computed=<c>`` otherwise.
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    return (
        "# LITMUS recompute script for percent_change.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"OLD_VALUE = {old_value!r}\n"
        f"NEW_VALUE = {new_value!r}\n"
        f"REPORTED_PCT_CHANGE = {reported!r}\n"
        f"TOLERANCE = {tolerance!r}\n"
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
        "computed = 100.0 * (NEW_VALUE - OLD_VALUE) / OLD_VALUE\n"
        "if abs(computed - REPORTED_PCT_CHANGE) <= TOLERANCE:\n"
        "    print('OK')\n"
        "else:\n"
        "    print('MISMATCH reported=' + _fmt(REPORTED_PCT_CHANGE) + ' computed=' + _fmt(computed))\n"
    )


class PercentChange(Verifier):
    """Reported percent change == 100*(new-old)/old (DESIGN §6.1, T0)."""

    manifest = VerifierManifest(
        id="percent_change.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["percent_change", "delta_pct", "percent_increase", "percent_decrease"],
        capability_tags=["arithmetic", "percent"],
        fpr_ceiling=0.05,
        authors=["LITMUS"],
        provenance="first-party",
        built_vs_borrowed={"ours": ["percent-change recompute"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a reported percent change equals 100*(new-old)/old (T0 arithmetic). "
            "Binds to evidence carrying extracted_values "
            "{'old_value': a, 'new_value': b, 'reported_pct_change': p}. Abstains when old_value==0."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_values(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries old_value, new_value, and reported_pct_change; "
                "cannot recompute a percent change (DESIGN §3.4: abstain > guess)",
            )
        ev, old_value, new_value, reported = bound

        if old_value == 0:
            # Percent change is undefined for a zero baseline; do not invent a verdict.
            return self.abstain(
                claim,
                "old_value == 0: percent change is undefined (division by zero); abstaining "
                "rather than flagging (DESIGN §3.4)",
            )

        computed = 100.0 * (new_value - old_value) / old_value
        tol = _tolerance(old_value, new_value, reported)

        if abs(computed - reported) <= tol:
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="reported percent change matches the recomputed old->new change",
                reported=reported,
                computed=computed,
                details={"old_value": old_value, "new_value": new_value},
            )

        # FAIL: ship executable evidence (DESIGN §3.2).
        expected = _mismatch_line(reported, computed)
        script = _build_recompute_script(old_value, new_value, reported, tol)
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
            message="reported percent change does not match the recomputed old->new change",
            discrepancy=(
                f"reported {_fmt(reported)}% but {_fmt(old_value)}->{_fmt(new_value)} "
                f"is {_fmt(computed)}%"
            ),
            reported=reported,
            computed=computed,
            evidence=packet,
            details={"old_value": old_value, "new_value": new_value},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (correct deltas) + planted (wrong deltas) cases across >=2 claim types.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted, spanning ``percent_increase`` and ``percent_decrease`` so per-claim-type FPR (G6)
        is exercised on both signs. Includes integer and fractional computed values.
        """
        cases: list[SelfTestCase] = []

        # (claim_type, name_suffix, old, new, reported_pct)
        clean_specs: list[tuple[str, str, float, float, float]] = [
            ("percent_increase", "doubling", 50.0, 100.0, 100.0),     # +100%
            ("percent_increase", "half_up", 200.0, 300.0, 50.0),      # +50%
            ("percent_increase", "small_rise", 80.0, 88.0, 10.0),     # +10%
            ("percent_decrease", "halving", 100.0, 50.0, -50.0),      # -50%
            ("percent_decrease", "quarter_drop", 200.0, 150.0, -25.0),  # -25%
            ("percent_decrease", "tenth_drop", 50.0, 45.0, -10.0),    # -10%
        ]
        for ctype, suffix, old, new, rep in clean_specs:
            assert abs(100.0 * (new - old) / old - rep) <= _tolerance(old, new, rep), (
                f"clean spec not clean: {suffix}"
            )
            cases.append(
                self._case(f"clean_{ctype}_{suffix}", "clean", ctype, old, new, rep)
            )

        planted_specs: list[tuple[str, str, float, float, float]] = [
            # The flagship archetype: 50->68 is +36%, not the claimed +40%.
            ("percent_increase", "flagship_40_vs_36", 50.0, 68.0, 40.0),
            ("percent_increase", "overstated", 100.0, 130.0, 50.0),    # really +30%
            ("percent_increase", "wrong_sign", 100.0, 80.0, 20.0),     # really -20%
            ("percent_decrease", "understated", 100.0, 70.0, -10.0),   # really -30%
            ("percent_decrease", "overstated_drop", 200.0, 180.0, -25.0),  # really -10%
            ("percent_decrease", "flipped", 80.0, 100.0, -25.0),       # really +25%
        ]
        for ctype, suffix, old, new, rep in planted_specs:
            assert abs(100.0 * (new - old) / old - rep) > _tolerance(old, new, rep), (
                f"planted spec is actually clean: {suffix}"
            )
            cases.append(
                self._case(f"planted_{ctype}_{suffix}", "planted", ctype, old, new, rep)
            )

        return cases

    @staticmethod
    def _case(
        name: str, kind: str, claim_type: str, old: float, new: float, reported: float
    ) -> SelfTestCase:
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.NUMBER,
            location=Location(section="self_test", quote=f"{old} to {new}, {reported}%"),
            extracted_values={OLD_KEY: old, NEW_KEY: new, REPORTED_KEY: reported},
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"A change from {old} to {new} is {reported}%.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T0,
            predicate="reported_pct_change == 100*(new-old)/old",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery (DESIGN §9, §19 WS-D).
VERIFIERS = [PercentChange()]
