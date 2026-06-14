// The data layer. If NEXT_PUBLIC_SUPABASE_URL + NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY
// are set, papers are read from Supabase; otherwise we fall back to the local
// fixtures (lib/fixtures). Default (no env) = fixtures, so `npm run dev` works
// with nothing configured.
//
// These functions are safe to call from Server Components (the gallery and the
// audit page are RSC) — they only use the publishable key + public SELECT RLS.

import type { AuditReport, PaperStatus, PaperSummary, TrustTier } from "@/lib/types";
import { FIXTURES, FIXTURE_BY_ID } from "@/lib/fixtures";
import { getSupabase, isSupabaseConfigured, type PaperRow } from "@/lib/supabase";
import { categorize } from "@/lib/labels";

const TIER_ORDER: TrustTier[] = [
  "deterministic_confirmed",
  "calibrated_synthesized",
  "advisory_assisted",
  "routed_to_human",
];

/** Derive the gallery card view from a full AuditReport. */
export function summarize(report: AuditReport): PaperSummary {
  const meta = (report.meta ?? {}) as Record<string, unknown>;
  const flags = report.findings.filter((f) => f.status === "fail");

  // Distinct tiers across all findings, in canonical order, so chips render
  // consistently and never collapse deterministic vs advisory (DESIGN §3.6).
  const tierSet = new Set<TrustTier>(report.findings.map((f) => f.trust_tier));
  const trust_tiers = TIER_ORDER.filter((t) => tierSet.has(t));

  const status = ((report.summary as Record<string, unknown> | undefined)?.status ??
    "done") as PaperStatus;

  const cat = categorize(report);
  return {
    id: report.paper_id,
    title: (meta.title as string) ?? report.paper_id,
    field: (meta.field as string) ?? "—",
    doi: (meta.doi as string) ?? null,
    status,
    flag_count: flags.length,
    categories: cat.counts,
    passes: cat.passes,
    reviewed_clean: cat.reviewedClean,
    trust_tiers,
    routed_count: (report.routed_to_human ?? []).length,
  };
}

function rowToReport(row: PaperRow): AuditReport | null {
  if (!row.audit_report) return null;
  const report = row.audit_report as AuditReport;
  // Backfill meta from columns so the gallery has title/field even if the
  // stored audit_report didn't duplicate them.
  report.meta = {
    title: row.title ?? undefined,
    field: row.field ?? undefined,
    doi: row.doi ?? undefined,
    ...(report.meta ?? {}),
  };
  return report;
}

/** All audited papers, as gallery summaries. */
export async function listPapers(): Promise<PaperSummary[]> {
  if (isSupabaseConfigured()) {
    const supabase = getSupabase();
    if (supabase) {
      const { data, error } = await supabase
        .from("papers")
        .select("*")
        .order("created_at", { ascending: false });
      if (error) throw new Error(`Supabase listPapers failed: ${error.message}`);
      return (data as PaperRow[])
        .map(rowToReport)
        .filter((r): r is AuditReport => r !== null)
        .map(summarize);
    }
  }
  return FIXTURES.map(summarize);
}

// A paper id arriving from the URL is one of: a uuid (the `id` column), a sha256 content_hash
// (hex), or a paper_id slug. Slugs include arXiv ids, so a literal '.' is legitimate (e.g.
// "chemistry-arxiv-2604.14784-water-nanodroplet-efields"). The chars that would actually break out
// of a raw PostgREST `.or()` filter are ',', '(', ')' (and whitespace) — NOT '.' — so we allow
// [A-Za-z0-9._-] and reject everything else up front, closing the filter-injection / malformed-
// query-500 hole on the public status endpoint without 404-ing real dotted slugs.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SAFE_ID_RE = /^[A-Za-z0-9._-]+$/;

/** A single audit report by paper id, or null if not found. */
export async function getPaper(id: string): Promise<AuditReport | null> {
  if (isSupabaseConfigured()) {
    const supabase = getSupabase();
    if (supabase) {
      // The gallery routes by report.paper_id (a slug), which lives in
      // audit_report->>paper_id — NOT the uuid `id` column. Comparing a slug to
      // the uuid column makes Postgres throw on the cast, so branch on shape:
      // a uuid hits `id`; anything else matches the paper_id slug (with
      // content_hash as a secondary key).
      const isUuid = UUID_RE.test(id);
      if (!isUuid && !SAFE_ID_RE.test(id)) return null; // not a valid id; never reaches the filter
      const base = supabase.from("papers").select("*").limit(1);
      const query = isUuid
        ? base.eq("id", id)
        : base.or(`content_hash.eq.${id},audit_report->>paper_id.eq.${id}`);
      const { data, error } = await query.maybeSingle();
      if (error) throw new Error(`Supabase getPaper failed: ${error.message}`);
      return data ? rowToReport(data as PaperRow) : null;
    }
  }
  return FIXTURE_BY_ID[id] ?? null;
}

// The lightweight live-status view of a paper row, for the polling endpoint and
// the in-flight <LiveProgress> page. Deliberately NARROW — it never pulls the
// (potentially large) audit_report/claim_graph, only what the live UI needs:
// the pipeline status, any error text, and the worker's progress JSONB. `progress`
// is `unknown` because the worker owns its exact shape (the UI reads it defensively).
export interface PaperStatusInfo {
  id: string;
  status: PaperStatus;
  error: string | null;
  progress: unknown | null;
  // True once the audit_report has a paper_id — i.e. there is a report to render.
  hasReport: boolean;
}

/**
 * Lightweight status for one paper — the poll target for the live page. Selects
 * ONLY id, content_hash, status, error, progress, and audit_report->>paper_id
 * (NOT the full report), and resolves the id the same way getPaper does (a uuid
 * hits the `id` column; a slug/hash matches content_hash or audit_report->>paper_id).
 * Returns null when no row exists. In fixtures mode every paper is already `done`.
 */
export async function getPaperStatus(id: string): Promise<PaperStatusInfo | null> {
  if (isSupabaseConfigured()) {
    const supabase = getSupabase();
    if (supabase) {
      const isUuid = UUID_RE.test(id);
      if (!isUuid && !SAFE_ID_RE.test(id)) return null; // reject unsafe ids before the .or() filter
      // NARROW select — no audit_report/claim_graph payload, just the status fields
      // plus paper_id (aliased) so we can report whether a report exists yet.
      const base = supabase
        .from("papers")
        .select("id, content_hash, status, error, progress, paper_id:audit_report->>paper_id")
        .limit(1);
      const query = isUuid
        ? base.eq("id", id)
        : base.or(`content_hash.eq.${id},audit_report->>paper_id.eq.${id}`);
      const { data, error } = await query.maybeSingle();
      if (error) throw new Error(`Supabase getPaperStatus failed: ${error.message}`);
      if (!data) return null;
      const row = data as {
        id: string;
        status: string;
        error: string | null;
        progress: unknown | null;
        paper_id: string | null;
      };
      return {
        id: row.id,
        status: row.status as PaperStatus,
        error: row.error ?? null,
        progress: row.progress ?? null,
        hasReport: Boolean(row.paper_id),
      };
    }
  }
  // Fixtures: a fixture exists ⇒ it's a finished (done) report; otherwise null.
  const fixture = FIXTURE_BY_ID[id];
  if (!fixture) return null;
  return {
    id: fixture.paper_id,
    status: "done",
    error: null,
    progress: null,
    hasReport: true,
  };
}

/** Whether the live data source (Supabase) is active, for UI hints. */
export function usingLiveData(): boolean {
  return isSupabaseConfigured();
}
