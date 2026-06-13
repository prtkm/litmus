"""The audit pipeline + the ExecutorAdapter seam (DESIGN §13, §15).

One upload → ``extract → plan → verify (parallel) → fresh-context confirm → assemble`` (DESIGN
§13). The pipeline is written ONCE and reached through :class:`ExecutorAdapter`, with two
implementations (DESIGN §15):

  * :class:`LocalExecutor` — subprocess/thread workers + direct API calls; used by the CLI,
        the calibration gate, and the discovery study. No external services.
  * :class:`ManagedAgentExecutor` — runs the same pipeline inside a Claude managed-agents
        session; used by the live app (WS-H). Only this one depends on managed agents.

Step 4 (fresh-context confirm) is the autonomy beat: every emitted flag's ``recompute_script``
is re-run in a fresh, network-less sandbox and **dropped if it does not reproduce** — the
dropped-flag log is the system catching its own false positives (DESIGN §13.4, §14).
"""

from __future__ import annotations

import abc
import concurrent.futures
import os
from dataclasses import dataclass, field
from typing import Optional

from litmus.commons.registry import Registry, build_default_registry
from litmus.core import sandbox
from litmus.core.claim import Claim, ClaimGraph
from litmus.core.finding import EvidencePacket, Finding, Status, TrustTier, VerifierKind
from litmus.core.provenance import AuditReport, DroppedFlag, RoutedItem
from litmus.plan.planner import CHECKABLE_TIERS, Action, plan_audit


def _abstain_finding(claim: Claim, reason: str) -> Finding:
    """A planner-level INCONCLUSIVE (DESIGN §3.4) — recorded in the report's abstained list."""
    return Finding(
        verifier_id="litmus.planner",
        claim_id=claim.id,
        status=Status.INCONCLUSIVE,
        trust_tier=TrustTier.ROUTED_TO_HUMAN,
        verifier_kind=VerifierKind.PREBUILT,
        message=reason,
        evidence=EvidencePacket(quote=claim.location.quote, location=claim.location),
    )


@dataclass
class _ClaimOutcome:
    claim: Claim
    verdicts: list[Finding] = field(default_factory=list)  # PASS/FAIL (non-abstain)
    routed: Optional[RoutedItem] = None
    abstained: Optional[Finding] = None
    synth_candidate: bool = False


class ExecutorAdapter(abc.ABC):
    """The seam (DESIGN §15). The framework + study never depend on managed agents — only
    :class:`ManagedAgentExecutor` does."""

    @abc.abstractmethod
    def audit_graph(self, graph: ClaimGraph, registry: Optional[Registry] = None) -> AuditReport:
        """Run verifiers over an already-extracted ClaimGraph and return the audit report."""


class LocalExecutor(ExecutorAdapter):
    """Runs the pipeline locally: in-process verifier judging + subprocess sandbox confirmation
    + direct Opus calls for extraction (DESIGN §13, §15). No external services."""

    def __init__(self, *, confirm: bool = True, max_workers: Optional[int] = None) -> None:
        self.confirm = confirm
        self.max_workers = max_workers or min(8, (os.cpu_count() or 4))

    # --- the pipeline over an extracted graph (DESIGN §13 steps 2-5) ---------
    def audit_graph(self, graph: ClaimGraph, registry: Optional[Registry] = None) -> AuditReport:
        registry = registry or build_default_registry()
        plans = {p.claim_id: p for p in plan_audit(graph, registry)}

        # Phase 1: plan + judge each claim (DESIGN §13 steps 2-3).
        outcomes: list[_ClaimOutcome] = []
        for claim in graph.claims:
            plan = plans[claim.id]
            outcome = _ClaimOutcome(claim=claim)
            if plan.action is Action.ROUTE_TO_HUMAN:
                outcome.routed = RoutedItem(
                    claim_id=claim.id,
                    dimension=f"tier:{claim.epistemic_tier.value if claim.epistemic_tier else '?'}",
                    note=plan.reason,
                    quote=claim.location.quote,
                )
                outcomes.append(outcome)
                continue
            if plan.action is Action.ABSTAIN:
                outcome.abstained = _abstain_finding(claim, plan.reason)
                outcomes.append(outcome)
                continue

            evidence = graph.evidence_for(claim)
            verdicts: list[Finding] = []
            for vid in plan.verifier_ids:
                try:
                    verdicts.append(registry.get(vid).judge(claim, evidence))
                except Exception as exc:  # a broken verifier never sinks the audit
                    continue
            non_abstain = [f for f in verdicts if f.status in (Status.PASS, Status.FAIL)]
            if non_abstain:
                outcome.verdicts = non_abstain
            else:
                # No verifier covered it. On a checkable claim with evidence, that's the
                # synthesis signal (DESIGN §8); otherwise just an abstain.
                if claim.epistemic_tier in CHECKABLE_TIERS and evidence:
                    outcome.synth_candidate = True
                    outcome.abstained = _abstain_finding(
                        claim,
                        "no existing verifier covers this checkable claim — synthesis candidate "
                        "(DESIGN §8, WS-F)",
                    )
                else:
                    outcome.abstained = _abstain_finding(claim, "no applicable verifier")
            outcomes.append(outcome)

        # Phase 2: fresh-context confirmation of every FAIL, in parallel (DESIGN §13 step 4).
        flags = [f for o in outcomes for f in o.verdicts if f.status is Status.FAIL]
        kept, dropped = self._confirm(flags)

        # Phase 3: assemble the report (DESIGN §13 step 5, §14).
        findings: list[Finding] = []
        routed: list[RoutedItem] = []
        abstained: list[Finding] = []
        synth_candidates: list[str] = []
        for o in outcomes:
            if o.routed is not None:
                routed.append(o.routed)
            if o.abstained is not None:
                abstained.append(o.abstained)
            if o.synth_candidate:
                synth_candidates.append(o.claim.id)
            for f in o.verdicts:
                if f.status is Status.PASS:
                    findings.append(f)
                elif f.status is Status.FAIL and f in kept:
                    findings.append(f)

        report = AuditReport(
            paper_id=graph.paper_id,
            findings=findings,
            dropped_flags=dropped,
            routed_to_human=routed,
            abstained=abstained,
            meta={
                "executor": "local",
                "n_claims": len(graph.claims),
                "n_verifiers": len(registry),
                "synthesis_candidates": synth_candidates,
                "confirmation": "fresh-sandbox" if self.confirm else "disabled",
                "registry_load_errors": registry.load_errors,
            },
        )
        return report

    def _confirm(self, flags: list[Finding]) -> tuple[list[Finding], list[DroppedFlag]]:
        """Re-run each flag's recompute_script in a fresh sandbox; keep iff it reproduces."""
        if not self.confirm:
            return flags, []
        kept: list[Finding] = []
        dropped: list[DroppedFlag] = []

        def check(f: Finding) -> tuple[Finding, bool, str]:
            problems = f.validate()
            if problems:
                return f, False, "; ".join(problems)
            ok, res = sandbox.reproduces(
                f.evidence.recompute_script or "", f.evidence.expected_output or ""
            )
            why = "" if ok else (
                "recompute_script did not reproduce expected_output in a fresh, network-less "
                f"sandbox (DESIGN §13.4){'' if res.ok else ': ' + res.stderr.strip()[:160]}"
            )
            return f, ok, why

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for f, ok, why in pool.map(check, flags):
                if ok:
                    kept.append(f)
                else:
                    dropped.append(DroppedFlag(finding=f, reason=why))
        return kept, dropped

    # --- the full upload→audit over a PDF (DESIGN §13 step 1 + 2-5) ----------
    def audit_pdf(
        self,
        pdf_path: str,
        registry: Optional[Registry] = None,
        *,
        paper_id: Optional[str] = None,
        model: str = "claude-opus-4-8",
    ) -> AuditReport:
        """Extract a PDF (Opus vision) then audit it. The only model-in-the-loop step is
        extraction (DESIGN §11); judging is always code."""
        from litmus.extract.extractor import extract_claim_graph

        graph = extract_claim_graph(pdf_path, model=model, paper_id=paper_id)
        return self.audit_graph(graph, registry)


class ManagedAgentExecutor(ExecutorAdapter):
    """Runs the pipeline inside a Claude managed-agents coordinator session (DESIGN §13, §15 — WS-H).

    Only this adapter depends on managed agents. The session combines deterministic verifier TOOLS
    (host-side ``registry.get(id).judge()`` — DESIGN §3.1) with non-deterministic multi-persona LLM
    review; ``litmus/pipeline/managed.py`` builds it. This shell keeps the seam visible in the core
    so the framework never reaches for managed agents. Constructor kwargs (``model``, ``confirm``,
    ``timeout_s``, ``allow_fallback``, ``resources``, ``api_key``, ``on_event``) flow through to
    :func:`~litmus.pipeline.managed.run_managed_audit`.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._args = args
        self._kwargs = kwargs

    def audit_graph(self, graph: ClaimGraph, registry: Optional[Registry] = None) -> AuditReport:
        try:
            from litmus.pipeline.managed import run_managed_audit  # WS-H
        except ImportError as exc:  # not built yet
            raise NotImplementedError(
                "ManagedAgentExecutor is provided by WS-H (litmus/pipeline/managed.py); "
                "use LocalExecutor for the framework/study/gate (DESIGN §15)."
            ) from exc
        return run_managed_audit(graph, registry=registry, *self._args, **self._kwargs)

    def audit_pdf(
        self,
        pdf_path: str,
        registry: Optional[Registry] = None,
        *,
        paper_id: Optional[str] = None,
        model: str = "claude-opus-4-8",
    ) -> AuditReport:
        """Extract a PDF (Opus vision — the only model-in-the-loop *finding* step, DESIGN §11)
        then audit the resulting ClaimGraph inside the managed-agents coordinator session. The
        deterministic verdicts still come from the verifier tools, never the model (DESIGN §3.1).
        """
        from litmus.extract.extractor import extract_claim_graph

        graph = extract_claim_graph(pdf_path, model=model, paper_id=paper_id)
        return self.audit_graph(graph, registry)
