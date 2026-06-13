"""WS-B unit tests — the parse path, no API (DESIGN §11, §19).

We feed a hand-built fake ``emit_claim_graph`` tool response through the same parse path the
live extractor uses (``_extract_tool_input`` -> ``build_claim_graph``) and assert it produces a
schema-valid ClaimGraph, that a missing value stays ``None`` (never invented), and that quotes
pass through verbatim. These tests must not touch the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from litmus.core import schema
from litmus.core.claim import ClaimGraph, EpistemicTier, EvidenceKind
from litmus.extract import build_claim_graph, default_out_path, default_paper_id
from litmus.extract.extractor import (
    ExtractionError,
    _extract_tool_input,
    _relax_additional_properties,
    _tool_input_schema,
    extract_to_file,
)

TOOL_NAME = "emit_claim_graph"


# --- fixtures: a realistic tool input + a fake SDK Message wrapping it -------------------
def _fake_tool_input() -> dict:
    """A plausible raw ``emit_claim_graph`` input, including a deliberately-missing value."""
    return {
        "claims": [
            {
                "id": "c1",
                "text": "The reaction proceeds in 142% yield.",
                "location": {
                    "section": "Results",
                    "page": 3,
                    "char_span": None,
                    "quote": "the product was obtained in 142% yield",
                },
                "epistemic_tier": "T0",
                "predicate": "reported_yield_pct == 100 * moles_product / moles_limiting",
                "strength": "exact",
                "scope": "Table 1, entry 4",
                "evidence_refs": ["ev1"],
                "confidence": 0.9,
            },
            {
                "id": "c2",
                "text": "Catalyst loading had no significant effect on selectivity.",
                "location": {"quote": "no significant effect on selectivity was observed"},
                "epistemic_tier": "T4",
                "predicate": "selectivity is invariant to catalyst_loading within stated error",
                "strength": "qualitative",
                "scope": None,
                "evidence_refs": ["ev2"],
                "confidence": 0.4,
            },
        ],
        "evidence": [
            {
                "id": "ev1",
                "kind": "table",
                "location": {"section": "Results", "page": 3, "quote": "142"},
                # moles_limiting is MISSING in the paper -> must be transcribed as null.
                "extracted_values": {
                    "reported_yield_pct": 142.0,
                    "moles_product": 0.71,
                    "moles_limiting": None,
                },
                "confidence": 0.85,
            },
            {
                "id": "ev2",
                "kind": "statistic",
                "location": {"quote": "p = 0.62"},
                "extracted_values": {"p": 0.62},
                "confidence": 0.8,
            },
        ],
        "bindings": [
            {"claim_id": "c1", "evidence_id": "ev1", "relation": "rests_on"},
            {"claim_id": "c2", "evidence_id": "ev2", "relation": "rests_on"},
        ],
    }


def _fake_message(tool_input: dict, *, name: str = TOOL_NAME) -> SimpleNamespace:
    """A fake Anthropic ``Message``: ``.content`` is a list of blocks with ``.type``/.name/.input."""
    block = SimpleNamespace(type="tool_use", name=name, id="toolu_x", input=tool_input)
    return SimpleNamespace(
        content=[block], stop_reason="tool_use", role="assistant", id="msg_x"
    )


# --- the parse path builds a schema-valid graph -----------------------------------------
def test_build_claim_graph_is_schema_valid():
    graph = build_claim_graph(_fake_tool_input(), paper_id="paper-xyz", model="claude-opus-4-8")
    assert isinstance(graph, ClaimGraph)
    assert graph.paper_id == "paper-xyz"
    assert schema.validate(graph.to_dict(), "claim") == []
    assert schema.is_valid(graph.to_dict(), "claim")


def test_full_parse_path_from_fake_message():
    """Mirror the live flow: pull the tool input off a fake Message, then build the graph."""
    msg = _fake_message(_fake_tool_input())
    tool_input = _extract_tool_input(msg)
    graph = build_claim_graph(tool_input, paper_id="paper-xyz")
    assert schema.validate(graph.to_dict(), "claim") == []
    assert len(graph.claims) == 2
    assert len(graph.evidence) == 2
    assert len(graph.bindings) == 2
    # Tiers proposed by the "extractor" survive the round trip.
    assert graph.claim_by_id("c1").epistemic_tier is EpistemicTier.T0
    assert graph.claim_by_id("c2").epistemic_tier is EpistemicTier.T4
    assert graph.evidence_by_id("ev1").kind is EvidenceKind.TABLE


def test_missing_value_stays_none_not_invented():
    """A null in extracted_values must remain None — never backfilled with a fabricated number."""
    graph = build_claim_graph(_fake_tool_input(), paper_id="p")
    ev1 = graph.evidence_by_id("ev1")
    assert "moles_limiting" in ev1.extracted_values  # the key is preserved...
    assert ev1.extracted_values["moles_limiting"] is None  # ...and the value stays null.
    # The present values are transcribed exactly.
    assert ev1.extracted_values["reported_yield_pct"] == 142.0
    assert ev1.extracted_values["moles_product"] == 0.71
    # round-trips through JSON without resurrecting the missing value.
    assert ClaimGraph.from_dict(graph.to_dict()).evidence_by_id("ev1").extracted_values[
        "moles_limiting"
    ] is None


def test_quotes_pass_through_verbatim():
    graph = build_claim_graph(_fake_tool_input(), paper_id="p")
    assert graph.claim_by_id("c1").location.quote == "the product was obtained in 142% yield"
    assert graph.claim_by_id("c2").location.quote == "no significant effect on selectivity was observed"
    assert graph.evidence_by_id("ev2").location.quote == "p = 0.62"


def test_low_confidence_is_preserved_for_planner():
    """Low-confidence extractions stay low so the planner can downgrade/abstain (DESIGN §12)."""
    graph = build_claim_graph(_fake_tool_input(), paper_id="p")
    assert graph.claim_by_id("c2").confidence == 0.4
    assert graph.evidence_by_id("ev1").confidence == 0.85


def test_bindings_link_claims_to_transcribed_numbers():
    graph = build_claim_graph(_fake_tool_input(), paper_id="p")
    c1 = graph.claim_by_id("c1")
    bound = graph.evidence_for(c1)
    assert len(bound) == 1
    assert bound[0].id == "ev1"
    assert bound[0].extracted_values["reported_yield_pct"] == 142.0


def test_provenance_meta_is_stamped():
    graph = build_claim_graph(_fake_tool_input(), paper_id="p", model="claude-opus-4-8")
    assert graph.meta["extractor"] == "litmus.extract"
    assert graph.meta["model"] == "claude-opus-4-8"
    assert graph.meta["source"] == "opus-native-pdf"


def test_extra_meta_is_merged_and_provenance_wins():
    graph = build_claim_graph(
        _fake_tool_input(), paper_id="p", model="claude-opus-4-8", meta={"doi": "10.1/abc"}
    )
    assert graph.meta["doi"] == "10.1/abc"
    assert graph.meta["extractor"] == "litmus.extract"


# --- error paths ------------------------------------------------------------------------
def test_missing_tool_call_raises():
    """A response without the emit_claim_graph tool call is an extraction failure."""
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")
    with pytest.raises(ExtractionError):
        _extract_tool_input(msg)


def test_wrong_tool_name_raises():
    msg = _fake_message(_fake_tool_input(), name="some_other_tool")
    with pytest.raises(ExtractionError):
        _extract_tool_input(msg)


def test_invalid_graph_raises_with_errors():
    """An out-of-enum tier must fail the post-parse schema assertion (not silently pass)."""
    bad = _fake_tool_input()
    bad["claims"][0]["epistemic_tier"] = "T9"  # not a valid tier
    with pytest.raises(Exception):  # ValueError from EpistemicTier or ExtractionError
        build_claim_graph(bad, paper_id="p")


def test_bad_evidence_kind_raises():
    bad = _fake_tool_input()
    bad["evidence"][0]["kind"] = "spreadsheet"  # not in the kind enum
    with pytest.raises(Exception):
        build_claim_graph(bad, paper_id="p")


# --- tool input_schema construction -----------------------------------------------------
def test_relax_additional_properties_flips_false_to_true():
    node = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"x": {"type": "object", "additionalProperties": False}},
        "items": [{"additionalProperties": False}],
    }
    relaxed = _relax_additional_properties(node)
    assert relaxed["additionalProperties"] is True
    assert relaxed["properties"]["x"]["additionalProperties"] is True
    assert relaxed["items"][0]["additionalProperties"] is True
    # original is untouched (deep copy)
    assert node["additionalProperties"] is False


def test_tool_input_schema_is_relaxed_and_stripped():
    s = _tool_input_schema()
    assert s["additionalProperties"] is True
    assert "$schema" not in s
    assert "$id" not in s
    # $defs/$ref are kept for the model.
    assert "$defs" in s
    assert s["properties"]["claims"]["items"]["$ref"] == "#/$defs/claim"


# --- default paths / caching (no network) -----------------------------------------------
def test_default_paper_id_and_out_path():
    assert default_paper_id("/a/b/mehta-2018-overcoming-scaling-relations.pdf") == (
        "mehta-2018-overcoming-scaling-relations"
    )
    out = default_out_path("mehta-2018-overcoming-scaling-relations")
    assert out.name == "mehta-2018-overcoming-scaling-relations.json"
    assert out.parent.name == "claims"


def test_extract_to_file_uses_cache_without_api(tmp_path):
    """If the out file exists and caching is on, extract_to_file must not hit the API.

    We point at a tmp out file pre-populated with a valid graph and pass a client that would
    raise if used — proving the cache short-circuits the network.
    """
    import json

    out = tmp_path / "cached.json"
    graph = build_claim_graph(_fake_tool_input(), paper_id="cached-paper")
    out.write_text(json.dumps(graph.to_dict()), encoding="utf-8")

    class _Boom:
        def __getattr__(self, _):
            raise AssertionError("API client must not be used on a cache hit")

    loaded, path = extract_to_file(
        "/nonexistent/does-not-matter.pdf",
        out_path=out,
        paper_id="cached-paper",
        use_cache=True,
        client=_Boom(),
    )
    assert path == out
    assert loaded.paper_id == "cached-paper"
    assert schema.validate(loaded.to_dict(), "claim") == []
