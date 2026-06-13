"""The LITMUS MCP adapter — audits exposed as programmatic tools (DESIGN §6.3, §15).

Mirrors the framing of the CLI tests but for the MCP surface. Every assertion holds the adapter to
the project's non-negotiables: code judges, not the agent (DESIGN §3.1); every flag ships runnable,
reproducible proof (DESIGN §3.2); results are schema-valid; a bad call returns a structured error,
never a crash (DESIGN §13).

Two layers are exercised:
  * the tool FUNCTIONS directly (fast, the bulk of the coverage);
  * the tools THROUGH an in-process MCP client/server session (the real protocol path), so we know
    an agent connecting over stdio gets the same structured JSON.

The recompute-script claim is verified for real: a planted sum-mismatch's recompute_script is run
in the actual fresh sandbox and must reproduce its expected_output — the run-it-yourself promise,
checked (DESIGN §3.2, §13.4).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from litmus.adapters import mcp as M
from litmus.core import sandbox
from litmus.core.schema import validate as schema_validate


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _call_via_client(tool_name: str, arguments: dict) -> dict:
    """Call a tool through a real in-process MCP client/server session and return its JSON result.

    Spins up the FastMCP server in memory, connects a client, calls the tool, and returns the
    parsed structured result — the exact path an agent over stdio takes.
    """
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def _run() -> dict:
        async with connect(M.mcp) as client:
            await client.initialize()
            result = await client.call_tool(tool_name, arguments)
            assert not result.isError, f"tool {tool_name} returned isError"
            # FastMCP returns a dict both as structuredContent and as JSON text content.
            if result.structuredContent is not None:
                return result.structuredContent
            return json.loads(result.content[0].text)

    return asyncio.run(_run())


def _tiny_sum_mismatch_graph() -> dict:
    """A one-claim ClaimGraph with a planted sum-mismatch (10+20+30 = 60, reported 65)."""
    return {
        "schema_version": "1.0",
        "paper_id": "tiny_test",
        "meta": {},
        "claims": [
            {
                "id": "c1",
                "text": "The three components sum to 65.",
                "location": {"section": "Table 1", "quote": "Total: 65"},
                "epistemic_tier": "T0",
                "predicate": "sum(parts) == reported_total",
                "evidence_refs": ["e1"],
                "confidence": 1.0,
            }
        ],
        "evidence": [
            {
                "id": "e1",
                "kind": "table",
                "location": {"section": "Table 1", "quote": "10, 20, 30; total 65"},
                "extracted_values": {"parts": [10, 20, 30], "reported_total": 65},
                "confidence": 1.0,
            }
        ],
        "bindings": [{"claim_id": "c1", "evidence_id": "e1", "relation": "rests_on"}],
    }


# ---------------------------------------------------------------------------
# list_verifiers — the catalog an agent routes against (DESIGN §6.3, §12).
# ---------------------------------------------------------------------------
def test_list_verifiers_returns_the_nine():
    out = M.list_verifiers()
    assert out["count"] == 9, "expected the 9 first-party verifiers"
    assert len(out["verifiers"]) == 9
    ids = {v["id"] for v in out["verifiers"]}
    # The headline checks the convenience tools wrap must all be present.
    assert {"statcheck.v1", "yield_check.v1", "grim.v1", "percent_change.v1", "sum_check.v1"} <= ids
    # Each catalog row carries the routing-relevant manifest fields (DESIGN §6.3).
    row = next(v for v in out["verifiers"] if v["id"] == "statcheck.v1")
    for key in (
        "epistemic_tier",
        "kind",
        "determinism",
        "consumes",
        "capability_tags",
        "fpr_ceiling",
        "description",
    ):
        assert key in row, f"manifest row missing {key}"
    assert "p_value" in row["consumes"]


def test_list_verifiers_via_client_matches_direct():
    """The protocol path returns the same catalog as the direct call."""
    out = _call_via_client("list_verifiers", {})
    assert out["count"] == 9


# ---------------------------------------------------------------------------
# run_verifier — the verifier's deterministic judge(), and its reproducible proof.
# ---------------------------------------------------------------------------
def test_run_verifier_planted_sum_mismatch_fails_with_reproducible_script():
    """A planted sum-mismatch FAILs, and its recompute_script reproduces in the real sandbox.

    This is the whole thesis in one test: the verdict is code (DESIGN §3.1), and it ships a script
    a skeptical agent reruns to confirm it (DESIGN §3.2).
    """
    res = M.run_verifier(
        "sum_check.v1",
        claim="The three components sum to 65.",
        evidence={"parts": [10, 20, 30], "reported_total": 65},
    )
    assert res["status"] == "fail"
    assert res["is_flag"] is True
    assert res["severity"] == "B"
    assert res["verifier_id"] == "sum_check.v1"
    # The Finding is schema-valid (finding.schema.json).
    assert res["schema_valid"] is True
    # The run-it-yourself payload is present...
    script = res["recompute_script"]
    expected = res["expected_output"]
    assert script and expected
    # ...and actually reproduces in a fresh, network-less sandbox (DESIGN §3.2, §13.4).
    reproduced, sbx = sandbox.reproduces(script, expected)
    assert reproduced, f"recompute_script did not reproduce: stdout={sbx.stdout!r} stderr={sbx.stderr!r}"
    assert "MISMATCH" in sbx.stdout


def test_run_verifier_clean_input_passes():
    res = M.run_verifier(
        "sum_check.v1",
        claim="The three components sum to 60.",
        evidence={"parts": [10, 20, 30], "reported_total": 60},
    )
    assert res["status"] == "pass"
    assert res["is_flag"] is False


def test_run_verifier_unknown_id_is_structured_error():
    """An unknown verifier returns an error result listing the known ids — never raises."""
    res = M.run_verifier("does_not_exist.v9", claim="x", evidence={})
    assert "error" in res
    assert "known_verifier_ids" in res
    assert "sum_check.v1" in res["known_verifier_ids"]


def test_run_verifier_bad_evidence_is_structured_error():
    """Malformed evidence is reported, not raised (DESIGN §13)."""
    res = M.run_verifier("sum_check.v1", claim="x", evidence=123)
    assert "error" in res


def test_run_verifier_via_client_reproduces():
    """Through the protocol, the planted mismatch still flags with a reproducible script."""
    res = _call_via_client(
        "run_verifier",
        {
            "verifier_id": "sum_check.v1",
            "claim": "totals to 65",
            "evidence": {"parts": [10, 20, 30], "reported_total": 65},
        },
    )
    assert res["status"] == "fail"
    reproduced, _ = sandbox.reproduces(res["recompute_script"], res["expected_output"])
    assert reproduced


# ---------------------------------------------------------------------------
# check_statistic — statcheck on an inconsistent (test, stat, df, p) (DESIGN §5 T0).
# ---------------------------------------------------------------------------
def test_check_statistic_flags_inconsistent_p():
    """t = 2.0, df = 20 is really p ~= 0.059 (ns); reported significant 0.04 -> FAIL (flips .05)."""
    res = M.check_statistic(test="t", statistic=2.0, df=20, reported_p=0.04)
    assert res["status"] == "fail"
    assert res["is_flag"] is True
    assert res["severity"] == "B"  # decision-flip (significance) error
    assert res["schema_valid"] is True
    assert res["recompute_script"]
    assert "INCONSISTENT" in (res["expected_output"] or "")
    # And the script reproduces (DESIGN §3.2).
    reproduced, _ = sandbox.reproduces(res["recompute_script"], res["expected_output"])
    assert reproduced


def test_check_statistic_consistent_p_passes():
    """t = 2.5, df = 18 is ~0.0223; reported 0.02 is a correct rounding -> PASS."""
    res = M.check_statistic(test="t", statistic=2.5, df=18, reported_p=0.02)
    assert res["status"] == "pass"
    assert res["is_flag"] is False


def test_check_statistic_F_uses_df1_df2():
    """The F branch needs df1 AND df2; a materially-wrong p flips the decision and FAILs."""
    res = M.check_statistic(test="F", statistic=2.0, df1=1, df2=20, reported_p=0.03)
    assert res["status"] == "fail"


def test_check_statistic_via_client():
    res = _call_via_client(
        "check_statistic", {"test": "t", "statistic": 2.0, "df": 20, "reported_p": 0.04}
    )
    assert res["status"] == "fail"
    assert res["severity"] == "B"


# ---------------------------------------------------------------------------
# The other convenience checks call the REAL verifier (DESIGN §6.3).
# ---------------------------------------------------------------------------
def test_check_yield_impossible_is_severity_A():
    res = M.check_yield(reported_yield_pct=142)
    assert res["status"] == "fail"
    assert res["severity"] == "A"  # a hard physical-bound violation
    assert "IMPOSSIBLE_YIELD" in (res["expected_output"] or "")


def test_check_yield_possible_passes():
    res = M.check_yield(reported_yield_pct=72)
    assert res["status"] == "pass"


def test_check_grim_impossible_fails():
    """mean 3.45 with n = 10 is unachievable (10 * 3.45 = 34.5 is not an integer total)."""
    res = M.check_grim(reported_mean=3.45, n=10)
    assert res["status"] == "fail"
    assert res["severity"] == "B"


def test_check_percent_change_overclaim_fails():
    """50 -> 68 is +36%, not the reported +40% -> FAIL."""
    res = M.check_percent_change(old_value=50, new_value=68, reported_pct_change=40)
    assert res["status"] == "fail"


# ---------------------------------------------------------------------------
# audit_claim_graph — the full pipeline -> a schema-valid AuditReport (DESIGN §13, §14).
# ---------------------------------------------------------------------------
def test_audit_claim_graph_returns_valid_report_with_confirmed_flag():
    report = M.audit_claim_graph(_tiny_sum_mismatch_graph())
    # Schema-valid against audit.schema.json (the adapter asserts this; we re-check).
    assert report["schema_valid"] is True
    assert schema_validate(
        {k: v for k, v in report.items() if k != "schema_valid"}, "audit"
    ) == []
    summary = report["summary"]
    assert summary["n_flags"] == 1
    # The flag survived fresh-context confirmation (its script reproduced) -> deterministic_confirmed.
    flag = report["findings"][0]
    assert flag["status"] == "fail"
    assert flag["verifier_id"] == "sum_check.v1"
    assert flag["trust_tier"] == "deterministic_confirmed"
    assert summary["n_dropped"] == 0


def test_audit_claim_graph_clean_graph_no_flags():
    graph = _tiny_sum_mismatch_graph()
    graph["evidence"][0]["extracted_values"]["reported_total"] = 60  # now correct
    report = M.audit_claim_graph(graph)
    assert report["schema_valid"] is True
    assert report["summary"]["n_flags"] == 0


def test_audit_claim_graph_invalid_input_is_structured_error():
    """A graph that fails claim-schema validation returns an error with the violations."""
    res = M.audit_claim_graph({"paper_id": "x"})  # missing required structure
    assert "error" in res
    assert "validation_errors" in res


def test_audit_claim_graph_via_client():
    report = _call_via_client("audit_claim_graph", {"claim_graph": _tiny_sum_mismatch_graph()})
    assert report["summary"]["n_flags"] == 1


# ---------------------------------------------------------------------------
# calibration_scorecard — the trust each tool earns (DESIGN §7).
# ---------------------------------------------------------------------------
def test_calibration_scorecard_grades_every_verifier():
    out = M.calibration_scorecard()
    assert out["count"] == 9
    assert len(out["scorecards"]) == 9
    # The reference verifier is admitted SCORING (the WS-A gate; DESIGN §7, §19).
    card = next(c for c in out["scorecards"] if c.get("verifier_id") == "sum_check.v1")
    assert card["admission"] == "scoring"
    assert card["recall"] is not None
    assert "fpr_overall" in card


# ---------------------------------------------------------------------------
# Auto-generated per-verifier tools (DESIGN §6.3: one manifest -> CLI + MCP tool).
# ---------------------------------------------------------------------------
def test_auto_generated_tool_per_verifier_registered():
    # One run_<id> tool for each of the 9 verifiers.
    assert len(M.AUTO_TOOL_NAMES) == 9
    assert "run_sum_check_v1" in M.AUTO_TOOL_NAMES
    assert "run_statcheck_v1" in M.AUTO_TOOL_NAMES


def test_full_tool_inventory_over_protocol():
    """The server exposes all hand-written + auto-generated tools over the real protocol."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def _run() -> set[str]:
        async with connect(M.mcp) as client:
            await client.initialize()
            tools = await client.list_tools()
            return {t.name for t in tools.tools}

    names = asyncio.run(_run())
    # 10 hand-written tools + 9 auto-generated per-verifier tools = 19.
    assert len(names) == 19
    expected_hand = {
        "list_verifiers",
        "run_verifier",
        "audit_claim_graph",
        "extract_claims",
        "audit_pdf",
        "check_statistic",
        "check_yield",
        "check_grim",
        "check_percent_change",
        "calibration_scorecard",
    }
    assert expected_hand <= names
    assert set(M.AUTO_TOOL_NAMES) <= names


def test_auto_generated_tool_runs_judge():
    """An auto-generated per-verifier tool judges exactly like run_verifier."""
    res = _call_via_client(
        "run_sum_check_v1",
        {"claim": "totals to 65", "evidence": {"parts": [10, 20, 30], "reported_total": 65}},
    )
    assert res["status"] == "fail"
    assert res["verifier_id"] == "sum_check.v1"
