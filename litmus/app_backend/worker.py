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
from litmus.pipeline.executor import LocalExecutor

# Same object-key convention the upload route writes to in the ``pdfs`` bucket.
PDF_OBJECT_TEMPLATE = "{content_hash}.pdf"


def content_hash_of(pdf_bytes: bytes) -> str:
    """sha256 of the raw PDF bytes — the DESIGN §2 cache key, identical to the corpus loader
    (``scripts/load_corpus_to_supabase.py``) so a re-upload of any audited paper is a cache hit."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def _humanize(stem: str) -> str:
    """Turn a filename stem into a passable title when the claim graph carries no ``title``.

    Mirrors the corpus loader's humanizer closely enough to be recognizable: strip a citekey
    token (``smith21``), drop a leading field word, title-case the rest. Falls back to the stem.
    """
    toks = [t for t in re.split(r"[-_\s]+", stem) if t and not re.match(r"^[a-z]+\d{2,4}[a-z]?$", t)]
    fields = {
        "nutrition", "psychology", "health", "chemistry", "biology", "medicine",
        "economics", "physics", "ml", "econ",
    }
    if toks and toks[0].lower() in fields:
        toks = toks[1:]
    return " ".join(w.capitalize() for w in toks) or stem


def _meta_field(stem: str) -> str:
    """Best-effort ``field`` from the id prefix (``chemistry-foo-21`` → ``chemistry``)."""
    head = re.split(r"[-_]", stem)[0]
    return head or "unknown"


def _bib_from_graph(graph: ClaimGraph, *, fallback_stem: str) -> dict[str, Optional[str]]:
    """Pull bibliographic ``title`` / ``field`` / ``doi`` from the claim graph's ``meta`` (the
    extractor may record them), falling back to filename-derived values so a row always has a
    human title in the gallery."""
    meta = graph.meta or {}
    title = meta.get("title") or _humanize(fallback_stem)
    field = meta.get("field") or _meta_field(fallback_stem)
    doi = meta.get("doi")
    return {"title": title, "field": field, "doi": doi}


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

    Returns the :class:`~litmus.core.provenance.AuditReport`.
    """
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")
    pid = paper_id or default_paper_id(str(pdf))
    chash = content_hash or content_hash_of(pdf.read_bytes())

    if store and io is None:
        io = SupabaseIO(SupabaseConfig.from_env())

    def _emit(status: PaperStatus, *, claim_graph: Any = None, **extra: Any) -> None:
        if on_status is not None:
            on_status(status)
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
        executor = LocalExecutor(confirm=confirm)
        if confirm:
            _emit(PaperStatus.CONFIRMING)
        report = executor.audit_graph(graph, registry)

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
        if on_status is not None:
            on_status(PaperStatus.DONE)
        return report
    except Exception as exc:
        # Mark the row errored so the UI stops showing a perpetual "auditing" (best effort).
        if store and io is not None:
            try:
                io.update_status(chash, PaperStatus.ERROR, error=str(exc))
            except SupabaseError:
                pass
        raise


def process_one_row(
    row: dict[str, Any],
    *,
    io: SupabaseIO,
    registry: Any = None,
    model: str = "claude-opus-4-8",
    confirm: bool = True,
) -> AuditReport:
    """Audit a single queued ``papers`` row: fetch its PDF from the ``pdfs`` bucket and run
    :func:`process_pdf`, persisting back under the SAME ``content_hash`` the row was queued with.

    The upload route stores the object at ``<content_hash>.pdf``; if the row carries no
    ``content_hash`` we cannot locate the PDF, so the row is marked ``error``.
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
) -> int:
    """Drain the queue once: audit every ``status='queued'`` paper currently in the table.

    Returns the number of rows processed (whether they succeeded or errored). Each row is
    isolated — one failing paper does not stop the rest.
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
                report = process_one_row(row, io=io, registry=registry, model=model, confirm=confirm)
                s = report.summary()
                log(f"[worker]   done: {s['n_flags']} flag(s), {s['n_routed_to_human']} routed.")
            except Exception as exc:  # keep draining the rest of the queue
                log(f"[worker]   ERROR on {label}: {exc}")
            processed += 1
        return processed
    finally:
        if own:
            io.close()


def poll(
    *,
    interval: float = 5.0,
    once: bool = False,
    limit: int = 100,
    registry: Any = None,
    model: str = "claude-opus-4-8",
    confirm: bool = True,
    log: Any = print,
) -> None:
    """Poll the queue forever (or once with ``once=True``), sleeping ``interval`` seconds between
    empty passes. Ctrl-C exits cleanly. This is the long-running drainer the app's README points
    at: ``python -m litmus.app_backend.worker --poll``."""
    io = SupabaseIO(SupabaseConfig.from_env())
    log(f"[worker] polling Supabase queue (interval={interval}s, once={once}). Ctrl-C to stop.")
    try:
        while True:
            n = process_queue(io=io, registry=registry, model=model, confirm=confirm, limit=limit, log=log)
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
    args = p.parse_args(argv)

    confirm = not args.no_confirm

    if args.poll:
        poll(interval=args.interval, once=args.once, limit=args.limit, model=args.model, confirm=confirm)
        return 0

    if not args.pdf:
        p.error("provide a PDF path (one-off mode) or --poll (queue drainer mode).")

    report = process_pdf(
        args.pdf,
        paper_id=args.paper_id,
        store=not args.no_store,
        model=args.model,
        confirm=confirm,
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
