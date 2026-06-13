"""The system-level calibration scorecard — the project's machine-checkable "done" (DESIGN §7, §19).

``verify.py`` is the harness that aggregates every registered verifier's individual calibration
result (DESIGN §7) into one report. It is the WS-A gate (DESIGN §19): it admits a real reference
verifier as SCORING, rejects a non-deterministic one, and prints a scorecard — recall, FPR,
reproducibility, and determinism all measured with zero human labels.

Usage::

    python -m litmus.verify            # human-readable scorecard, exit 0 iff no regression
    python -m litmus.verify --json     # machine-readable list of scorecard dicts
    python -m litmus.verify --strict   # exit 1 unless EVERY verifier is admitted SCORING

Exit policy:
  * ``--strict``  — fail (exit 1) if ANY registered verifier is not SCORING.
  * default       — fail (exit 1) only if a verifier *regresses below its declared intent*.
        For WS-A every registered first-party verifier is intended to score, so the default and
        strict predicates currently coincide: exit 1 if any registered verifier is not SCORING.
        ``_intended_scoring`` is the seam that widens the default once deliberately-advisory
        verifiers (e.g. LLM-anchored, DESIGN §6.1 class D) join the library.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from litmus.commons.registry import Registry, build_default_registry
from litmus.core.calibration import AdmissionStatus, Scorecard, calibrate


def _intended_scoring(card: Scorecard) -> bool:
    """Whether this verifier is *meant* to score, so falling short of SCORING is a regression.

    For WS-A every registered first-party verifier is intended-scoring. When deliberately
    -advisory verifiers enter the library this is where their intent gets declared (e.g. by
    verifier id / kind), so the default gate stops treating their advisory status as a failure.
    """
    return True


def _gate_passes(cards: list[Scorecard], *, strict: bool) -> bool:
    """The gate verdict under the active policy.

    ``strict`` requires every verifier to be SCORING. The default requires only that no
    *intended-scoring* verifier dropped out of SCORING (a regression).
    """
    if strict:
        return all(c.admission is AdmissionStatus.SCORING for c in cards)
    return not _regressions(cards)


def _regressions(cards: list[Scorecard]) -> list[Scorecard]:
    """Intended-scoring verifiers that are not SCORING (the default-policy failures)."""
    return [
        c for c in cards if _intended_scoring(c) and c.admission is not AdmissionStatus.SCORING
    ]


def run(registry: Optional[Registry] = None) -> list[Scorecard]:
    """Calibrate every registered verifier and return their scorecards (registration order)."""
    reg = registry if registry is not None else build_default_registry()
    return [calibrate(v) for v in reg.all()]


def _print_report(cards: list[Scorecard], *, strict: bool) -> bool:
    """Print the human-readable scorecard. Returns True iff the gate passes under the policy."""
    print("LITMUS — system calibration scorecard (DESIGN §7)")
    print("=" * 78)
    if not cards:
        print("(no verifiers registered)")

    for card in cards:
        print(card.summary_line())
        # Surface the deciding reason(s) so a failure is self-explanatory.
        for reason in card.reasons:
            print(f"    {reason}")

    print("-" * 78)
    n_scoring = sum(1 for c in cards if c.admission is AdmissionStatus.SCORING)
    n_advisory = sum(1 for c in cards if c.admission is AdmissionStatus.ADVISORY)
    n_rejected = sum(1 for c in cards if c.admission is AdmissionStatus.REJECTED)
    print(
        f"{len(cards)} verifier(s): {n_scoring} scoring, "
        f"{n_advisory} advisory, {n_rejected} rejected"
    )

    if strict:
        policy = "strict: every verifier must be SCORING"
        not_scoring = [c for c in cards if c.admission is not AdmissionStatus.SCORING]
        if not_scoring:
            ids = ", ".join(c.verifier_id for c in not_scoring)
            print(f"NOT SCORING: {ids}")
    else:
        policy = "default: no intended-scoring verifier may regress"
        regressed = _regressions(cards)
        if regressed:
            ids = ", ".join(c.verifier_id for c in regressed)
            print(f"REGRESSION: intended-scoring verifier(s) not SCORING: {ids}")

    ok = _gate_passes(cards, strict=strict)
    print(f"GATE [{policy}]: {'PASS' if ok else 'FAIL'}")
    return ok


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="litmus.verify",
        description="Run the calibration gate over every registered verifier (DESIGN §7).",
    )
    parser.add_argument("--json", action="store_true", help="emit scorecards as a JSON list")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless every registered verifier is admitted SCORING",
    )
    args = parser.parse_args(argv)

    cards = run()

    if args.json:
        print(json.dumps([c.to_dict() for c in cards], indent=2, sort_keys=True))
    else:
        _print_report(cards, strict=args.strict)

    return 0 if _gate_passes(cards, strict=args.strict) else 1


if __name__ == "__main__":
    sys.exit(main())
