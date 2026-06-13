"""The live upload→audit→store worker + the §2 content-hash cache (WS-H, DESIGN §2/§13/§15).

Mostly deterministic — no network, no API. The single live test (real Opus extraction over a
corpus PDF) is guarded behind ANTHROPIC_API_KEY and SKIPS (never fakes) when it's absent.

What's covered:
  * the §2 cache key: ``content_hash_of`` == sha256 of the PDF bytes, byte-identical to the
    corpus loader (scripts/load_corpus_to_supabase.py) — this is what makes a re-upload a cache hit;
  * ``process_pdf`` running a hand-built ClaimGraph's audit (extractor monkeypatched), walking
    status queued→extracting→auditing→confirming→done, and persisting via a fake SupabaseIO;
  * the cache-probe / queue-select REST contracts (``get_paper`` / ``list_by_status``);
  * the queue drainer (``process_one_row`` / ``process_queue``): fetch PDF from the bucket, audit,
    persist, and keep draining past a failing row;
  * the route's cache-hit response shape (the JSON the worker-side data supports).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from litmus.app_backend import worker
from litmus.app_backend.supabase_io import PaperStatus, SupabaseConfig, SupabaseIO
from litmus.commons.registry import Registry
from litmus.core import schema
from litmus.core.claim import (
    Claim,
    ClaimGraph,
    EpistemicTier,
    Evidence,
    EvidenceKind,
    Location,
)
from litmus.core.provenance import AuditReport
from litmus.core.finding import Status
from litmus.verifiers import percent_change, sum_check, yield_check


# --- shared fixtures ---------------------------------------------------------
def _focused_registry() -> Registry:
    reg = Registry()
    reg.register_all(sum_check.VERIFIERS + percent_change.VERIFIERS + yield_check.VERIFIERS)
    return reg


def _claim(cid, tier, conf, ev_id):
    return Claim(
        id=cid,
        text=f"claim {cid}",
        location=Location(section="test", quote=f"q{cid}"),
        epistemic_tier=tier,
        confidence=conf,
        evidence_refs=[ev_id],
    )


def _crafted_graph(paper_id="worker-test") -> ClaimGraph:
    """One real FAIL (sum 97≠100), one PASS, one routed-to-human — exercises the full report."""
    evidence = [
        Evidence(id="e_sum", kind=EvidenceKind.TABLE, extracted_values={"parts": [12, 25, 60], "reported_total": 100}),
        Evidence(id="e_ok", kind=EvidenceKind.TABLE, extracted_values={"parts": [40, 35, 25], "reported_total": 100}),
        Evidence(id="e_subj", kind=EvidenceKind.TEXT, extracted_values={}),
    ]
    claims = [
        _claim("c_sum", EpistemicTier.T0, 0.9, "e_sum"),
        _claim("c_ok", EpistemicTier.T0, 0.9, "e_ok"),
        _claim("c_subj", EpistemicTier.T8, 0.9, "e_subj"),
    ]
    return ClaimGraph(
        paper_id=paper_id,
        claims=claims,
        evidence=evidence,
        meta={"title": "A Crafted Paper", "field": "chemistry"},
    )


class _RecordingIO:
    """A SupabaseIO stand-in that records every write and serves a download — no network.

    Implements the small surface ``process_pdf`` / ``process_one_row`` touch:
    ``upsert_paper`` / ``update_status`` / ``persist_audit`` / ``download_pdf`` / ``close``.
    """

    def __init__(self, pdf_bytes: bytes = b""):
        self.pdf_bytes = pdf_bytes
        self.statuses: list[str] = []
        self.upserts: list[dict] = []
        self.persisted: dict | None = None
        self.closed = False

    def upsert_paper(self, **kw):
        if kw.get("status") is not None:
            s = kw["status"]
            self.statuses.append(s.value if isinstance(s, PaperStatus) else str(s))
        self.upserts.append(kw)
        return {"id": "row-uuid", "content_hash": kw.get("content_hash"), "status": "queued"}

    def update_status(self, content_hash, status, *, error=None):
        self.statuses.append(status.value if isinstance(status, PaperStatus) else str(status))
        return {"content_hash": content_hash, "status": str(status)}

    def persist_audit(self, **kw):
        self.persisted = kw
        s = kw.get("status", PaperStatus.DONE)
        self.statuses.append(s.value if isinstance(s, PaperStatus) else str(s))
        return {"id": "row-uuid", "status": "done"}

    def download_pdf(self, object_path):
        return self.pdf_bytes

    def close(self):
        self.closed = True


# ============================================================================
# 1) The §2 cache key — content_hash_of == the loader's sha256 of the PDF bytes.
# ============================================================================
def test_content_hash_is_sha256_of_pdf_bytes():
    data = b"%PDF-1.4 fake bytes for hashing"
    assert worker.content_hash_of(data) == hashlib.sha256(data).hexdigest()


def test_content_hash_matches_corpus_loader_for_a_real_pdf():
    """The cache only hits if the worker hashes a PDF exactly like the loader did. Verify against
    a real corpus PDF if one is present (the loader: sha256 of open(path,'rb').read())."""
    pdfs = sorted(Path("study/corpus/pdfs").glob("*.pdf"))
    if not pdfs:
        pytest.skip("no corpus PDFs present to cross-check the cache key")
    p = pdfs[0]
    loader_hash = hashlib.sha256(p.read_bytes()).hexdigest()
    assert worker.content_hash_of(p.read_bytes()) == loader_hash
    assert worker.PDF_OBJECT_TEMPLATE.format(content_hash=loader_hash) == f"{loader_hash}.pdf"


# ============================================================================
# 2) process_pdf — audit a hand-built graph (extractor monkeypatched), no store.
# ============================================================================
def _patch_extractor(monkeypatch, graph: ClaimGraph):
    """Replace the Opus extraction with a deterministic hand-built graph (so process_pdf runs
    end to end without the API). Patches the name the worker imported."""
    monkeypatch.setattr(worker, "extract_claim_graph", lambda *a, **k: graph)


def test_process_pdf_no_store_returns_real_audit(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 not actually parsed (extractor is patched)")
    _patch_extractor(monkeypatch, _crafted_graph())

    seen: list[str] = []
    report = worker.process_pdf(
        pdf,
        store=False,
        registry=_focused_registry(),
        on_status=lambda s: seen.append(s.value),
    )

    # One confirmed flag (sum 97≠100), a PASS, and the subjective claim routed.
    assert any(f.status is Status.FAIL for f in report.findings)
    assert any(f.status is Status.PASS for f in report.findings)
    assert len(report.routed_to_human) == 1
    assert schema.validate(report.to_dict(), "audit") == []

    # Status walked the lifecycle, ending at done (DESIGN §2 live status).
    assert seen[0] == "extracting"
    assert "auditing" in seen and "confirming" in seen
    assert seen[-1] == "done"


def test_process_pdf_no_store_never_touches_supabase(tmp_path, monkeypatch):
    """store=False must not construct a SupabaseIO or read env — a pure local audit."""
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    _patch_extractor(monkeypatch, _crafted_graph())

    def _boom(*a, **k):  # SupabaseConfig.from_env / SupabaseIO must NOT be called
        raise AssertionError("store=False must not access Supabase")

    monkeypatch.setattr(worker.SupabaseConfig, "from_env", classmethod(lambda cls, **k: _boom()))
    report = worker.process_pdf(pdf, store=False, registry=_focused_registry())
    assert report.paper_id == "worker-test"


# ============================================================================
# 3) process_pdf — with a (fake) SupabaseIO: status transitions + persisted row.
# ============================================================================
def test_process_pdf_stores_audit_and_walks_status(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    chash = worker.content_hash_of(pdf.read_bytes())
    _patch_extractor(monkeypatch, _crafted_graph())

    io = _RecordingIO()
    report = worker.process_pdf(
        pdf, store=True, io=io, content_hash=chash, registry=_focused_registry()
    )

    # Persisted the derived report under the SAME content hash, status='done'.
    assert io.persisted is not None
    assert io.persisted["content_hash"] == chash
    assert io.persisted["status"] is PaperStatus.DONE
    assert io.persisted["audit_report"] is report
    # Title/field came off the claim-graph meta (DESIGN §10) → the gallery has a real title.
    assert io.persisted["title"] == "A Crafted Paper"
    assert io.persisted["field"] == "chemistry"

    # Full lifecycle reached the DB, finishing at done.
    assert io.statuses[0] == "extracting"
    assert io.statuses[-1] == "done"
    assert {"extracting", "auditing", "confirming", "done"} <= set(io.statuses)


def test_process_pdf_marks_error_on_extraction_failure(tmp_path, monkeypatch):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    def _raise(*a, **k):
        raise RuntimeError("extraction blew up")

    monkeypatch.setattr(worker, "extract_claim_graph", _raise)
    io = _RecordingIO()
    with pytest.raises(RuntimeError):
        worker.process_pdf(pdf, store=True, io=io, content_hash="h")
    assert io.statuses[-1] == "error"


# ============================================================================
# 4) The cache-probe / queue-select REST contracts (get_paper / list_by_status).
# ============================================================================
def test_get_paper_cache_probe_contract():
    """The cache hit hinges on get_paper(content_hash): GET papers filtered by content_hash."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return [{"id": "u", "content_hash": "h", "status": "done", "audit_report": {"paper_id": "slug"}}]

    class _FakeHttp:
        def request(self, method, url, **kw):
            captured.update(method=method, url=url, **kw)
            return _Resp()

    io = SupabaseIO(SupabaseConfig(url="https://proj.supabase.co", secret_key="k"), client=_FakeHttp())
    row = io.get_paper("h")
    assert row["status"] == "done"
    assert captured["method"] == "GET"
    assert "content_hash=eq.h" in captured["url"]


def test_list_by_status_selects_queued_fifo():
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return [{"id": "u1", "content_hash": "h1", "status": "queued"}]

    class _FakeHttp:
        def request(self, method, url, **kw):
            captured.update(method=method, url=url, **kw)
            return _Resp()

    io = SupabaseIO(SupabaseConfig(url="https://proj.supabase.co", secret_key="k"), client=_FakeHttp())
    rows = io.list_by_status(PaperStatus.QUEUED, limit=10)
    assert rows and rows[0]["status"] == "queued"
    assert captured["method"] == "GET"
    assert "status=eq.queued" in captured["url"]
    assert "order=created_at.asc" in captured["url"]  # FIFO drain


# ============================================================================
# 5) The queue drainer — process_one_row / process_queue.
# ============================================================================
def test_process_one_row_downloads_pdf_and_persists(monkeypatch):
    pdf_bytes = b"%PDF-1.4 queued paper"
    chash = worker.content_hash_of(pdf_bytes)
    _patch_extractor(monkeypatch, _crafted_graph("queued-paper"))
    io = _RecordingIO(pdf_bytes=pdf_bytes)

    row = {"id": "row-uuid", "content_hash": chash, "title": "My Paper", "status": "queued"}
    report = worker.process_one_row(row, io=io, registry=_focused_registry())

    assert io.persisted is not None
    assert io.persisted["content_hash"] == chash  # persisted under the queued hash
    assert io.persisted["status"] is PaperStatus.DONE
    assert any(f.status is Status.FAIL for f in report.findings)


def test_process_queue_keeps_draining_past_a_failure(monkeypatch):
    """One bad row (no content_hash → cannot locate the PDF) must not stop the rest."""
    good_bytes = b"%PDF-1.4 good"
    chash = worker.content_hash_of(good_bytes)
    _patch_extractor(monkeypatch, _crafted_graph("good"))
    io = _RecordingIO(pdf_bytes=good_bytes)
    io.list_by_status = lambda status, limit=100: [  # type: ignore[assignment]
        {"id": "bad", "content_hash": None, "title": "Bad", "status": "queued"},
        {"id": "good", "content_hash": chash, "title": "Good", "status": "queued"},
    ]

    logs: list[str] = []
    n = worker.process_queue(io=io, registry=_focused_registry(), log=logs.append)
    assert n == 2  # both rows attempted
    assert io.persisted is not None and io.persisted["content_hash"] == chash  # the good one landed
    assert any("ERROR" in line for line in logs)  # the bad one was logged, not raised


# ============================================================================
# 6) The route cache-hit response SHAPE (the JSON contract the page consumes).
#    The TS route builds this from a papers row; assert the worker-side data is
#    sufficient and the shape the upload page expects is satisfiable.
# ============================================================================
def test_cache_hit_response_shape_is_buildable_from_a_row():
    """A 'done' papers row carries everything /api/upload returns on a cache hit:
    an id the gallery can route to (audit_report.paper_id slug), status, cached."""
    row = {
        "id": "11111111-2222-3333-4444-555555555555",
        "content_hash": "deadbeef",
        "status": "done",
        "title": "Cached Paper",
        "audit_report": AuditReport(paper_id="cached-paper").to_dict(),
    }
    # Mirror the route's reportId(): prefer audit_report.paper_id, then content_hash, then uuid.
    report_id = (row["audit_report"] or {}).get("paper_id") or row["content_hash"] or row["id"]
    resp = {"id": report_id, "status": row["status"], "cached": True, "content_hash": row["content_hash"]}
    assert resp == {"id": "cached-paper", "status": "done", "cached": True, "content_hash": "deadbeef"}
    # The page links to /paper/<id>; a non-empty id is what makes that link resolve.
    assert resp["id"] and resp["cached"] is True


# ============================================================================
# 7) LIVE: real Opus extraction over a corpus PDF (guarded; SKIPS without a key).
# ============================================================================
@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
    reason="no ANTHROPIC_API_KEY — skipping the live extraction path (never faked)",
)
def test_process_pdf_live_extraction_no_store():
    """End-to-end over a real PDF: Opus extracts → LocalExecutor audits → a schema-valid report.
    No Supabase (store=False). SKIPS (never fakes) when the key is absent."""
    pdfs = sorted(Path("study/corpus/pdfs").glob("*.pdf"))
    if not pdfs:
        pytest.skip("no corpus PDF available for the live extraction test")
    report = worker.process_pdf(pdfs[0], store=False)
    assert isinstance(report, AuditReport)
    assert schema.validate(report.to_dict(), "audit") == []
    assert report.paper_id  # a paper_id was stamped
