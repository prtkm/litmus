"""WS-H · the managed-agents executor + Supabase persistence (DESIGN §13, §15).

Three layers, only one of which touches the network:

  * ``test_supabase_payload_*`` — pure payload-shape tests for ``litmus.app_backend.supabase_io``
    (no I/O): the upsert body matches the ``papers`` columns and status walks the lifecycle.
  * ``test_managed_fallback_*`` — ``run_managed_audit`` with the managed beta forced unavailable:
    proves the seam still returns a correct, schema-valid AuditReport via the in-process fallback
    (``meta.executor == "managed:fallback-local"``), with the flagged claim's flag confirmed and a
    non-reproducing flag dropped (DESIGN §13.4).
  * ``test_live_managed_session`` — a real managed session (guarded behind ANTHROPIC_API_KEY).
    SKIPS with a clear message if the beta is not enabled / the API is unreachable; never fakes.
"""

from __future__ import annotations

import json
import os

import pytest

from litmus.app_backend.supabase_io import (
    PaperStatus,
    SupabaseConfig,
    SupabaseIO,
    paper_row_payload,
)
from litmus.core import schema
from litmus.core.claim import Claim, ClaimGraph, EpistemicTier, Evidence, EvidenceKind, Location
from litmus.core.finding import (
    EvidencePacket,
    Finding,
    Severity,
    Status,
    TrustTier,
    VerifierKind,
)
from litmus.core.provenance import AuditReport
from litmus.commons.registry import Registry
from litmus.pipeline import managed
from litmus.pipeline.managed import run_managed_audit
from litmus.core.verifier import Determinism, Verifier, VerifierManifest


# ============================================================================
# A tiny hand-built ClaimGraph: 3 claims, one of which flags (DESIGN §13).
# Reuses the first-party verifiers so judging is the real deterministic path.
# ============================================================================
def _claim(cid, tier, ev_id, conf=0.9):
    return Claim(
        id=cid,
        text=f"claim {cid}",
        location=Location(section="test", quote=f"q{cid}"),
        epistemic_tier=tier,
        confidence=conf,
        evidence_refs=[ev_id],
    )


def _tiny_graph() -> ClaimGraph:
    """sum_check: one table sums wrong (FAIL → must be confirmed), one sums right (PASS),
    one subjective claim routed to a human. Exactly one reproducible flag."""
    evidence = [
        Evidence(id="e_bad", kind=EvidenceKind.TABLE, extracted_values={"parts": [12, 25, 60], "reported_total": 100}),
        Evidence(id="e_ok", kind=EvidenceKind.TABLE, extracted_values={"parts": [40, 35, 25], "reported_total": 100}),
        Evidence(id="e_subj", kind=EvidenceKind.TEXT, extracted_values={}),
    ]
    claims = [
        _claim("c_bad", EpistemicTier.T0, "e_bad"),    # 12+25+60 = 97 != 100 → sum_check FAIL
        _claim("c_ok", EpistemicTier.T0, "e_ok"),      # 40+35+25 = 100 → sum_check PASS
        _claim("c_subj", EpistemicTier.T8, "e_subj"),  # subjective → routed to human
    ]
    return ClaimGraph(paper_id="managed-tiny", claims=claims, evidence=evidence)


def _focused_registry() -> Registry:
    from litmus.verifiers import sum_check

    reg = Registry()
    reg.register_all(sum_check.VERIFIERS)
    return reg


class _BadFlagVerifier(Verifier):
    """A FAIL whose recompute_script does NOT reproduce its expected_output — must be dropped in
    fresh-context confirmation (DESIGN §13.4), whether that confirmation runs in the managed
    sandbox or the local fallback."""

    manifest = VerifierManifest(
        id="bad_flag.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["anything"],
    )

    def judge(self, claim, evidence):
        return self.make_finding(
            claim=claim,
            status=Status.FAIL,
            severity=Severity.A,
            message="bogus flag",
            evidence=EvidencePacket(recompute_script="print('WRONG')", expected_output="RIGHT"),
        )

    def self_test(self):
        return []


# ============================================================================
# 1) Supabase payload-shape (no network).
# ============================================================================
def test_supabase_payload_matches_papers_columns():
    graph = _tiny_graph()
    report = AuditReport(paper_id=graph.paper_id)
    payload = paper_row_payload(
        content_hash="sha256:abc",
        doi="10.1234/xyz",
        title="A Test Paper",
        field="chemistry",
        status=PaperStatus.DONE,
        claim_graph=graph,
        audit_report=report,
    )
    # Exactly the writable columns from supabase/migrations/0001_init.sql — nothing extra.
    assert set(payload) == {"content_hash", "doi", "title", "field", "status", "claim_graph", "audit_report"}
    assert payload["status"] == "done"
    # Domain objects are serialized to jsonb-ready dicts (schema-valid).
    assert isinstance(payload["claim_graph"], dict) and payload["claim_graph"]["paper_id"] == graph.paper_id
    assert isinstance(payload["audit_report"], dict) and payload["audit_report"]["paper_id"] == graph.paper_id
    assert schema.validate(payload["claim_graph"], "claim") == []
    assert schema.validate(payload["audit_report"], "audit") == []


def test_supabase_partial_status_payload():
    # A status-only update payload carries just that key (drives queued→…→done).
    p = paper_row_payload(status=PaperStatus.CONFIRMING)
    assert p == {"status": "confirming"}
    assert paper_row_payload(content_hash="h", status="extracting") == {"content_hash": "h", "status": "extracting"}


def test_supabase_status_lifecycle_values():
    # The lifecycle the executor walks (DESIGN §13) maps to the migration's text values.
    assert [s.value for s in (
        PaperStatus.QUEUED, PaperStatus.EXTRACTING, PaperStatus.AUDITING,
        PaperStatus.CONFIRMING, PaperStatus.DONE,
    )] == ["queued", "extracting", "auditing", "confirming", "done"]
    assert PaperStatus.ERROR.value == "error"


def test_supabase_config_headers_use_service_key():
    cfg = SupabaseConfig(url="https://proj.supabase.co/", secret_key="sb_secret_xyz")
    assert cfg.rest_url == "https://proj.supabase.co/rest/v1"
    h = cfg.headers()
    # Service role goes in BOTH apikey and bearer (PostgREST), bypassing RLS (DESIGN §15).
    assert h["apikey"] == "sb_secret_xyz"
    assert h["Authorization"] == "Bearer sb_secret_xyz"


def test_supabase_upsert_uses_merge_duplicates_on_content_hash():
    """Drive SupabaseIO against a fake httpx client and assert the REST contract: POST to the
    papers table with on_conflict=content_hash + Prefer: merge-duplicates (so a re-audit updates
    in place), service-role headers, and the full row body."""
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 201

        def json(self):
            return [{"id": "uuid-1", "content_hash": "sha256:abc", "status": "done"}]

        text = ""

    class _FakeHttp:
        def request(self, method, url, **kw):
            captured.update({"method": method, "url": url, **kw})
            return _Resp()

    io = SupabaseIO(SupabaseConfig(url="https://proj.supabase.co", secret_key="sb_secret_xyz"), client=_FakeHttp())
    graph = _tiny_graph()
    row = io.upsert_paper(
        content_hash="sha256:abc",
        title="T",
        field="chemistry",
        status=PaperStatus.DONE,
        claim_graph=graph,
        audit_report=AuditReport(paper_id=graph.paper_id),
    )
    assert row["content_hash"] == "sha256:abc"
    assert captured["method"] == "POST"
    assert "on_conflict=content_hash" in captured["url"]
    assert "merge-duplicates" in captured["headers"]["Prefer"]
    assert captured["headers"]["apikey"] == "sb_secret_xyz"
    assert captured["json"]["content_hash"] == "sha256:abc"
    assert captured["json"]["claim_graph"]["paper_id"] == graph.paper_id


def test_supabase_update_status_patches_by_content_hash():
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        def json(self):
            return [{"content_hash": "h", "status": "confirming"}]

        text = ""

    class _FakeHttp:
        def request(self, method, url, **kw):
            captured.update({"method": method, "url": url, **kw})
            return _Resp()

    io = SupabaseIO(SupabaseConfig(url="https://proj.supabase.co", secret_key="k"), client=_FakeHttp())
    row = io.update_status("h", PaperStatus.CONFIRMING)
    assert row["status"] == "confirming"
    assert captured["method"] == "PATCH"
    assert "content_hash=eq.h" in captured["url"]
    # status + updated_at are written (the updated_at column exists since migration 0002 so the
    # poller can detect movement); no error key unless an error is passed.
    body = captured["json"]
    assert body["status"] == "confirming"
    assert "updated_at" in body and isinstance(body["updated_at"], str)
    assert "error" not in body
    # When an error IS passed (terminal failure), it lands in the error column too.
    io.update_status("h", PaperStatus.ERROR, error="boom")
    assert captured["json"]["status"] == "error"
    assert captured["json"]["error"] == "boom"


# ============================================================================
# 2) run_managed_audit fallback path (no network): the seam is real even with
#    the managed beta unavailable.
# ============================================================================
def test_managed_fallback_confirms_real_flag(monkeypatch):
    """Force the managed client to be unavailable; run_managed_audit must fall back to the local
    sandbox, return a schema-valid report with the one real flag CONFIRMED, the PASS present, and
    the subjective claim routed — and mark meta.executor accordingly."""
    def _boom(*a, **k):
        raise RuntimeError("managed-agents beta not enabled (simulated)")

    monkeypatch.setattr(managed, "_client", _boom)

    report = run_managed_audit(_tiny_graph(), registry=_focused_registry())
    assert isinstance(report, AuditReport)
    assert report.meta["executor"] == "managed:fallback-local"
    assert "fallback_reason" in report.meta["managed"]

    flags = report.checkable
    assert len(flags) == 1 and flags[0].verifier_id == "sum_check.v1"
    assert flags[0].status is Status.FAIL
    assert any(f.status is Status.PASS for f in report.findings)
    assert len(report.routed_to_human) == 1 and report.routed_to_human[0].claim_id == "c_subj"
    assert report.dropped_flags == []
    assert schema.validate(report.to_dict(), "audit") == []


def test_managed_fallback_drops_non_reproducing_flag(monkeypatch):
    monkeypatch.setattr(managed, "_client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no beta")))
    reg = Registry()
    reg.register(_BadFlagVerifier())
    graph = ClaimGraph(
        paper_id="managed-drop",
        claims=[_claim("c1", EpistemicTier.T0, "e1")],
        evidence=[Evidence(id="e1", kind=EvidenceKind.NUMBER, extracted_values={"x": 1})],
    )
    report = run_managed_audit(graph, registry=reg)
    assert report.meta["executor"] == "managed:fallback-local"
    assert len(report.checkable) == 0  # bogus flag did not survive confirmation
    assert len(report.dropped_flags) == 1
    assert "did not reproduce" in report.dropped_flags[0].reason
    assert schema.validate(report.to_dict(), "audit") == []


def test_managed_fallback_disabled_raises(monkeypatch):
    monkeypatch.setattr(managed, "_client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no beta")))
    with pytest.raises(RuntimeError, match="allow_fallback=False"):
        run_managed_audit(_tiny_graph(), registry=_focused_registry(), allow_fallback=False)


def test_managed_assemble_report_from_tool_records():
    """The assembler builds the report from the HOST's deterministic verifier Findings (source of
    truth, DESIGN §3.1), keeping a flag only if confirm_recompute reproduced it; an unconfirmed
    flag becomes a DroppedFlag (DESIGN §13.4). The coordinator's classification adds routed items."""
    from litmus.pipeline.managed_tools import VerifierToolHost

    # A graph with one wrong sum (FAIL, reproduces) and one subjective claim.
    graph = _tiny_graph()
    host = VerifierToolHost(_focused_registry())

    # Drive the host exactly as the coordinator would: run_verifier on the two T0 claims, then
    # confirm the flag that comes back.
    c_bad = graph.claim_by_id("c_bad")
    out = json.loads(host.handle("run_verifier", {
        "verifier_id": "sum_check.v1", "claim": c_bad.to_dict(),
        "evidence": [e.to_dict() for e in graph.evidence_for(c_bad)],
    }))
    assert out["status"] == "fail"
    host.handle("confirm_recompute", {
        "recompute_script": out["recompute_script"], "expected_output": out["expected_output"]})
    c_ok = graph.claim_by_id("c_ok")
    host.handle("run_verifier", {
        "verifier_id": "sum_check.v1", "claim": c_ok.to_dict(),
        "evidence": [e.to_dict() for e in graph.evidence_for(c_ok)],
    })

    final = {
        "claims": [
            {"claim_id": "c_bad", "classification": "flaggable", "verifier_id": "sum_check.v1"},
            {"claim_id": "c_ok", "classification": "correct", "verifier_id": "sum_check.v1"},
            {"claim_id": "c_subj", "classification": "subjective", "reason": "significance is subjective"},
        ],
        "routed_to_human": [{"claim_id": "c_subj", "dimension": "significance", "note": "subjective"}],
        "panel_summary": "one sum is wrong; the subjective claim is routed.",
    }
    out_report = managed._assemble_report(graph, host, final, confirm=True)
    assert [f.verifier_id for f in out_report.checkable] == ["sum_check.v1"]
    assert out_report.checkable[0].claim_id == "c_bad"
    assert any(f.status is Status.PASS for f in out_report.findings)
    assert len(out_report.routed_to_human) == 1 and out_report.routed_to_human[0].claim_id == "c_subj"
    assert out_report.dropped_flags == []
    assert schema.validate(out_report.to_dict(), "audit") == []


def test_managed_assemble_drops_unconfirmed_flag():
    """A FAIL the coordinator never confirmed (or that doesn't reproduce) is dropped host-side so
    the §13.4 invariant always holds — no flag ships unconfirmed."""
    from litmus.pipeline.managed_tools import VerifierToolHost

    reg = Registry()
    reg.register(_BadFlagVerifier())
    graph = ClaimGraph(
        paper_id="managed-drop",
        claims=[_claim("c1", EpistemicTier.T0, "e1")],
        evidence=[Evidence(id="e1", kind=EvidenceKind.NUMBER, extracted_values={"x": 1})],
    )
    host = VerifierToolHost(reg)
    c1 = graph.claim_by_id("c1")
    host.handle("run_verifier", {
        "verifier_id": "bad_flag.v1", "claim": c1.to_dict(),
        "evidence": [e.to_dict() for e in graph.evidence_for(c1)],
    })  # FAIL with a non-reproducing script; the coordinator does NOT confirm it
    final = {"claims": [{"claim_id": "c1", "classification": "flaggable", "verifier_id": "bad_flag.v1"}]}
    report = managed._assemble_report(graph, host, final, confirm=True)
    assert len(report.checkable) == 0  # host-side confirmation caught the bogus flag
    assert len(report.dropped_flags) == 1
    assert "did not reproduce" in report.dropped_flags[0].reason
    assert schema.validate(report.to_dict(), "audit") == []


def test_final_parser_tolerates_prose():
    txt = (
        "I have completed the audit.\n"
        'LITMUS_AUDIT {"claims": [{"claim_id": "c1", "classification": "correct"}], '
        '"routed_to_human": [], "panel_summary": "sound"}\nDone.'
    )
    parsed = managed._parse_final(txt)
    assert parsed is not None and parsed["claims"][0]["claim_id"] == "c1"
    # Recover from an embedded object with no marker (last balanced {...} with a "claims" key).
    txt2 = 'noise {"claims": [{"claim_id": "c9", "classification": "subjective"}], "panel_summary": "x"} trailing'
    parsed2 = managed._parse_final(txt2)
    assert parsed2["claims"][0]["claim_id"] == "c9"
    # Brace inside a string must not confuse the balancer.
    txt3 = 'LITMUS_AUDIT {"claims": [], "panel_summary": "uses a { brace in prose"}'
    parsed3 = managed._parse_final(txt3)
    assert parsed3 == {"claims": [], "panel_summary": "uses a { brace in prose"}


# ============================================================================
# 3) LIVE managed session (guarded). Honest skip if the beta is not enabled.
# ============================================================================
def _live_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.mark.live
@pytest.mark.skipif(not _live_enabled(), reason="ANTHROPIC_API_KEY not set — skipping live managed-agents test")
def test_live_managed_session():
    """Create a REAL managed-agents coordinator session and run run_managed_audit on the tiny
    graph. Asserts a schema-valid AuditReport with the one expected confirmed flag. If the beta is
    unavailable on this key (404/403/beta error) or the SDK lacks the surface, SKIP with a clear
    message — never fake success. (The full-paper live audit lives in test_managed_live.py.)"""
    try:
        import anthropic  # noqa: F401
    except Exception as exc:
        pytest.skip(f"anthropic SDK not importable: {exc}")

    events: list[str] = []

    def _capture(kind, payload):
        if kind == "tool_use":
            events.append(str(payload))

    # allow_fallback=True so we always get a report; we then inspect meta.executor to tell whether
    # the managed coordinator actually ran, and surface the session id / reason either way.
    report = run_managed_audit(
        _tiny_graph(),
        registry=_focused_registry(),
        allow_fallback=True,
        timeout_s=600.0,
        on_event=_capture,
    )

    assert isinstance(report, AuditReport)
    assert schema.validate(report.to_dict(), "audit") == []

    if report.meta.get("executor") != "managed":
        reason = report.meta.get("managed", {}).get("fallback_reason")
        pytest.skip(
            f"managed-agents coordinator unavailable on this key — fell back to local. "
            f"session_id={report.meta.get('managed', {}).get('session_id')!r} reason={reason!r}"
        )

    # Managed path ran: assert the verdict and surface the session id for the report.
    flags = report.checkable
    assert len(flags) == 1 and flags[0].verifier_id == "sum_check.v1", [f.verifier_id for f in flags]
    assert flags[0].status is Status.FAIL
    assert report.dropped_flags == []  # the one real flag reproduced via confirm_recompute
    assert report.meta["managed"].get("session_id"), report.meta["managed"]
    print(
        "LIVE managed session OK — session_id=",
        report.meta["managed"].get("session_id"),
        "tool_calls=", report.meta.get("tool_calls"),
        "confirmed flags=", [f.verifier_id for f in flags],
    )
