"""WS-B live smoke test — actually calls Opus 4.8 (DESIGN §11, §19).

Guarded: skips unless ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is set, so the default
test run stays offline. Runs the real extractor on an owner paper and asserts the result is a
schema-valid ClaimGraph with substantive content: >= 8 claims, every claim carries a proposed
tier, and at least one claim has bound evidence with a transcribed number. Uses the on-disk
cache (DESIGN §10) to keep this to one API call per paper.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from litmus.core import schema
from litmus.extract import extract_to_file

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PDF = _REPO_ROOT / "study" / "corpus" / "pdfs" / "mehta-2018-overcoming-scaling-relations.pdf"

_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

pytestmark = [
    pytest.mark.skipif(not _HAS_KEY, reason="no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN set"),
    pytest.mark.skipif(not _PDF.is_file(), reason=f"corpus PDF missing: {_PDF}"),
]


def test_live_extract_owner_paper_is_substantive():
    # Uses cache if study/corpus/claims/<id>.json already exists -> at most one API call.
    graph, out = extract_to_file(str(_PDF), use_cache=True)

    assert out.is_file()
    assert schema.validate(graph.to_dict(), "claim") == [], "extracted graph must be schema-valid"
    assert len(graph.claims) >= 8, f"expected >= 8 claims, got {len(graph.claims)}"

    # Every claim carries a proposed epistemic tier.
    untiered = [c.id for c in graph.claims if c.epistemic_tier is None]
    assert not untiered, f"claims missing a proposed tier: {untiered}"

    # At least one claim is bound to evidence that carries a transcribed (non-null) number.
    def _has_number(values: dict) -> bool:
        return any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values.values())

    bound_with_number = [
        c.id
        for c in graph.claims
        for ev in graph.evidence_for(c)
        if _has_number(ev.extracted_values)
    ]
    assert bound_with_number, "no claim has bound evidence with a transcribed number"
