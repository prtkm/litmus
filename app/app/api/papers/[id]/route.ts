// GET /api/papers/[id] — the audit report for one paper as JSON.
//
// Reads through the same data layer as the pages (Supabase when configured,
// else local fixtures). A small read-only endpoint so the audit report is
// consumable programmatically, not only via the rendered card page.

import { getPaper } from "@/lib/data";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  const report = await getPaper(decodeURIComponent(id));
  if (!report) {
    return Response.json({ error: "Paper not found." }, { status: 404 });
  }
  return Response.json(report);
}
