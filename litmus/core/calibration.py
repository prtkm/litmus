"""The calibration kernel — the immune system (DESIGN §7).

The single admission gate for EVERY verifier, whether first-party, contributed, or
synthesized on the fly. Given a verifier and its ``self_test``, it measures — with **zero
human labels** — whether the verifier can be trusted to *score*:

  * **G1 Recall**          ≥ 0.90 on the verifier's planted errors.
  * **G2 / G6 FPR**        ≤ the declared ceiling on clean instances, overall AND per claim_type.
  * **G3 Reproducibility** = 100% of emitted flags' recompute scripts reproduce their
                             ``expected_output`` in a fresh, network-less sandbox (run twice:
                             must match expected AND be deterministic across runs).
  * **G4 Determinism**     = identical ``judge`` output across N runs (no RNG/clock/network/LLM).

Admission (DESIGN §7):
  * REJECTED  — no self_test, OR non-deterministic (G4), OR a flag fails to reproduce (G3).
                These break hard invariants (DESIGN §3.1, §3.2): the verifier never reaches
                a verdict.
  * ADVISORY  — passes G3+G4 but recall < 0.90 (G1) or FPR over ceiling (G2/G6), or too few
                self-test cases to calibrate. Surfaces flags, but never an A/B verdict.
  * SCORING   — G1 ∧ G2 ∧ G3 ∧ G4 ∧ G6 all hold, with sufficient coverage.

The trust comes from THIS gate, not from the model (DESIGN §8).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from litmus.core import sandbox
from litmus.core.finding import Finding, Status
from litmus.core.verifier import SelfTestCase, Verifier

DEFAULT_RECALL_FLOOR = 0.90
DEFAULT_DETERMINISM_RUNS = 3
DEFAULT_MIN_CLEAN = 3
DEFAULT_MIN_PLANTED = 3
DEFAULT_SANDBOX_TIMEOUT_S = 15.0


class AdmissionStatus(str, Enum):
    SCORING = "scoring"
    ADVISORY = "advisory"
    REJECTED = "rejected"


@dataclass
class Scorecard:
    """The calibration result for one verifier (DESIGN §7). Machine-checkable 'trust'."""

    verifier_id: str
    version: str
    epistemic_tier: str
    kind: str
    declared_fpr_ceiling: float

    n_clean: int = 0
    n_planted: int = 0

    # Gate measurements
    recall: Optional[float] = None  # G1
    fpr_overall: Optional[float] = None  # G2
    fpr_by_claim_type: dict[str, float] = field(default_factory=dict)  # G6
    deterministic: Optional[bool] = None  # G4
    n_flags_checked: int = 0  # G3
    n_flags_reproduced: int = 0  # G3
    reproducibility: Optional[float] = None  # G3

    gates: dict[str, bool] = field(default_factory=dict)  # {"G1":..., "G2":..., ...}
    admission: AdmissionStatus = AdmissionStatus.REJECTED
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_scoring(self) -> bool:
        return self.admission is AdmissionStatus.SCORING

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_id": self.verifier_id,
            "version": self.version,
            "epistemic_tier": self.epistemic_tier,
            "kind": self.kind,
            "declared_fpr_ceiling": self.declared_fpr_ceiling,
            "n_clean": self.n_clean,
            "n_planted": self.n_planted,
            "recall": self.recall,
            "fpr_overall": self.fpr_overall,
            "fpr_by_claim_type": self.fpr_by_claim_type,
            "deterministic": self.deterministic,
            "n_flags_checked": self.n_flags_checked,
            "n_flags_reproduced": self.n_flags_reproduced,
            "reproducibility": self.reproducibility,
            "gates": self.gates,
            "admission": self.admission.value,
            "reasons": self.reasons,
            "details": self.details,
        }

    def summary_line(self) -> str:
        rc = "—" if self.recall is None else f"{self.recall:.2f}"
        fp = "—" if self.fpr_overall is None else f"{self.fpr_overall:.2f}"
        rp = "—" if self.reproducibility is None else f"{self.reproducibility:.0%}"
        det = {True: "yes", False: "NO", None: "—"}[self.deterministic]
        return (
            f"{self.verifier_id:<28} {self.epistemic_tier:<3} {self.kind:<11} "
            f"recall={rc:<5} fpr={fp:<5}(ceil {self.declared_fpr_ceiling:.2f}) "
            f"det={det:<3} repro={rp:<4} -> {self.admission.value.upper()}"
        )


def _canonical(finding: Optional[Finding], error: Optional[str]) -> str:
    """A stable string for determinism comparison."""
    if error is not None:
        return "ERROR::" + error
    assert finding is not None
    return json.dumps(finding.to_dict(), sort_keys=True, ensure_ascii=True)


def _run_judge(verifier: Verifier, case: SelfTestCase) -> tuple[Optional[Finding], Optional[str]]:
    try:
        finding = verifier.judge(case.claim, case.evidence)
        return finding, None
    except Exception as exc:  # a judge that raises is caught; counts as no-catch / not-a-FP
        return None, f"{type(exc).__name__}: {exc}"


def calibrate(
    verifier: Verifier,
    *,
    recall_floor: float = DEFAULT_RECALL_FLOOR,
    determinism_runs: int = DEFAULT_DETERMINISM_RUNS,
    min_clean: int = DEFAULT_MIN_CLEAN,
    min_planted: int = DEFAULT_MIN_PLANTED,
    sandbox_timeout_s: float = DEFAULT_SANDBOX_TIMEOUT_S,
    check_reproducibility: bool = True,
) -> Scorecard:
    """Run the full calibration gate on ``verifier`` and return its Scorecard (DESIGN §7)."""
    m = verifier.manifest
    card = Scorecard(
        verifier_id=m.id,
        version=m.version,
        epistemic_tier=m.epistemic_tier.value,
        kind=m.kind.value,
        declared_fpr_ceiling=m.fpr_ceiling,
    )

    # --- self_test fuel ------------------------------------------------------
    try:
        cases = list(verifier.self_test())
    except Exception as exc:
        card.admission = AdmissionStatus.REJECTED
        card.reasons.append(f"self_test() raised: {type(exc).__name__}: {exc}")
        return card

    if not cases:
        card.admission = AdmissionStatus.REJECTED
        card.reasons.append("no self_test cases (DESIGN §6.3: no self_test -> advisory only / reject)")
        return card

    clean = [c for c in cases if c.kind == "clean"]
    planted = [c for c in cases if c.kind == "planted"]
    card.n_clean = len(clean)
    card.n_planted = len(planted)

    # --- G4 determinism: judge N times per case, compare canonical output ----
    deterministic = True
    nondet_cases: list[str] = []
    first_findings: dict[str, tuple[Optional[Finding], Optional[str]]] = {}
    for case in cases:
        signatures = set()
        first: Optional[tuple[Optional[Finding], Optional[str]]] = None
        for _ in range(max(1, determinism_runs)):
            finding, error = _run_judge(verifier, case)
            if first is None:
                first = (finding, error)
            signatures.add(_canonical(finding, error))
        first_findings[case.name] = first  # type: ignore[assignment]
        if len(signatures) > 1:
            deterministic = False
            nondet_cases.append(case.name)
    card.deterministic = deterministic
    card.gates["G4"] = deterministic
    if not deterministic:
        card.details["nondeterministic_cases"] = nondet_cases

    # --- G1 recall on planted ------------------------------------------------
    if planted:
        caught = 0
        for case in planted:
            finding, _err = first_findings[case.name]
            if finding is not None and finding.status is Status.FAIL:
                caught += 1
        card.recall = caught / len(planted)
        card.gates["G1"] = card.recall >= recall_floor
    else:
        card.gates["G1"] = False

    # --- G2 / G6 FPR on clean (overall + per claim_type) ---------------------
    if clean:
        fp = 0
        by_type_total: dict[str, int] = {}
        by_type_fp: dict[str, int] = {}
        for case in clean:
            by_type_total[case.claim_type] = by_type_total.get(case.claim_type, 0) + 1
            finding, _err = first_findings[case.name]
            is_fp = finding is not None and finding.status is Status.FAIL
            if is_fp:
                fp += 1
                by_type_fp[case.claim_type] = by_type_fp.get(case.claim_type, 0) + 1
        card.fpr_overall = fp / len(clean)
        card.fpr_by_claim_type = {
            t: by_type_fp.get(t, 0) / n for t, n in by_type_total.items()
        }
        g2 = card.fpr_overall <= m.fpr_ceiling
        g6 = all(v <= m.fpr_ceiling for v in card.fpr_by_claim_type.values())
        card.gates["G2"] = g2
        card.gates["G6"] = g6
    else:
        card.gates["G2"] = False
        card.gates["G6"] = False

    # --- G3 reproducibility: every emitted FAIL's script reproduces ----------
    if check_reproducibility:
        n_checked = 0
        n_repro = 0
        repro_failures: list[str] = []
        for case in cases:
            finding, _err = first_findings[case.name]
            if finding is None or finding.status is not Status.FAIL:
                continue
            problems = finding.validate()
            n_checked += 1
            if problems:
                repro_failures.append(f"{case.name}: invalid Finding ({'; '.join(problems)})")
                continue
            script = finding.evidence.recompute_script or ""
            expected = finding.evidence.expected_output or ""
            reproduced, res1 = sandbox.reproduces(script, expected, timeout_s=sandbox_timeout_s)
            # determinism of the script itself (run again, compare stdout)
            res2 = sandbox.run_script(script, timeout_s=sandbox_timeout_s)
            script_deterministic = res1.stdout.strip() == res2.stdout.strip()
            if reproduced and script_deterministic:
                n_repro += 1
            else:
                why = []
                if not reproduced:
                    why.append("stdout != expected_output" if res1.ok else f"script errored: {res1.stderr.strip()[:200]}")
                if not script_deterministic:
                    why.append("script non-deterministic across runs")
                repro_failures.append(f"{case.name}: {'; '.join(why)}")
        card.n_flags_checked = n_checked
        card.n_flags_reproduced = n_repro
        card.reproducibility = (n_repro / n_checked) if n_checked else 1.0
        card.gates["G3"] = card.reproducibility >= 1.0
        if repro_failures:
            card.details["repro_failures"] = repro_failures
    else:
        card.gates["G3"] = True

    # --- admission decision (DESIGN §7) --------------------------------------
    card.admission, card.reasons = _decide(card, min_clean=min_clean, min_planted=min_planted)
    return card


def _decide(card: Scorecard, *, min_clean: int, min_planted: int) -> tuple[AdmissionStatus, list[str]]:
    reasons: list[str] = []

    # Hard rejections — break the non-negotiable invariants.
    if card.gates.get("G4") is False:
        reasons.append("G4 fail: non-deterministic judge (DESIGN §3.1, §7) -> REJECTED")
        return AdmissionStatus.REJECTED, reasons
    if card.gates.get("G3") is False:
        reasons.append(
            f"G3 fail: {card.n_flags_reproduced}/{card.n_flags_checked} flags reproduce "
            f"(DESIGN §3.2: no script, no flag) -> REJECTED"
        )
        return AdmissionStatus.REJECTED, reasons

    # Deterministic + every flag reproduces. Now: scoring vs advisory.
    if card.n_planted < min_planted or card.n_clean < min_clean:
        reasons.append(
            f"insufficient self_test coverage (clean={card.n_clean}<{min_clean} or "
            f"planted={card.n_planted}<{min_planted}) -> ADVISORY"
        )
        return AdmissionStatus.ADVISORY, reasons

    if not card.gates.get("G1", False):
        reasons.append(f"G1 fail: recall {card.recall:.2f} < floor -> ADVISORY")
        return AdmissionStatus.ADVISORY, reasons

    if not card.gates.get("G2", False) or not card.gates.get("G6", False):
        over = {t: v for t, v in card.fpr_by_claim_type.items() if v > card.declared_fpr_ceiling}
        reasons.append(
            f"G2/G6 fail: FPR over ceiling {card.declared_fpr_ceiling:.2f} "
            f"(overall={card.fpr_overall}, over-by-type={over}) -> ADVISORY (DESIGN §7)"
        )
        return AdmissionStatus.ADVISORY, reasons

    reasons.append("G1 ∧ G2 ∧ G3 ∧ G4 ∧ G6 all pass -> SCORING")
    return AdmissionStatus.SCORING, reasons
