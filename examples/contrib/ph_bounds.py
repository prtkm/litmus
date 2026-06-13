"""``ph_bounds.v1`` — a contributed T1 verifier: a reported aqueous pH must lie in [0, 14].

Authored as an *external contribution* (DESIGN §9: "a chemist, economist, biologist, or
astronomer adds a verifier for their field through one contract, held to the same bar as
everything else"). It is held to the exact same calibration gate (DESIGN §7) as every first-party
verifier — run ``litmus verifier test examples/contrib/ph_bounds.py`` to see its scorecard.

The check is a fixed external-knowledge bound (DESIGN §5, T1): on the conventional aqueous scale at
~25 °C, pH ranges 0–14. A reported value outside [0, 14] is not an arithmetic slip in the paper's
own numbers (that would be T0); it contradicts a constant of the domain, so this is T1.

Contract with the extractor (DESIGN §11): a claim of type ``ph`` / ``reported_ph`` rests on an
:class:`~litmus.core.claim.Evidence` whose ``extracted_values`` carries::

    {"reported_ph": <number>}

``judge``:

  * no bound evidence carries ``reported_ph``     -> ABSTAIN (DESIGN §3.4: abstain > guess).
  * ``reported_ph`` is not a real number          -> ABSTAIN (nothing to bound a verdict to).
  * 0 <= reported_ph <= 14                          -> PASS.
  * reported_ph < 0 or reported_ph > 14            -> FAIL (severity A — an impossible value on the
        standard scale) shipping an EvidencePacket whose stdlib-only ``recompute_script`` reprints
        the out-of-range line, so a skeptical reader can rerun it (DESIGN §3.2: no script, no flag).

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

# The conventional aqueous pH scale (DESIGN §5, T1: fixed external knowledge).
PH_MIN = 0.0
PH_MAX = 14.0

REPORTED_PH_KEY = "reported_ph"


def _fmt(value: float | int) -> str:
    """Render a number the way both the live verdict and the emitted script must agree on.

    Integral values print without a decimal point (``15`` not ``15.0``); genuinely fractional
    values print via ``repr``. The SAME function is embedded verbatim in the recompute script so
    ``judge``'s ``expected_output`` and the script's stdout are byte-identical (DESIGN §7 G3).
    """
    if isinstance(value, bool):  # bool is an int subclass; keep it numeric-looking
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def _out_of_range_line(reported: float | int) -> str:
    """The single canonical violation line both sides print."""
    return f"OUT_OF_RANGE reported_ph={_fmt(reported)} allowed=[0,14]"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_bound_value(evidence: list[Evidence]) -> Optional[tuple[Evidence, float | int]]:
    """First bound evidence carrying a numeric ``reported_ph``. Returns ``(ev, reported_ph)``."""
    for ev in evidence:
        vals = ev.extracted_values or {}
        if REPORTED_PH_KEY in vals:
            rep = vals[REPORTED_PH_KEY]
            if _is_number(rep):
                return ev, rep
    return None


def _build_recompute_script(reported_ph: float | int) -> str:
    """A self-contained stdlib program that re-checks the bound and prints the verdict line.

    Hardcodes ``reported_ph`` (no input, no network, no clock), re-tests ``0 <= ph <= 14``, and
    prints exactly one line:
      * ``OK`` if it is in range,
      * ``OUT_OF_RANGE reported_ph=<r> allowed=[0,14]`` otherwise.
    The ``_fmt`` body is identical to the module-level one so outputs agree byte-for-byte.
    """
    return (
        "# LITMUS recompute script for ph_bounds.v1 (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"REPORTED_PH = {reported_ph!r}\n"
        "PH_MIN = 0.0\n"
        "PH_MAX = 14.0\n"
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
        "if PH_MIN <= REPORTED_PH <= PH_MAX:\n"
        "    print('OK')\n"
        "else:\n"
        "    print('OUT_OF_RANGE reported_ph=' + _fmt(REPORTED_PH) + ' allowed=[0,14]')\n"
    )


class PhBounds(Verifier):
    """A reported aqueous pH must lie within [0, 14] (DESIGN §6.1 class A contract, T1)."""

    manifest = VerifierManifest(
        id="ph_bounds.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T1,
        determinism=Determinism.LOOKUP,
        consumes=["ph", "reported_ph"],
        capability_tags=["chemistry", "ph", "range", "bounds"],
        fpr_ceiling=0.05,
        authors=["A. Contributor"],
        license="Apache-2.0",
        provenance="contributed",
        built_vs_borrowed={"ours": ["pH range check"], "libs": []},
        dependencies=[],
        description=(
            "Checks that a reported aqueous pH lies within the conventional [0, 14] scale (T1, "
            "fixed external knowledge). Binds to evidence carrying extracted_values "
            "{'reported_ph': n}; FAILs (severity A) on an impossible value."
        ),
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_value(evidence)
        if bound is None:
            return self.abstain(
                claim,
                "no bound evidence carries a numeric 'reported_ph'; cannot check the pH bound "
                "(DESIGN §3.4: abstain > guess)",
            )
        ev, reported_ph = bound

        if PH_MIN <= reported_ph <= PH_MAX:
            return self.make_finding(
                claim=claim,
                status=Status.PASS,
                message="reported pH lies within the conventional [0, 14] aqueous scale",
                reported=reported_ph,
                computed=None,
                details={"ph_min": PH_MIN, "ph_max": PH_MAX},
            )

        # FAIL: an impossible value on the standard scale -> severity A, ship executable evidence.
        expected = _out_of_range_line(reported_ph)
        script = _build_recompute_script(reported_ph)
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
            message="reported pH lies outside the conventional [0, 14] aqueous scale",
            discrepancy=(
                f"reported pH {_fmt(reported_ph)} is outside the allowed range [0, 14]"
            ),
            reported=reported_ph,
            computed=None,
            evidence=packet,
            details={"ph_min": PH_MIN, "ph_max": PH_MAX},
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean (in-range) + planted (out-of-range) cases across the two claim types.

        Fixed, hand-written numbers — deterministic, no RNG (DESIGN §7 G4). >=6 clean and >=6
        planted, spanning ``ph`` and ``reported_ph`` so per-claim-type FPR (G6) is exercised.
        Includes the inclusive boundaries (0 and 14, which must PASS) and both under- and
        over-range planted values.
        """
        cases: list[SelfTestCase] = []

        # (claim_type, name_suffix, reported_ph) — every one is in [0, 14].
        clean_specs: list[tuple[str, str, float | int]] = [
            ("ph", "neutral", 7.0),
            ("ph", "lower_bound", 0),          # inclusive boundary -> PASS
            ("ph", "acidic", 2.5),
            ("reported_ph", "upper_bound", 14),  # inclusive boundary -> PASS
            ("reported_ph", "basic", 11.3),
            ("reported_ph", "slightly_acidic", 6.4),
        ]
        for ctype, suffix, ph in clean_specs:
            assert PH_MIN <= ph <= PH_MAX, f"clean spec not in range: {suffix}"
            cases.append(self._case(f"clean_{ctype}_{suffix}", "clean", ctype, ph))

        planted_specs: list[tuple[str, str, float | int]] = [
            ("ph", "negative", -1.0),          # below 0
            ("ph", "way_negative", -3.2),
            ("ph", "over_fourteen", 15.0),     # above 14
            ("reported_ph", "fifteen_point_two", 15.2),
            ("reported_ph", "twenty", 20),
            ("reported_ph", "just_below_zero", -0.5),
        ]
        for ctype, suffix, ph in planted_specs:
            assert not (PH_MIN <= ph <= PH_MAX), f"planted spec is actually in range: {suffix}"
            cases.append(self._case(f"planted_{ctype}_{suffix}", "planted", ctype, ph))

        return cases

    @staticmethod
    def _case(name: str, kind: str, claim_type: str, reported_ph: float | int) -> SelfTestCase:
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.NUMBER,
            location=Location(section="self_test", quote=f"pH {reported_ph}"),
            extracted_values={REPORTED_PH_KEY: reported_ph},
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The reported pH is {reported_ph}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.T1,
            predicate="0 <= reported_ph <= 14",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery + the entry-point seam (DESIGN §9).
VERIFIERS = [PhBounds()]
