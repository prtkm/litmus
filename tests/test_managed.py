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
    assert captured["json"] == {"status": "confirming"}


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


def test_managed_assemble_from_confirmation_unit():
    """The keep/drop assembly that consumes the managed sandbox's result, in isolation: a
    confirmed flag is kept, an unconfirmed one becomes a DroppedFlag (DESIGN §13.4)."""
    f_keep = Finding(
        verifier_id="v.keep", claim_id="c1", status=Status.FAIL, trust_tier=TrustTier.DETERMINISTIC_CONFIRMED,
        verifier_kind=VerifierKind.PREBUILT, severity=Severity.A,
        evidence=EvidencePacket(recompute_script="print('X')", expected_output="X"),
    )
    f_drop = Finding(
        verifier_id="v.drop", claim_id="c2", status=Status.FAIL, trust_tier=TrustTier.DETERMINISTIC_CONFIRMED,
        verifier_kind=VerifierKind.PREBUILT, severity=Severity.A,
        evidence=EvidencePacket(recompute_script="print('WRONG')", expected_output="RIGHT"),
    )
    pre = AuditReport(paper_id="p", findings=[f_keep, f_drop])
    result = {"confirmed": [0], "dropped": [{"idx": 1, "reason": "stdout did not match expected_output"}]}
    out = managed._assemble_from_confirmation(pre, [f_keep, f_drop], result)
    assert [f.verifier_id for f in out.findings] == ["v.keep"]
    assert len(out.dropped_flags) == 1 and out.dropped_flags[0].finding.verifier_id == "v.drop"
    assert schema.validate(out.to_dict(), "audit") == []


def test_confirm_result_parser_tolerates_prose():
    txt = "I ran it. Here is the output:\nLITMUS_CONFIRM {\"confirmed\": [0, 2], \"dropped\": []}\nDone."
    parsed = managed._parse_confirm_result(txt)
    assert parsed == {"confirmed": [0, 2], "dropped": []}
    # Also recover from a fenced/embedded object with no sentinel.
    txt2 = "result: ```json\n{\"confirmed\": [1], \"dropped\": [{\"idx\": 0, \"reason\": \"x\"}]}\n```"
    parsed2 = managed._parse_confirm_result(txt2)
    assert parsed2["confirmed"] == [1] and parsed2["dropped"][0]["idx"] == 0


# ============================================================================
# 3) LIVE managed session (guarded). Honest skip if the beta is not enabled.
# ============================================================================
def _live_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@pytest.mark.live
@pytest.mark.skipif(not _live_enabled(), reason="ANTHROPIC_API_KEY not set — skipping live managed-agents test")
def test_live_managed_session():
    """Create a REAL managed-agents session and run run_managed_audit on the tiny graph. Asserts
    a schema-valid AuditReport with the one expected confirmed flag. If the beta is unavailable on
    this key (404/403/beta error) or the SDK lacks the surface, SKIP with a clear message and the
    captured error — never fake success."""
    try:
        import anthropic  # noqa: F401
    except Exception as exc:
        pytest.skip(f"anthropic SDK not importable: {exc}")

    captured: dict[str, object] = {}

    def _capture(run):
        captured["session_id"] = run.session_id
        captured["error"] = run.error
        captured["transcript_tail"] = (run.transcript or "")[-600:]

    # allow_fallback=True so we always get a report; we then inspect meta.executor to tell whether
    # the managed sandbox actually did the confirmation, and surface the session id / error either way.
    report = run_managed_audit(
        _tiny_graph(),
        registry=_focused_registry(),
        allow_fallback=True,
        timeout_s=300.0,
        on_event=_capture,
    )

    assert isinstance(report, AuditReport)
    assert schema.validate(report.to_dict(), "audit") == []

    executor = report.meta.get("executor")
    if executor != "managed":
        # The managed path didn't confirm (beta off / timeout / API error). That's a SKIP, not a
        # failure — but we print exactly what happened so the run is honest.
        reason = report.meta.get("managed", {}).get("fallback_reason") or captured.get("error")
        pytest.skip(
            f"managed-agents confirmation unavailable on this key — fell back to local. "
            f"session_id={captured.get('session_id')!r} reason={reason!r} "
            f"transcript_tail={captured.get('transcript_tail')!r}"
        )

    # Managed path ran: assert the verdict and surface the session id for the report.
    flags = report.checkable
    assert len(flags) == 1 and flags[0].verifier_id == "sum_check.v1", [f.verifier_id for f in flags]
    assert flags[0].status is Status.FAIL
    assert report.dropped_flags == []  # the one real flag reproduced in the managed sandbox
    assert report.meta["managed"].get("session_id"), report.meta["managed"]
    print(
        "LIVE managed session OK — session_id=",
        report.meta["managed"].get("session_id"),
        "confirmed flags=",
        [f.verifier_id for f in flags],
    )
