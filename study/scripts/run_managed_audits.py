"""Run the FULL managed-agents (LLM coordinator + deterministic verifier tools) audit on each
demo claim graph, one paper at a time, and write the audit report to study/corpus/audits/.

This is the non-deterministic + deterministic workflow: a Claude coordinator reasons over the
ClaimGraph, calls deterministic verifier tools for checkable claims, and convenes persona
sub-agents (skeptic, domain-expert, methodologist, claims-auditor, integrity-screener) for the
subjective dimensions. meta["executor"] records whether the managed beta ran or it fell back local.
"""
import json
import sys
from pathlib import Path

from litmus.commons.registry import build_default_registry
from litmus.core import schema
from litmus.core.claim import ClaimGraph
from litmus.pipeline.managed import run_managed_audit

PAPERS = sys.argv[1:] or [
    "psychology-festinger1959-cognitive-dissonance",
    "nutrition-wansink2015-buffet-price-regret",
    "nutrition-just2014-buffet-taste-satisfaction",
    "psychology-kniffin2016-men-eat-more-with-women",
    "health-econ-loo2024-pediatric-mental-health-spending",
    "global-health-2023-disability-play-opportunities",
    "psychology-kanngiesser2012-merit-sharing",
]

registry = build_default_registry()
audits = Path("study/corpus/audits")
audits.mkdir(parents=True, exist_ok=True)

for pid in PAPERS:
    src = Path("study/corpus/claims") / f"{pid}.json"
    if not src.exists():
        print(f"!! missing claim graph: {src}")
        continue
    print(f"\n######## managed audit: {pid} ########")
    graph = ClaimGraph.from_dict(json.loads(src.read_text()))

    def _ev(kind, payload, _pid=pid):
        # Compact live trace so we can see the LLM coordinator working.
        if kind in ("agent_started", "persona", "tool_use", "tool_result", "classification", "status"):
            print(f"   [{kind}] {str(payload)[:120]}")

    report = run_managed_audit(graph, registry=registry, confirm=True, on_event=_ev)
    d = report.to_dict()
    errors = schema.validate(d, "audit")
    out = audits / f"{pid}.json"
    out.write_text(json.dumps(d, indent=2))
    s = report.summary()
    print(f"   executor={report.meta.get('executor')}  flags={s['n_flags']} "
          f"routed={s['n_routed_to_human']} abstained={s['n_abstained']} dropped={s['n_dropped']} "
          f"schema_valid={not errors}")
print("\nDONE")
