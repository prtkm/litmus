"""App backend helpers (WS-H) — Supabase persistence for the live audit pipeline.

App-only (DESIGN §15): the framework, study, and gate never import this. The managed-agents
executor writes the derived ``claim_graph`` + ``audit_report`` (and status transitions) back to
the Supabase ``papers`` table via the REST API using the service-role secret key.
"""

from litmus.app_backend.supabase_io import (
    PaperStatus,
    SupabaseConfig,
    SupabaseError,
    SupabaseIO,
    paper_row_payload,
)

__all__ = [
    "PaperStatus",
    "SupabaseConfig",
    "SupabaseError",
    "SupabaseIO",
    "paper_row_payload",
]
