// GET /api/papers/[id]/status — the LIGHTWEIGHT live status of one paper.
//
// Distinct from the full-report GET at /api/papers/[id] (which returns the whole
// AuditReport): this is the poll target for the in-flight page's <LiveProgress>.
// It returns only { id, status, error, progress, hasReport } via the narrow
// getPaperStatus select, so the client can poll it cheaply every few seconds
// without fetching the (large) audit_report on every tick.
//
// Reads through the same data layer as the pages (Supabase when configured, else
// local fixtures). 404 only when there is no such paper row.

import { getPaperStatus } from "@/lib/data";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  try {
    const status = await getPaperStatus(decodeURIComponent(id));
    if (!status) {
      return Response.json({ error: "Paper not found." }, { status: 404 });
    }
    return Response.json(status);
  } catch {
    // Never echo the underlying Supabase/PostgREST error text to the public poller.
    return Response.json({ error: "Status unavailable." }, { status: 500 });
  }
}
