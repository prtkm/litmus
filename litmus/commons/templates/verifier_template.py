"""``__VERIFIER_ID__`` — TODO one-line summary of what this verifier checks.

Scaffolded by ``litmus verifier new`` (DESIGN §9). This is a starting point, not a finished
verifier: fill in ``judge`` and ``self_test`` and the calibration gate (``litmus verifier test``)
will tell you whether it is admitted SCORING.

The contract (DESIGN §6.3): a verifier is a self-describing package —

  * ``manifest``           — who/what wrote it, what it ``consumes``, its tier + FPR ceiling.
  * ``judge(claim, ev)``   — PURE + DETERMINISTIC: no RNG, clock, network, or LLM. Returns a
        :class:`~litmus.core.finding.Finding`. A ``FAIL`` MUST ship a self-contained, stdlib-only
        ``recompute_script`` whose stdout equals ``expected_output`` (DESIGN §3.2: no script,
        no flag).
  * ``self_test()``        — the admission fuel: clean instances that must PASS and planted
        instances that must FAIL. No self_test -> the gate can never admit it as SCORING.

Trust comes from the calibration gate (DESIGN §7), not from the model.
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

# TODO: the extracted_values key(s) this verifier reads off its bound evidence (DESIGN §11).
VALUE_KEY = "value"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_bound_value(evidence: list[Evidence]) -> Optional[tuple[Evidence, Any]]:
    """First bound evidence carrying ``VALUE_KEY``. Returns ``(evidence, value)`` or ``None``.

    TODO: widen this to pull every value your ``judge`` needs off the evidence.
    """
    for ev in evidence:
        vals = ev.extracted_values or {}
        if VALUE_KEY in vals:
            return ev, vals[VALUE_KEY]
    return None


def _build_recompute_script(value: Any) -> str:
    """A self-contained, stdlib-only program that reprints this verifier's verdict line.

    Run in the recompute sandbox it must print exactly ``expected_output`` (DESIGN §7 G3).
    TODO: make it recompute the quantity your ``judge`` checks, hardcoding the inputs (no
    input(), no network, no clock).
    """
    return (
        "# LITMUS recompute script for __VERIFIER_ID__ (DESIGN §3.2: no script, no flag).\n"
        "# Self-contained, stdlib-only, network-less, deterministic.\n"
        f"VALUE = {value!r}\n"
        "print('VIOLATION value=' + repr(VALUE))\n"
    )


class __VERIFIER_CLASS__(Verifier):
    """TODO one-line statement of the invariant this verifier enforces."""

    manifest = VerifierManifest(
        id="__VERIFIER_ID__",
        version="__VERIFIER_VERSION__",
        kind=VerifierKind.__VERIFIER_KIND__,
        epistemic_tier=EpistemicTier.__VERIFIER_TIER__,
        determinism=Determinism.__VERIFIER_DETERMINISM__,
        consumes=["__VERIFIER_CONSUME__"],  # TODO routing keys: claim_type | record_type (DESIGN §12)
        capability_tags=[],  # TODO tags for discovery (DESIGN §9)
        fpr_ceiling=0.05,
        authors=["__VERIFIER_AUTHOR__"],
        provenance="__VERIFIER_PROVENANCE__",
        built_vs_borrowed={"ours": [], "libs": []},  # DESIGN §3.7: reimplement, don't wrap-and-claim
        dependencies=[],  # stdlib-only by default (DESIGN §3.8 P8)
        description="TODO: what this verifier checks and the evidence shape it binds to.",
    )

    # --- verdict (pure, deterministic) ---------------------------------------
    def judge(self, claim: Claim, evidence: list[Evidence]) -> Finding:
        bound = _find_bound_value(evidence)
        if bound is None:
            # Cannot bind the evidence this verifier needs -> abstain (DESIGN §3.4: abstain > guess).
            return self.abstain(
                claim,
                "no bound evidence carries the value this verifier checks "
                "(DESIGN §3.4: abstain > guess)",
            )
        ev, value = bound

        # TODO: replace this stub with the real check, e.g.::
        #
        #     if <value violates the invariant>:
        #         return self._violation_finding(claim, ev, value)
        #     return self.make_finding(claim=claim, status=Status.PASS, message="ok")
        #
        # A scaffold returns INCONCLUSIVE by default so a freshly-generated verifier is
        # deterministic and never emits a false flag before you have written (and calibrated)
        # its logic. ``ev``/``value`` are bound and ready for your comparison.
        return self.make_finding(
            claim=claim,
            status=Status.INCONCLUSIVE,
            message="judge() is still a scaffold stub — implement the check (DESIGN §6.3)",
            reported=value,
            details={"bound_evidence_id": ev.id},
        )

    def _violation_finding(self, claim: Claim, ev: Evidence, value: Any) -> Finding:
        """A worked example of a FAIL that ships executable evidence (DESIGN §3.2).

        TODO: adapt the message/severity/discrepancy and the recompute script to your invariant.
        A ``FAIL`` is only admissible if this script's stdout equals ``expected_output`` in the
        recompute sandbox (DESIGN §7 G3) — keep the two in lockstep.
        """
        expected = f"VIOLATION value={value!r}"
        script = _build_recompute_script(value)
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
            severity=Severity.A,  # TODO: A (impossible) | B (inconsistency) | C (minor)
            message="TODO: human-readable violation",
            discrepancy="TODO: reported X but should be Y",
            reported=value,
            computed=None,
            evidence=packet,
        )

    # --- admission fuel (DESIGN §6.3) ----------------------------------------
    def self_test(self) -> list[SelfTestCase]:
        """Clean instances that must PASS + planted instances that must FAIL.

        TODO: hand-write fixed, deterministic cases (no RNG — DESIGN §7 G4). Ship at least
        ``min_clean`` clean and ``min_planted`` planted across >=1 claim_type, or the gate keeps
        this verifier ADVISORY (insufficient coverage). An empty list -> the gate rejects it.
        """
        return []

    @staticmethod
    def _case(name: str, kind: str, claim_type: str, value: Any) -> SelfTestCase:
        """Helper to build one self-test instance. TODO: shape the evidence your judge reads."""
        ev = Evidence(
            id=f"ev_{name}",
            kind=EvidenceKind.NUMBER,
            location=Location(section="self_test", quote=f"value {value}"),
            extracted_values={VALUE_KEY: value},
        )
        claim = Claim(
            id=f"claim_{name}",
            text=f"The reported value is {value}.",
            location=Location(section="self_test"),
            epistemic_tier=EpistemicTier.__VERIFIER_TIER__,
            predicate="TODO: the operationalized predicate",
            evidence_refs=[ev.id],
        )
        return SelfTestCase(name=name, kind=kind, claim=claim, evidence=[ev], claim_type=claim_type)


# Module-level export consumed by the registry's auto-discovery + the entry-point seam (DESIGN §9).
VERIFIERS = [__VERIFIER_CLASS__()]
