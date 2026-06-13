"""Key-alignment regression: a canonical key in extracted_values reaches a verifier (DESIGN §11, §12).

The bug this guards: the extractor emits free-form ``extracted_values`` whose keys no verifier
recognizes, so every deterministic verifier abstains on real papers (0 flags, all synthesis
candidates). The fix is the prompt teaching the model to ALSO add the verifier's canonical keys
(``reported_yield_pct``, ``test``/``statistic``/``df``/``reported_p``, ``parts``/``reported_total``,
...) whenever a transcribed quantity matches a checkable pattern.

These tests do NOT touch the network. They drive a hand-built ``emit_claim_graph`` tool response
through the SAME parse path the live extractor uses (``_extract_tool_input`` -> ``build_claim_graph``),
then run the resulting graph through ``LocalExecutor`` and assert the verifier FIRES (the canonical
key bound it) — exactly what the live re-extraction is supposed to achieve. The flagship case is the
impossible 142% yield (DESIGN §6.1 class A).
"""

from __future__ import annotations

from types import SimpleNamespace

from litmus.commons.registry import Registry
from litmus.core import schema
from litmus.core.claim import EpistemicTier
from litmus.core.finding import Severity, Status, TrustTier
from litmus.extract import build_claim_graph
from litmus.extract.extractor import _extract_tool_input
from litmus.pipeline.executor import LocalExecutor
from litmus.verifiers import (
    percent_change,
    statcheck,
    sum_check,
    yield_check,
)

TOOL_NAME = "emit_claim_graph"


# --- fakes: a tool response whose evidence carries CANONICAL verifier keys ---------------
def _yield_tool_input() -> dict:
    """A realistic ``emit_claim_graph`` input for a paper reporting an impossible 142% yield.

    Crucially, ``extracted_values`` carries BOTH a descriptive key (as the old extractor would
    emit) AND the canonical ``reported_yield_pct`` key the new prompt requires — proving the
    canonical key is what makes ``yield_check`` bind.
    """
    return {
        "claims": [
            {
                "id": "c1",
                "text": "The product was obtained in 142% yield.",
                "location": {
                    "section": "Results",
                    "page": 3,
                    "char_span": None,
                    "quote": "the product was obtained in 142% yield",
                },
                "epistemic_tier": "T1",
                "predicate": "reported_yield_pct <= 100",
                "strength": "exact",
                "scope": "Table 1, entry 4",
                "evidence_refs": ["ev1"],
                "confidence": 0.9,
            }
        ],
        "evidence": [
            {
                "id": "ev1",
                "kind": "table",
                "location": {"section": "Results", "page": 3, "quote": "142"},
                "extracted_values": {
                    # descriptive key the model also kept (paper-specific) ...
                    "entry4_yield": 142.0,
                    # ... and the CANONICAL key the new prompt adds for [yield_check].
                    "reported_yield_pct": 142.0,
                },
                "confidence": 0.9,
            }
        ],
        "bindings": [{"claim_id": "c1", "evidence_id": "ev1", "relation": "rests_on"}],
    }


def _fake_message(tool_input: dict, *, name: str = TOOL_NAME) -> SimpleNamespace:
    """A fake Anthropic ``Message`` whose single content block is the emit_claim_graph tool call."""
    block = SimpleNamespace(type="tool_use", name=name, id="toolu_x", input=tool_input)
    return SimpleNamespace(content=[block], stop_reason="tool_use", role="assistant", id="msg_x")


def _focused_registry() -> Registry:
    """Just the verifiers these tests rely on — isolated from sibling churn."""
    reg = Registry()
    reg.register_all(
        yield_check.VERIFIERS
        + percent_change.VERIFIERS
        + sum_check.VERIFIERS
        + statcheck.VERIFIERS
    )
    return reg


# --- the canonical key survives the parse path ------------------------------------------
def test_canonical_yield_key_survives_extractor_parse_path():
    """A 142% yield emitted with ``reported_yield_pct`` must reach the graph through the same
    parse path the live extractor uses (no value invented, key preserved verbatim)."""
    tool_input = _extract_tool_input(_fake_message(_yield_tool_input()))
    graph = build_claim_graph(tool_input, paper_id="keyalign-yield")

    assert schema.validate(graph.to_dict(), "claim") == []
    ev1 = graph.evidence_by_id("ev1")
    # the canonical key the verifier consumes is present and carries the transcribed number ...
    assert ev1.extracted_values["reported_yield_pct"] == 142.0
    # ... and the descriptive key the model also kept is preserved alongside it.
    assert ev1.extracted_values["entry4_yield"] == 142.0


# --- the aligned key makes the verifier FIRE (the whole point) --------------------------
def test_local_executor_flags_impossible_yield_via_canonical_key():
    """End to end: the parse-path graph, run through LocalExecutor, produces a confirmed
    impossible-yield flag — the verifier bound BECAUSE the canonical key was present."""
    tool_input = _extract_tool_input(_fake_message(_yield_tool_input()))
    graph = build_claim_graph(tool_input, paper_id="keyalign-yield")

    report = LocalExecutor(confirm=True).audit_graph(graph, _focused_registry())

    flags = report.checkable
    assert len(flags) == 1, [f.verifier_id for f in flags]
    flag = flags[0]
    assert flag.verifier_id == "yield_check.v1"
    assert flag.status is Status.FAIL
    assert flag.severity is Severity.A  # >100% is the severity-A physical-bound wedge
    assert flag.trust_tier is TrustTier.DETERMINISTIC_CONFIRMED  # survived fresh-context confirm
    assert flag.reported == 142.0
    # the flag carries a recompute script that actually reproduced (DESIGN §3.2 / §13.4).
    assert report.dropped_flags == []
    assert flag.evidence.recompute_script
    assert "IMPOSSIBLE_YIELD" in (flag.evidence.expected_output or "")
    assert schema.validate(report.to_dict(), "audit") == []


def test_free_form_key_alone_does_not_fire_verifier():
    """Control: WITHOUT the canonical key, the same number under a descriptive-only key abstains —
    this is precisely the pre-fix failure the prompt change repairs."""
    bad = _yield_tool_input()
    # strip the canonical key, leaving only the descriptive one (the old extractor's behavior).
    del bad["evidence"][0]["extracted_values"]["reported_yield_pct"]
    graph = build_claim_graph(_extract_tool_input(_fake_message(bad)), paper_id="keyalign-noalign")

    report = LocalExecutor(confirm=True).audit_graph(graph, _focused_registry())

    assert report.checkable == []  # no verifier could bind -> no flag
    # a checkable (T1) claim with evidence that no verifier covered is a synthesis candidate.
    assert "c1" in report.meta.get("synthesis_candidates", [])


def test_canonical_statcheck_keys_fire_via_parse_path():
    """A second pattern (a mis-reported p-value) to show the alignment is not yield-specific:
    t=2.0, df=20 is p≈0.059 (ns) but reported as significant .04 -> statcheck FAILs."""
    tool_input = {
        "claims": [
            {
                "id": "c1",
                "text": "The effect was significant, t(20) = 2.0, p = .04.",
                "location": {"quote": "t(20) = 2.0, p = .04"},
                "epistemic_tier": "T0",
                "predicate": "reported_p is a correct rounding of p from the statistic",
                "evidence_refs": ["ev1"],
                "confidence": 0.9,
            }
        ],
        "evidence": [
            {
                "id": "ev1",
                "kind": "statistic",
                "location": {"quote": "t(20) = 2.0, p = .04"},
                # canonical [statcheck] keys, alongside a descriptive label.
                "extracted_values": {
                    "comparison": "treatment_vs_control",
                    "test": "t",
                    "statistic": 2.0,
                    "df": 20,
                    "reported_p": 0.04,
                },
                "confidence": 0.9,
            }
        ],
        "bindings": [{"claim_id": "c1", "evidence_id": "ev1", "relation": "rests_on"}],
    }
    graph = build_claim_graph(_extract_tool_input(_fake_message(tool_input)), paper_id="keyalign-stat")
    report = LocalExecutor(confirm=True).audit_graph(graph, _focused_registry())

    flags = report.checkable
    assert len(flags) == 1, [f.verifier_id for f in flags]
    assert flags[0].verifier_id == "statcheck.v1"
    assert flags[0].status is Status.FAIL
    assert report.dropped_flags == []
    assert schema.validate(report.to_dict(), "audit") == []
