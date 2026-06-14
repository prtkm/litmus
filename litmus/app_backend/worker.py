"""The live upload → audit → store worker (DESIGN §2 cache, §13 pipeline, §15 — WS-H).

This is the queue drainer behind the web app's upload path. The Next.js route
(``app/app/api/upload/route.ts``) is serverless and cannot run the multi-minute audit
inline, so it only stages work: it computes the PDF's content hash (the §2 cache key),
uploads the bytes to the private ``pdfs`` Storage bucket, and upserts a ``papers`` row with
``status='queued'``. THIS worker picks those rows up and runs the real pipeline:

    extract (Opus native PDF → ClaimGraph)  →  audit (LocalExecutor)  →  persist + status='done'

walking the row's ``status`` through ``queued → extracting → auditing → confirming → done``
(or ``error``) so the gallery's live status reflects progress.

Two entry points:

    python -m litmus.app_backend.worker <pdf> [--paper-id ID] [--no-store]
        one-off: audit a local PDF and (by default) upsert the result to Supabase.

    python -m litmus.app_backend.worker --poll [--interval 5] [--once] [--limit N]
        the drainer: repeatedly select ``status='queued'`` rows, fetch each PDF from the
        ``pdfs`` bucket, and audit it. Run this (or a managed-agents session) alongside the
        app to process queued uploads.

It uses :class:`~litmus.pipeline.executor.LocalExecutor` (subprocess/thread workers + direct
Opus calls for extraction — DESIGN §15). The hosted ``ManagedAgentExecutor`` is the other
surface of the same pipeline; this worker is the local/owner-driven drainer the design allows
("no separate worker host" — §15) and is what makes a re-upload of any corpus paper a cache hit.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from litmus.app_backend.supabase_io import (
    PaperStatus,
    SupabaseConfig,
    SupabaseError,
    SupabaseIO,
)
from litmus.core.claim import ClaimGraph
from litmus.core.provenance import AuditReport
from litmus.extract.extractor import default_paper_id, extract_claim_graph
from litmus.pipeline.executor import LocalExecutor, ManagedAgentExecutor

# Same object-key convention the upload route writes to in the ``pdfs`` bucket.
PDF_OBJECT_TEMPLATE = "{content_hash}.pdf"


def content_hash_of(pdf_bytes: bytes) -> str:
    """sha256 of the raw PDF bytes — the DESIGN §2 cache key, identical to the corpus loader
    (``scripts/load_corpus_to_supabase.py``) so a re-upload of any audited paper is a cache hit."""
    return hashlib.sha256(pdf_bytes).hexdigest()


# Research-field words a corpus-style id prefix can start with (``chemistry-foo-21`` → chemistry).
_FIELD_WORDS = {
    "nutrition", "psychology", "health", "chemistry", "biology", "medicine",
    "economics", "physics", "ml", "econ", "neuroscience", "materials", "engineering",
}


def _humanize(stem: str) -> str:
    """Turn a filename stem into a passable title when the claim graph carries no ``title``.

    Mirrors the corpus loader's humanizer closely enough to be recognizable: strip a citekey
    token (``smith21``) and a trailing ``pdf`` (from ``foo.pdf`` → stem ``foo-pdf``), drop a leading
    field word, title-case the rest. Falls back to the stem.
    """
    toks = [
        t
        for t in re.split(r"[-_\s]+", stem)
        if t and t.lower() != "pdf" and not re.match(r"^[a-z]+\d{2,4}[a-z]?$", t)
    ]
    if toks and toks[0].lower() in _FIELD_WORDS:
        toks = toks[1:]
    return " ".join(w.capitalize() for w in toks) or stem


def _meta_field(stem: str) -> str:
    """Best-effort ``field`` from a corpus-style id prefix (``chemistry-foo-21`` → ``chemistry``).

    For an arbitrary UPLOAD filename the first token is meaningless (``real-1810-…`` → "real",
    ``understanding-ionic-…`` → "understanding"), so only trust it when it is a known field word.
    Otherwise emit a neutral em-dash the gallery renders cleanly, never a garbage field chip."""
    head = re.split(r"[-_]", stem)[0].lower()
    return head if head in _FIELD_WORDS else "—"


def _bib_from_graph(graph: ClaimGraph, *, fallback_stem: str) -> dict[str, Optional[str]]:
    """Pull bibliographic ``title`` / ``field`` / ``doi`` from the claim graph's ``meta`` (the
    extractor may record them), falling back to filename-derived values so a row always has a
    human title in the gallery."""
    meta = graph.meta or {}
    title = meta.get("title") or _humanize(fallback_stem)
    field = meta.get("field") or _meta_field(fallback_stem)
    doi = meta.get("doi")
    return {"title": title, "field": field, "doi": doi}


# --- live progress feed (managed mode) ---------------------------------------
# Coarse % the page can render as a bar. The managed-agents stream doesn't carry a true
# completion fraction, so we map event kinds to monotonic-ish milestones (and never regress).
_STEP_PCT = {
    "extracting": 10,
    "auditing": 25,
    "agent_started": 30,
    "tool_use": 45,
    "tool_result": 60,
    "persona": 70,
    "classification": 90,
    "confirming": 92,
    "done": 100,
    "error": 100,
}
_PROGRESS_MIN_INTERVAL_S = 0.75  # throttle: at most ~1.3 writes/sec to the `progress` jsonb
_PROGRESS_EVENTS_KEEP = 12       # rolling tail of recent events kept in the feed
# The pipeline stages the page's StageStrip can render (must match app/lib/labels PIPELINE_STAGES).
# A mid-stream `step` is often a managed event kind (tool_use/persona/…) — those are sub-steps of
# 'auditing', so failed_step is normalized to one of these before it's stored.
_PIPELINE_STEPS = frozenset({"queued", "extracting", "auditing", "confirming"})

# String fields the frontend's eventText() scans for a renderable line. If an event carries none of
# these (as a non-empty string), it would render "(no detail)" — so _push_event synthesizes a `text`.
_EVENT_TEXT_FIELDS = ("text", "message", "label", "name", "summary", "tool", "persona", "value")


def _event_has_text(ev: dict[str, Any]) -> bool:
    return any(isinstance(ev.get(f), str) and ev[f].strip() for f in _EVENT_TEXT_FIELDS)


def _event_text(kind: str, ev: dict[str, Any]) -> str:
    """A plain-language one-liner for a structured event kind, so no feed row is blank. Reads the
    fields _push_event flattened onto ``ev`` (the worker owns this shape end to end)."""
    if kind == "classification":
        order = ("correct", "flaggable", "subjective", "routed_to_human")
        parts = [f"{ev[k]} {k.replace('_', ' ')}" for k in order if isinstance(ev.get(k), int)]
        return " · ".join(parts)
    if kind == "agent_started":
        pid = ev.get("paper_id")
        return f"audit session started{f' · {pid}' if pid else ''}"
    if kind == "status":
        sr = ev.get("stop_reason")
        return f"coordinator idle: {sr}" if sr else "coordinator idle"
    if kind == "done":
        s = ev.get("summary")
        if isinstance(s, dict):
            return f"{s.get('n_flags', 0)} flag(s) · {s.get('n_routed_to_human', 0)} routed to human"
        return "audit complete"
    if "value" in ev:  # defensive: a bare scalar payload
        return str(ev["value"])
    return ""


def _public_error(exc: BaseException) -> str:
    """A short, sanitized failure line safe to expose to anonymous clients — ``papers.error`` is
    anon-readable and rendered verbatim on the page. Drops URLs / file paths / internal detail
    (which can carry Supabase storage paths, content hashes, class/file names); the full exception
    stays in the worker logs only."""
    return f"The audit could not be completed ({type(exc).__name__})."


class _ProgressSink:
    """Coalesce the managed-agents event stream into one small, throttled ``progress`` jsonb.

    DESIGN §13/§15, migration 0002. The frontend polls ``papers.progress``; this turns a chatty SSE
    stream into a single rolling document and rate-limits the DB writes:

      * ``on_event(kind, payload)`` — the sink handed to ``run_managed_audit``. It updates an
        in-memory ``{step, pct, events:[…last ~12…], seq, executor}`` and writes to Supabase at most
        once per ``_PROGRESS_MIN_INTERVAL_S``. Raw streamy ``message`` chunks are NOT written
        verbatim every tick — they're summarized into ``step``/``last_message`` only; the structured
        ``tool_use`` / ``tool_result`` / ``status`` / ``classification`` events are what populate the
        feed. Never raises (a progress write must never sink the audit).
      * ``finish(report)`` / ``fail(msg)`` — terminal frames (``step='done'`` / an ``error`` field),
        written immediately (throttle bypassed) so a polling page lands on a final state.

    The exact shape is the integration contract with the frontend track — see ``snapshot``.
    """

    def __init__(self, io: Optional[SupabaseIO], content_hash: Optional[str], *, executor: str) -> None:
        self._io = io
        self._chash = content_hash
        self._executor = executor
        self._seq = 0
        self._step = "queued"
        self._pct = 0
        self._events: list[dict[str, Any]] = []
        self._last_message: Optional[str] = None
        self._classification: Optional[dict[str, Any]] = None
        self._failed_step: Optional[str] = None
        self._last_write = 0.0

    # -- public hooks ---------------------------------------------------------
    def set_executor(self, executor: Optional[str]) -> None:
        """Record which executor actually ran the audit (``"managed"`` vs ``"managed:fallback-local"``,
        read off ``report.meta`` AFTER the run). Lets the terminal frame carry the honest label so the
        live page never claims a managed agent ran when the audit degraded to local (DESIGN: never
        overstate)."""
        if executor:
            self._executor = executor

    def on_event(self, kind: str, payload: Any) -> None:
        """Fold one managed-agents event into the rolling snapshot and maybe flush it (throttled)."""
        try:
            self._fold(kind, payload)
            self._maybe_write()
        except Exception:
            # Defensive: the live feed is best-effort; never propagate into the audit.
            pass

    def note_step(self, step: str) -> None:
        """Record a pipeline-lifecycle step (e.g. 'extracting') in the feed even before the managed
        stream opens. Best-effort, throttled like any other event."""
        try:
            self._set_step(step)
            self._maybe_write()
        except Exception:
            pass

    def finish(self, report: Any = None) -> None:
        """Terminal success frame (``step='done'``), written immediately (throttle bypassed)."""
        try:
            self._set_step("done")
            summary = None
            try:
                summary = report.summary() if report is not None else None
            except Exception:
                summary = None
            self._push_event("done", {"summary": summary} if summary else {})
            self._write(force=True, extra={"summary": summary} if summary else None)
        except Exception:
            pass

    def fail(self, message: str) -> None:
        """Terminal error frame: an ``error`` field on the snapshot, written immediately. Preserves
        the last in-flight pipeline step as ``failed_step`` BEFORE overwriting it to 'error', so the
        page can mark which stage failed (PIPELINE_STAGES has no 'error' entry)."""
        try:
            if self._step not in ("error", "done"):
                # Normalize to a stage the page can render (mid-stream `step` is often a managed
                # event kind like tool_use/persona — all sub-steps of 'auditing').
                self._failed_step = self._step if self._step in _PIPELINE_STEPS else "auditing"
            self._set_step("error")
            self._push_event("error", {"text": message[:300]})
            self._write(force=True, extra={"error": message[:500]})
        except Exception:
            pass

    # -- snapshot (THE integration contract) ----------------------------------
    def snapshot(self, *, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """The exact ``progress`` jsonb document written to the row. Field names are load-bearing —
        the frontend reads them. See the structured summary's integration_notes."""
        snap: dict[str, Any] = {
            "step": self._step,
            "pct": self._pct,
            "seq": self._seq,
            "executor": self._executor,
            "events": list(self._events[-_PROGRESS_EVENTS_KEEP:]),
        }
        if self._last_message:
            snap["last_message"] = self._last_message
        if self._classification is not None:
            snap["classification"] = self._classification
        if self._failed_step:
            snap["failed_step"] = self._failed_step
        if extra:
            snap.update(extra)
        return snap

    # -- internals ------------------------------------------------------------
    def _fold(self, kind: str, payload: Any) -> None:
        if kind == "message":
            # Do NOT push raw streamy chunks into the events feed on every tick — keep only a short
            # rolling tail of the latest prose as `last_message`, and let it nudge the step.
            text = payload if isinstance(payload, str) else str(payload)
            text = text.strip()
            if text:
                self._last_message = text[-280:]
            return
        if kind == "classification":
            if isinstance(payload, dict):
                self._classification = payload
            self._set_step("classification")
            self._push_event(kind, payload)
            return
        if kind == "status":
            # session.status_idle stop_reason.type — informative, advances nothing by itself.
            self._push_event(kind, {"stop_reason": payload})
            return
        # agent_started / tool_use / tool_result / persona → structured feed + step bump.
        self._set_step(kind)
        self._push_event(kind, payload)

    def _push_event(self, kind: str, payload: Any) -> None:
        self._seq += 1
        ev: dict[str, Any] = {"seq": self._seq, "kind": kind}
        if isinstance(payload, dict):
            ev.update({k: v for k, v in payload.items() if k != "seq"})
        elif payload is not None:
            ev["value"] = payload if isinstance(payload, (str, int, float, bool)) else str(payload)
        # Guarantee a human-readable line for the frontend feed. Structured kinds (counts, ids,
        # bare scalars) otherwise render as "(no detail)" because the UI scans only string fields —
        # so synthesize a `text` summary here, the single source of truth for the progress shape.
        if not _event_has_text(ev):
            text = _event_text(kind, ev)
            if text:
                ev["text"] = text
        self._events.append(ev)
        if len(self._events) > _PROGRESS_EVENTS_KEEP * 2:
            self._events = self._events[-_PROGRESS_EVENTS_KEEP:]

    def _set_step(self, step: str) -> None:
        self._step = step
        pct = _STEP_PCT.get(step)
        if pct is not None and pct > self._pct:
            self._pct = pct  # monotonic — the bar never goes backwards

    def _maybe_write(self) -> None:
        now = time.monotonic()
        if now - self._last_write >= _PROGRESS_MIN_INTERVAL_S:
            self._write()

    def _write(self, *, force: bool = False, extra: Optional[dict[str, Any]] = None) -> None:
        if self._io is None or not self._chash:
            return
        if not force:
            # _maybe_write already gated on the interval; just stamp the clock here too.
            pass
        self._last_write = time.monotonic()
        try:
            self._io.update_progress(self._chash, self.snapshot(extra=extra))
        except Exception:
            # SupabaseError or any transport hiccup must not interrupt the audit.
            pass


def _make_progress_sink(
    io: Optional[SupabaseIO], content_hash: Optional[str], *, executor: str
) -> _ProgressSink:
    """Construct the throttled live-progress sink for one paper (managed mode). Kept tiny so the
    default LocalExecutor path never pays for it."""
    return _ProgressSink(io, content_hash, executor=executor)


def process_pdf(
    pdf_path: str | os.PathLike[str],
    *,
    paper_id: Optional[str] = None,
    store: bool = True,
    content_hash: Optional[str] = None,
    doi: Optional[str] = None,
    registry: Any = None,
    model: str = "claude-opus-4-8",
    io: Optional[SupabaseIO] = None,
    confirm: bool = True,
    on_status: Any = None,
    managed: bool = False,
    resources: Any = None,
) -> AuditReport:
    """Extract then audit a single PDF, optionally persisting the result to Supabase.

    The pipeline is the §13 one, split so the row's ``status`` can advance as it runs:

        extracting → auditing → (confirming) → done

    Args:
        pdf_path: path to the source PDF on disk.
        paper_id: id stamped on the claim graph + report; defaults to the filename stem.
        store: when True (default), upsert a ``papers`` row keyed on ``content_hash`` with the
            extracted ``claim_graph`` + derived ``audit_report`` and ``status='done'`` (DESIGN
            §2/§10). When False, no Supabase access — just returns the report (used by the CLI's
            ``--no-store`` and by unit tests).
        content_hash: the §2 cache key; computed from the PDF bytes if not supplied. Pass the
            hash the row was queued under (the drainer does) so the upsert lands on the same row.
        doi: optional DOI to record (the upload form may collect it); claim-graph meta wins if set.
        registry: a verifier :class:`~litmus.commons.registry.Registry`; the executor builds the
            default one if omitted.
        model: extraction model id (the only model-in-the-loop step — DESIGN §11).
        io: a :class:`SupabaseIO` to reuse; one is constructed from the environment if ``store``
            and none is given.
        confirm: run the fresh-context confirmation beat (DESIGN §13.4). Surfaced as a status.
        on_status: optional ``callable(status: PaperStatus)`` hook fired on each transition
            (handy for tests / logging without a database).
        managed: when True, audit inside a Claude managed-agents coordinator session
            (:class:`~litmus.pipeline.executor.ManagedAgentExecutor`) and stream its live events
            (tool calls, persona review, per-claim classification) into the row's ``progress``
            jsonb (throttled). DEFAULT False → the unchanged in-process
            :class:`~litmus.pipeline.executor.LocalExecutor` path. ``allow_fallback=True`` so a
            managed outage still yields a report via the local pipeline (DESIGN §15).
        resources: optional pre-created :class:`~litmus.pipeline.managed.ManagedResources` to reuse
            across papers (the drainer creates the coordinator + personas + environment ONCE and
            threads it here) — ignored unless ``managed``.

    Returns the :class:`~litmus.core.provenance.AuditReport`.
    """
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")
    pid = paper_id or default_paper_id(str(pdf))
    chash = content_hash or content_hash_of(pdf.read_bytes())

    if store and io is None:
        io = SupabaseIO(SupabaseConfig.from_env())

    # The live progress sink (managed mode only): closes over `io` + `chash`, coalesces the streamy
    # managed-agents events into one small rolling dict, and throttles writes to `progress` jsonb.
    # Defensive/non-throwing — managed.py swallows callback exceptions, but we also guard here so a
    # progress write can never sink the audit.
    progress_sink = _make_progress_sink(io if store else None, chash, executor="managed") if managed else None
    on_event = progress_sink.on_event if progress_sink is not None else None

    def _emit(status: PaperStatus, *, claim_graph: Any = None, **extra: Any) -> None:
        if on_status is not None:
            on_status(status)
        # Reflect the lifecycle step in the live feed before the managed stream opens (managed only).
        if progress_sink is not None:
            progress_sink.note_step(status.value if isinstance(status, PaperStatus) else str(status))
        if store and io is not None:
            try:
                if claim_graph is not None or extra:
                    io.upsert_paper(content_hash=chash, status=status, claim_graph=claim_graph, **extra)
                else:
                    io.update_status(chash, status)
            except SupabaseError:
                # A status write should never sink the audit; the final persist is what matters.
                pass

    try:
        # 1) Extraction (Opus native PDF → ClaimGraph). DESIGN §11/§13 step 1.
        _emit(PaperStatus.EXTRACTING)
        graph = extract_claim_graph(str(pdf), model=model, paper_id=pid)
        bib = _bib_from_graph(graph, fallback_stem=pid)
        if doi and not bib["doi"]:
            bib["doi"] = doi

        # 2) Audit the extracted graph (planner + verifiers + confirm). DESIGN §13 steps 2-4.
        #    Write the claim graph up front (status='auditing') so the gallery can show it while
        #    the audit runs, mirroring SupabaseIO.run_and_persist.
        _emit(PaperStatus.AUDITING, claim_graph=graph, title=bib["title"], field=bib["field"], doi=bib["doi"])
        if managed:
            # Hosted coordinator + persona panel + deterministic verifier tools (DESIGN §13, §15).
            # allow_fallback=True so a managed outage degrades to the local pipeline, not an error.
            # on_event drives the live `progress` feed; the kwargs pass through verbatim to
            # run_managed_audit (ManagedAgentExecutor forwards *args/**kwargs).
            executor: Any = ManagedAgentExecutor(
                confirm=confirm,
                allow_fallback=True,
                on_event=on_event,
                timeout_s=900.0,
                model=model,
                resources=resources,
            )
        else:
            executor = LocalExecutor(confirm=confirm)
        if confirm and not managed:
            # Local path only: surface the confirm beat as a distinct status. In MANAGED mode the
            # confirmation runs host-side INSIDE the coordinator (the confirm_recompute tool), not as
            # a pre-audit phase — emitting it here would pin progress at 92% for the whole stream
            # (pct is monotonic) and sit the row in 'confirming'. Let the streamed events own pct.
            _emit(PaperStatus.CONFIRMING)
        report = executor.audit_graph(graph, registry)
        # Reflect the executor that ACTUALLY ran (managed vs managed:fallback-local) so the live
        # terminal frame is honest if the managed beta was unavailable and it degraded to local.
        if progress_sink is not None:
            progress_sink.set_executor(str((getattr(report, "meta", None) or {}).get("executor") or ""))

        # 3) Persist the derived report + mark done (DESIGN §13 step 5). One round-trip.
        if store and io is not None:
            io.persist_audit(
                content_hash=chash,
                claim_graph=graph,
                audit_report=report,
                title=bib["title"],
                field=bib["field"],
                doi=bib["doi"],
                status=PaperStatus.DONE,
            )
            # Final live-progress beat so a polling page lands on a terminal 'done' frame even if it
            # never caught the streamed events (managed mode only; best-effort).
            if progress_sink is not None:
                progress_sink.finish(report)
        if on_status is not None:
            on_status(PaperStatus.DONE)
        return report
    except Exception as exc:
        # Mark the row errored so the UI stops showing a perpetual "auditing" (best effort), and
        # record the failure in the live progress feed so a polling page sees it without the report.
        if store and io is not None:
            # Write a SANITIZED message to the anon-readable error column (the page renders it
            # verbatim) — the full exception is preserved in the worker logs by the queue drainer.
            safe = _public_error(exc)
            try:
                io.update_status(chash, PaperStatus.ERROR, error=safe)
            except SupabaseError:
                pass
            if progress_sink is not None:
                progress_sink.fail(safe)
        raise


def process_one_row(
    row: dict[str, Any],
    *,
    io: SupabaseIO,
    registry: Any = None,
    model: str = "claude-opus-4-8",
    confirm: bool = True,
    managed: bool = False,
    resources: Any = None,
) -> AuditReport:
    """Audit a single queued ``papers`` row: fetch its PDF from the ``pdfs`` bucket and run
    :func:`process_pdf`, persisting back under the SAME ``content_hash`` the row was queued with.

    The upload route stores the object at ``<content_hash>.pdf``; if the row carries no
    ``content_hash`` we cannot locate the PDF, so the row is marked ``error``. ``managed`` /
    ``resources`` pass through to :func:`process_pdf` (the hosted coordinator path + its live
    progress feed); the default is the unchanged in-process pipeline.
    """
    chash = row.get("content_hash")
    pid = row.get("id") or chash or "queued-paper"
    if not chash:
        io.update_status(str(pid), PaperStatus.ERROR, error="row has no content_hash; cannot locate PDF")
        raise SupabaseError(f"queued row {pid} has no content_hash")

    object_path = PDF_OBJECT_TEMPLATE.format(content_hash=chash)
    data = io.download_pdf(object_path)

    # Prefer a clean paper_id: the stored title slugged, else the content hash.
    title = (row.get("title") or "").strip()
    paper_id = _slug(title) if title else chash
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        return process_pdf(
            tmp.name,
            paper_id=paper_id,
            store=True,
            content_hash=chash,
            doi=row.get("doi"),
            registry=registry,
            model=model,
            io=io,
            confirm=confirm,
            managed=managed,
            resources=resources,
        )


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "paper"


def process_queue(
    *,
    io: Optional[SupabaseIO] = None,
    registry: Any = None,
    model: str = "claude-opus-4-8",
    confirm: bool = True,
    limit: int = 100,
    log: Any = print,
    managed: bool = False,
    resources: Any = None,
) -> int:
    """Drain the queue once: audit every ``status='queued'`` paper currently in the table.

    Returns the number of rows processed (whether they succeeded or errored). Each row is
    isolated — one failing paper does not stop the rest. ``managed`` / ``resources`` pass through to
    :func:`process_one_row` so every drained paper runs in the hosted coordinator session reusing
    the SAME control-plane handles (the drainer creates them once — see :func:`poll`).
    """
    own = io is None
    if io is None:
        io = SupabaseIO(SupabaseConfig.from_env())
    processed = 0
    try:
        rows = io.list_by_status(PaperStatus.QUEUED, limit=limit)
        if not rows:
            return 0
        log(f"[worker] draining {len(rows)} queued paper(s)…")
        for row in rows:
            label = (row.get("title") or row.get("content_hash") or row.get("id") or "?")
            try:
                log(f"[worker] auditing {label} …")
                report = process_one_row(
                    row, io=io, registry=registry, model=model, confirm=confirm,
                    managed=managed, resources=resources,
                )
                s = report.summary()
                log(f"[worker]   done: {s['n_flags']} flag(s), {s['n_routed_to_human']} routed.")
            except Exception as exc:  # keep draining the rest of the queue
                log(f"[worker]   ERROR on {label}: {exc}")
            processed += 1
        return processed
    finally:
        if own:
            io.close()


def _ensure_managed_resources_once(*, model: str, log: Any) -> Any:
    """Create the managed-agents control plane (coordinator Agent + 5 persona sub-agents +
    Environment) ONCE for the lifetime of the drainer, so we don't recreate them per paper
    (SKILL.md gotcha #2). Returns a :class:`~litmus.pipeline.managed.ManagedResources` (or ``None``
    if it can't be built — each row then self-provisions / falls back, never crashing the drainer).

    Honors ``LITMUS_AGENT_ID`` / ``LITMUS_ENVIRONMENT_ID``: ``ensure_resources`` reuses those
    instead of creating fresh handles when they're set."""
    try:
        from litmus.pipeline.managed import ensure_resources
        from litmus.pipeline.managed import _client as _managed_client

        res = ensure_resources(_managed_client(), model=model)
        log(
            f"[worker] managed control plane ready: agent={res.agent_id} env={res.environment_id} "
            f"personas={len(res.persona_agent_ids)} (reused across the queue)."
        )
        return res
    except Exception as exc:  # control-plane setup failed — degrade gracefully, per-row fallback
        log(f"[worker] WARN: could not pre-create managed resources ({exc}); each row will self-provision.")
        return None


def poll(
    *,
    interval: float = 5.0,
    once: bool = False,
    limit: int = 100,
    registry: Any = None,
    model: str = "claude-opus-4-8",
    confirm: bool = True,
    log: Any = print,
    managed: bool = False,
) -> None:
    """Poll the queue forever (or once with ``once=True``), sleeping ``interval`` seconds between
    empty passes. Ctrl-C exits cleanly. This is the long-running drainer the app's README points
    at: ``python -m litmus.app_backend.worker --poll``.

    When ``managed`` is set every drained paper is audited inside a Claude managed-agents
    coordinator session and its live trace streamed into the row's ``progress`` jsonb. The
    coordinator Agent + persona sub-agents + Environment are created ONCE here and threaded into
    each row (SKILL.md gotcha #2 — don't recreate the control plane per paper). The DEFAULT
    (``managed=False``) is the unchanged in-process LocalExecutor drainer."""
    io = SupabaseIO(SupabaseConfig.from_env())
    resources = _ensure_managed_resources_once(model=model, log=log) if managed else None
    mode = "managed coordinator" if managed else "local"
    log(f"[worker] polling Supabase queue (interval={interval}s, once={once}, mode={mode}). Ctrl-C to stop.")
    try:
        while True:
            n = process_queue(
                io=io, registry=registry, model=model, confirm=confirm, limit=limit, log=log,
                managed=managed, resources=resources,
            )
            if once:
                return
            if n == 0:
                time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        log("\n[worker] stopped.")
    finally:
        io.close()


# --- CLI ---------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m litmus.app_backend.worker",
        description="LITMUS live upload→audit→store worker (DESIGN §2/§13/§15).",
    )
    p.add_argument("pdf", nargs="?", help="audit a single local PDF (one-off mode).")
    p.add_argument("--poll", action="store_true", help="drain the Supabase 'queued' papers queue.")
    p.add_argument("--once", action="store_true", help="with --poll: make a single pass, then exit.")
    p.add_argument("--interval", type=float, default=5.0, help="with --poll: seconds between empty passes.")
    p.add_argument("--limit", type=int, default=100, help="with --poll: max rows per pass.")
    p.add_argument("--paper-id", default=None, help="one-off: paper id (defaults to the filename stem).")
    p.add_argument("--no-store", action="store_true", help="one-off: do NOT write to Supabase; just print.")
    p.add_argument("--no-confirm", action="store_true", help="skip fresh-context confirmation (faster; less safe).")
    p.add_argument("--model", default="claude-opus-4-8", help="extraction model id.")
    p.add_argument(
        "--managed",
        action="store_true",
        help="audit via the Claude managed-agents coordinator session and stream its live trace "
        "into the row's progress (also enabled by LITMUS_MANAGED=1). Default: in-process LocalExecutor.",
    )
    args = p.parse_args(argv)

    confirm = not args.no_confirm
    managed = args.managed or os.getenv("LITMUS_MANAGED", "").lower() in ("1", "true", "yes")

    if args.poll:
        poll(
            interval=args.interval, once=args.once, limit=args.limit, model=args.model,
            confirm=confirm, managed=managed,
        )
        return 0

    if not args.pdf:
        p.error("provide a PDF path (one-off mode) or --poll (queue drainer mode).")

    report = process_pdf(
        args.pdf,
        paper_id=args.paper_id,
        store=not args.no_store,
        model=args.model,
        confirm=confirm,
        managed=managed,  # one-off: resources=None (self-provisions / env vars); see ensure_resources
    )
    s = report.summary()
    where = "not stored (--no-store)" if args.no_store else "stored to Supabase (status='done')"
    print(
        f"[worker] {report.paper_id}: {s['n_flags']} flag(s), "
        f"{s['n_routed_to_human']} routed-to-human, {s['n_dropped']} dropped — {where}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
