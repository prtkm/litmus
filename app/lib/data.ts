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
      const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);
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

/** Whether the live data source (Supabase) is active, for UI hints. */
export function usingLiveData(): boolean {
  return isSupabaseConfigured();
}
