"""Supabase persistence for the live audit pipeline (WS-H, DESIGN §10, §15).

App-only. The managed-agents executor upserts one ``papers`` row per audited paper and walks
its status through the pipeline lifecycle. We talk to Supabase over its PostgREST REST API with
``httpx`` and the **service-role secret key** (which bypasses RLS — DESIGN §15: "writes happen
server-side from the managed-agents executor using the service role"). No ``supabase-py``
dependency; the REST surface is small and explicit.

The ``papers`` table (supabase/migrations/0001_init.sql):

    id uuid pk | content_hash text unique | doi | title | field | status (default 'queued')
    | claim_graph jsonb | audit_report jsonb | created_at

``content_hash`` is the unique upsert key (DESIGN §2 cache key). Status lifecycle:

    queued → extracting → auditing → confirming → done   (or → error)

A private Storage bucket ``pdfs`` holds the uploaded source PDFs (helpers here can fetch one
to feed the pipeline). This module never raises at import time when env is unset; callers
construct :class:`SupabaseConfig` (typically ``SupabaseConfig.from_env()``) and a
:class:`SupabaseIO` only when they actually need to persist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from litmus.core.claim import ClaimGraph
from litmus.core.provenance import AuditReport

PAPERS_TABLE = "papers"
PDFS_BUCKET = "pdfs"
_REST_TIMEOUT_S = 30.0


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string PostgREST accepts for a ``timestamptz`` column
    (the ``updated_at`` the poller watches — migration 0002)."""
    return datetime.now(timezone.utc).isoformat()


class PaperStatus(str, Enum):
    """The pipeline lifecycle of a paper row (matches the migration's CHECK-free text column)."""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    AUDITING = "auditing"
    CONFIRMING = "confirming"
    DONE = "done"
    ERROR = "error"


# Allowed forward transitions (for callers that want to assert progress). Not enforced on write.
_LIFECYCLE_ORDER = [
    PaperStatus.QUEUED,
    PaperStatus.EXTRACTING,
    PaperStatus.AUDITING,
    PaperStatus.CONFIRMING,
    PaperStatus.DONE,
]


class SupabaseError(RuntimeError):
    """A non-2xx response (or transport failure) from the Supabase REST API."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def paper_row_payload(
    *,
    content_hash: Optional[str] = None,
    doi: Optional[str] = None,
    title: Optional[str] = None,
    field: Optional[str] = None,
    status: Optional[PaperStatus | str] = None,
    claim_graph: Optional[ClaimGraph | dict[str, Any]] = None,
    audit_report: Optional[AuditReport | dict[str, Any]] = None,
    progress: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Build the JSON body for a ``papers`` upsert, matching the table columns exactly.

    Accepts domain objects (``ClaimGraph`` / ``AuditReport``) or already-serialized dicts for the
    jsonb columns, and a ``PaperStatus`` or raw string for status. ``progress`` (the coalesced live
    audit feed — DESIGN §13/§15, migration 0002) and ``error`` / ``updated_at`` are included only
    when passed. Only the keys you pass are included, so this doubles as a partial-update payload
    (e.g. just ``status``). Pure/no I/O — unit-testable without a network.
    """
    payload: dict[str, Any] = {}
    if content_hash is not None:
        payload["content_hash"] = content_hash
    if doi is not None:
        payload["doi"] = doi
    if title is not None:
        payload["title"] = title
    if field is not None:
        payload["field"] = field
    if status is not None:
        payload["status"] = status.value if isinstance(status, PaperStatus) else str(status)
    if claim_graph is not None:
        payload["claim_graph"] = claim_graph.to_dict() if isinstance(claim_graph, ClaimGraph) else claim_graph
    if audit_report is not None:
        payload["audit_report"] = (
            audit_report.to_dict() if isinstance(audit_report, AuditReport) else audit_report
        )
    if progress is not None:
        payload["progress"] = progress
    if error is not None:
        payload["error"] = error
    if updated_at is not None:
        payload["updated_at"] = updated_at
    return payload


@dataclass
class SupabaseConfig:
    """Connection config. Use :meth:`from_env` to read ``SUPABASE_URL`` + ``SUPABASE_SECRET_KEY``."""

    url: str
    secret_key: str

    @classmethod
    def from_env(cls, *, require: bool = True) -> Optional["SupabaseConfig"]:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SECRET_KEY")
        if not (url and key):
            if require:
                raise SupabaseError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set in the environment")
            return None
        return cls(url=url.rstrip("/"), secret_key=key)

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    @property
    def rest_url(self) -> str:
        return f"{self.base_url}/rest/v1"

    @property
    def storage_url(self) -> str:
        return f"{self.base_url}/storage/v1"

    def headers(self, *, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Auth headers for PostgREST. The secret key goes in BOTH ``apikey`` and the bearer
        token (PostgREST expects both); the service role bypasses RLS."""
        h = {
            "apikey": self.secret_key,
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h


class SupabaseIO:
    """Thin REST client for the ``papers`` table + the ``pdfs`` Storage bucket.

    Construct with a :class:`SupabaseConfig` (and optionally inject an ``httpx.Client`` for
    tests). All methods raise :class:`SupabaseError` on a non-2xx response.
    """

    def __init__(self, config: SupabaseConfig, *, client: Any = None) -> None:
        self.config = config
        self._client = client  # an httpx.Client; lazily created if None
        self._owns_client = client is None

    # --- low-level -----------------------------------------------------------
    def _http(self):
        if self._client is None:
            import httpx  # lazy: app-only dependency

            self._client = httpx.Client(timeout=_REST_TIMEOUT_S)
        return self._client

    def _request(self, method: str, url: str, **kw: Any):
        try:
            resp = self._http().request(method, url, **kw)
        except Exception as exc:  # transport-level (DNS, connect, timeout)
            raise SupabaseError(f"transport error calling Supabase: {exc}") from exc
        if resp.status_code >= 300:
            raise SupabaseError(
                f"Supabase {method} {url} -> HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )
        return resp

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> "SupabaseIO":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- papers table --------------------------------------------------------
    def upsert_paper(
        self,
        *,
        content_hash: Optional[str] = None,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        field: Optional[str] = None,
        status: Optional[PaperStatus | str] = None,
        claim_graph: Optional[ClaimGraph | dict[str, Any]] = None,
        audit_report: Optional[AuditReport | dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Upsert one ``papers`` row keyed on ``content_hash`` (DESIGN §2 cache key) and return
        the stored row. Uses PostgREST ``Prefer: resolution=merge-duplicates`` so re-running an
        audit updates the existing row in place rather than erroring on the unique constraint."""
        payload = paper_row_payload(
            content_hash=content_hash,
            doi=doi,
            title=title,
            field=field,
            status=status,
            claim_graph=claim_graph,
            audit_report=audit_report,
        )
        if "content_hash" not in payload:
            raise SupabaseError("upsert_paper requires content_hash (the unique on_conflict key)")
        url = f"{self.config.rest_url}/{PAPERS_TABLE}?on_conflict=content_hash"
        resp = self._request(
            "POST",
            url,
            headers=self.config.headers(
                extra={"Prefer": "resolution=merge-duplicates,return=representation"}
            ),
            json=payload,
        )
        rows = resp.json()
        return rows[0] if isinstance(rows, list) and rows else (rows or {})

    def update_status(
        self, content_hash: str, status: PaperStatus | str, *, error: Optional[str] = None
    ) -> dict[str, Any]:
        """Patch a paper's ``status`` (queued→extracting→auditing→confirming→done | error),
        matched on ``content_hash``, stamping ``updated_at`` so the poller sees movement. ``error``
        (optional) is written to the ``error`` column (added in migration 0002) — e.g. the terminal
        failure message when ``status='error'``. Returns the updated row."""
        status_val = status.value if isinstance(status, PaperStatus) else str(status)
        payload: dict[str, Any] = {"status": status_val, "updated_at": _now_iso()}
        if error is not None:
            payload["error"] = error
        url = f"{self.config.rest_url}/{PAPERS_TABLE}?content_hash=eq.{content_hash}"
        resp = self._request(
            "PATCH",
            url,
            headers=self.config.headers(extra={"Prefer": "return=representation"}),
            json=payload,
        )
        rows = resp.json()
        return rows[0] if isinstance(rows, list) and rows else (rows or {})

    def update_progress(self, content_hash: str, progress: dict[str, Any]) -> dict[str, Any]:
        """Patch ONLY the live ``progress`` jsonb (+ ``updated_at``) for one paper, matched on
        ``content_hash`` (migration 0002). A deliberately small, high-frequency write: the
        managed-agents worker coalesces+throttles streamy events into ``progress`` and calls this
        a few times a second at most, so the gallery/audit page's poll reflects the audit live
        without rewriting the heavy ``claim_graph`` / ``audit_report`` columns. Returns nothing
        useful by design (``Prefer: return=minimal``) to keep the round-trip cheap."""
        payload: dict[str, Any] = {"progress": progress, "updated_at": _now_iso()}
        url = f"{self.config.rest_url}/{PAPERS_TABLE}?content_hash=eq.{content_hash}"
        resp = self._request(
            "PATCH",
            url,
            headers=self.config.headers(extra={"Prefer": "return=minimal"}),
            json=payload,
        )
        try:
            rows = resp.json()
        except Exception:
            return {}
        return rows[0] if isinstance(rows, list) and rows else (rows or {})

    def get_paper(self, content_hash: str) -> Optional[dict[str, Any]]:
        """Fetch a single paper row by ``content_hash``; ``None`` if absent."""
        url = f"{self.config.rest_url}/{PAPERS_TABLE}?content_hash=eq.{content_hash}&limit=1"
        resp = self._request("GET", url, headers=self.config.headers())
        rows = resp.json()
        return rows[0] if isinstance(rows, list) and rows else None

    def list_by_status(
        self, status: PaperStatus | str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return ``papers`` rows in a given ``status``, oldest first (the queue drainer reads
        ``status='queued'``). Ordered by ``created_at`` so the queue is FIFO."""
        status_val = status.value if isinstance(status, PaperStatus) else str(status)
        url = (
            f"{self.config.rest_url}/{PAPERS_TABLE}"
            f"?status=eq.{status_val}&order=created_at.asc&limit={int(limit)}"
        )
        resp = self._request("GET", url, headers=self.config.headers())
        rows = resp.json()
        return rows if isinstance(rows, list) else []

    def persist_audit(
        self,
        *,
        content_hash: str,
        claim_graph: ClaimGraph | dict[str, Any],
        audit_report: AuditReport | dict[str, Any],
        doi: Optional[str] = None,
        title: Optional[str] = None,
        field: Optional[str] = None,
        status: PaperStatus | str = PaperStatus.DONE,
    ) -> dict[str, Any]:
        """Convenience for the executor's final step: upsert the derived ``claim_graph`` +
        ``audit_report`` and mark the row ``done`` in a single round-trip (DESIGN §13 step 5)."""
        return self.upsert_paper(
            content_hash=content_hash,
            doi=doi,
            title=title,
            field=field,
            status=status,
            claim_graph=claim_graph,
            audit_report=audit_report,
        )

    # --- pipeline orchestration (status lifecycle) ---------------------------
    def run_and_persist(
        self,
        graph: ClaimGraph,
        *,
        content_hash: str,
        executor: Any = None,
        doi: Optional[str] = None,
        title: Optional[str] = None,
        field: Optional[str] = None,
        confirm: bool = True,
    ) -> AuditReport:
        """Run the managed audit over ``graph`` and persist it, walking the row's status through
        the pipeline lifecycle (DESIGN §13, §15): ``auditing → confirming → done`` (or ``error``).

        ``executor`` defaults to a :class:`~litmus.pipeline.executor.ManagedAgentExecutor`. The
        ClaimGraph is written up front (so the gallery can show it while the audit runs), then the
        derived ``audit_report`` and final ``done`` status land together. On any failure the row is
        marked ``error`` and the exception re-raised. App-only; imports the executor lazily so the
        framework never depends on it.
        """
        # Seed the row with the extracted claim graph and mark it auditing.
        self.upsert_paper(
            content_hash=content_hash,
            doi=doi,
            title=title,
            field=field,
            status=PaperStatus.AUDITING,
            claim_graph=graph,
        )
        try:
            if executor is None:
                from litmus.pipeline.executor import ManagedAgentExecutor

                executor = ManagedAgentExecutor(confirm=confirm)
            # Confirmation is the final deterministic beat (DESIGN §13.4) — surface it as a status.
            if confirm:
                self.update_status(content_hash, PaperStatus.CONFIRMING)
            report = executor.audit_graph(graph)
        except Exception as exc:
            try:
                self.update_status(content_hash, PaperStatus.ERROR, error=str(exc))
            except Exception:
                pass
            raise
        self.persist_audit(
            content_hash=content_hash,
            claim_graph=graph,
            audit_report=report,
            doi=doi,
            title=title,
            field=field,
            status=PaperStatus.DONE,
        )
        return report

    # --- pdfs storage bucket -------------------------------------------------
    def download_pdf(self, object_path: str) -> bytes:
        """Download a source PDF from the private ``pdfs`` Storage bucket (service role)."""
        url = f"{self.config.storage_url}/object/{PDFS_BUCKET}/{object_path.lstrip('/')}"
        resp = self._request("GET", url, headers=self.config.headers())
        return resp.content

    def upload_pdf(self, object_path: str, data: bytes, *, upsert: bool = True) -> dict[str, Any]:
        """Upload bytes to the private ``pdfs`` bucket (used by the upload path / tests)."""
        url = f"{self.config.storage_url}/object/{PDFS_BUCKET}/{object_path.lstrip('/')}"
        headers = self.config.headers(
            extra={"Content-Type": "application/pdf", "x-upsert": "true" if upsert else "false"}
        )
        resp = self._request("POST", url, headers=headers, content=data)
        try:
            return resp.json()
        except Exception:
            return {"path": object_path}
