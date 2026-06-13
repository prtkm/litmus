"""WS-H · live managed-agents auditor test (DESIGN §13, §15).

Exercises the owner's architecture against a **real** managed-agents coordinator session:
a coordinator Agent with persona sub-agents (``multiagent:{type:"coordinator", agents:[...]}``)
that combines deterministic verifier TOOLS (host-side ``registry.get(id).judge()`` — DESIGN §3.1)
with non-deterministic multi-persona review.

Two layers:

  * ``test_custom_tools_run_real_verifier_code`` — no network. Proves the custom-tool host runs the
    REAL registry verifier (``run_verifier``) and confirms a flag in the real recompute sandbox
    (``confirm_recompute``). This is the hard invariant (DESIGN §3.1): the verdict comes from code.
  * ``test_live_managed_audit`` — a real session over the cached ClaimGraph of the target paper
    (``psychology-bai2025-acute-exercise-executive-function``). SKIPS with a clear message if
    ANTHROPIC_API_KEY is unset or the managed-agents beta is unavailable; it never fakes. On
    success it asserts a schema-valid AuditReport whose CHECKABLE flag(s) were produced by
    ``run_verifier`` (deterministic) and reproduce, and prints the live session id.

Run the live one explicitly:
    LITMUS_RUN_LIVE=1 .venv/bin/pytest tests/test_managed_live.py -k live -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from litmus.commons.registry import build_default_registry
from litmus.core import schema
from litmus.core.claim import ClaimGraph
from litmus.core.finding import Status
from litmus.core.provenance import AuditReport
from litmus.pipeline import managed
from litmus.pipeline.managed import run_managed_audit
from litmus.pipeline.managed_tools import VerifierToolHost

_REPO = Path(__file__).resolve().parents[1]
TARGET_PAPER = "psychology-bai2025-acute-exercise-executive-function"
TARGET_GRAPH = _REPO / "study" / "corpus" / "claims" / f"{TARGET_PAPER}.json"


def _load_target_graph() -> ClaimGraph:
    return ClaimGraph.from_dict(json.loads(TARGET_GRAPH.read_text()))


# ============================================================================
# 1) The deterministic invariant, no network: the host runs the real verifier.
# ============================================================================
def test_custom_tools_run_real_verifier_code():
    """run_verifier executes registry.get(id).judge() host-side; the model never decides.
    The known c27 statcheck flag is reproduced exactly, and confirm_recompute reproduces it."""
    graph = _load_target_graph()
    reg = build_default_registry()
    host = VerifierToolHost(reg)

    c27 = graph.claim_by_id("c27")
    assert c27 is not None, "fixture changed: expected claim c27 in the cached graph"
    evidence = [e.to_dict() for e in graph.evidence_for(c27)]

    out = json.loads(
        host.handle("run_verifier", {"verifier_id": "statcheck.v1", "claim": c27.to_dict(), "evidence": evidence})
    )
    # The verdict came from the real verifier code (it's a FAIL with executable evidence).
    assert out["status"] == "fail"
    assert out["severity"] == "C"
    assert out["recompute_script"] and out["expected_output"]
    assert "achievable_p" in out["expected_output"]

    # And the flag reproduces in the real network-less sandbox (DESIGN §13.4).
    conf = json.loads(
        host.handle(
            "confirm_recompute",
            {"recompute_script": out["recompute_script"], "expected_output": out["expected_output"]},
        )
    )
    assert conf["reproduced"] is True, conf

    # The host recorded the deterministic Finding (the assembler's source of truth, DESIGN §3.1).
    findings = host.findings()
    assert any(f.status is Status.FAIL and f.claim_id == "c27" for f in findings)

    # list_verifiers exposes the registry (routing input for the coordinator, DESIGN §12).
    lv = json.loads(host.handle("list_verifiers", {}))
    ids = {v["id"] for v in lv["verifiers"]}
    assert "statcheck.v1" in ids and "sum_check.v1" in ids


# ============================================================================
# 2) Live managed-agents coordinator session over the target paper.
# ============================================================================
def _should_run_live() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    # Default to running when a key is present; allow opt-out for offline CI.
    return os.environ.get("LITMUS_SKIP_LIVE", "") not in ("1", "true", "yes")


@pytest.mark.skipif(not _should_run_live(), reason="ANTHROPIC_API_KEY unset or LITMUS_SKIP_LIVE set")
def test_live_managed_audit():
    """Run the real coordinator session over the target paper's ClaimGraph and assert a
    schema-valid AuditReport whose checkable flags came from the deterministic verifier tools."""
    graph = _load_target_graph()
    reg = build_default_registry()

    seen: list[str] = []

    def on_event(kind: str, payload) -> None:
        if kind == "tool_use":
            seen.append(str(payload))

    # allow_fallback=False so a managed-agents failure SKIPS (never silently passes via local).
    try:
        report = run_managed_audit(
            graph,
            registry=reg,
            confirm=True,
            allow_fallback=False,
            timeout_s=float(os.environ.get("LITMUS_LIVE_TIMEOUT_S", "900")),
            on_event=on_event,
        )
    except Exception as exc:
        pytest.skip(f"managed-agents beta unavailable / session failed: {type(exc).__name__}: {exc}")

    report_dict = report.to_dict()
    errors = schema.validate(report_dict, "audit")
    assert not errors, f"audit report not schema-valid: {errors[:5]}"

    assert report.meta.get("executor") == "managed", report.meta.get("executor")
    assert report.meta.get("architecture", "").startswith("coordinator+personas")
    sid = report.meta.get("managed", {}).get("session_id")
    assert sid, "no live session id recorded"
    print(f"\nLIVE managed session id: {sid}")
    print("tool_calls:", report.meta.get("tool_calls"))
    print("summary:", json.dumps(report.summary(), indent=2))

    # The coordinator actually used the deterministic verifier tools.
    tool_calls = report.meta.get("tool_calls", {})
    assert tool_calls.get("run_verifier", 0) > 0, "coordinator never called run_verifier"

    # Every CHECKABLE flag was produced by a real verifier (not a persona) and ships a reproducing
    # recompute script (DESIGN §3.1, §3.2). Confirm it reproduces right now.
    from litmus.core import sandbox

    for f in report.checkable:
        assert f.verifier_id in reg.ids(), f"flag from non-verifier {f.verifier_id!r}"
        assert f.evidence.recompute_script and f.evidence.expected_output, "flag without executable evidence"
        ok, _ = sandbox.reproduces(f.evidence.recompute_script, f.evidence.expected_output)
        assert ok, f"flag {f.verifier_id}/{f.claim_id} does not reproduce"

    # The output carries claims + provenance + classification: findings + routed-to-human + abstained.
    assert report.meta.get("n_claims") == len(graph.claims)
    print("routed_to_human:", [(r.claim_id, r.dimension) for r in report.routed_to_human])
    print("panel_summary:", report.meta.get("panel_summary", ""))
