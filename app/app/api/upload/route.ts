// POST /api/upload — accept a paper PDF, cache-check it, and stage it for audit
// (DESIGN §2 content-hash cache + §13 upload→audit→store, WS-H).
//
// Flow:
//   1. Validate the multipart body (PDF, non-empty, ≤ 50 MB).
//   2. Compute sha256 of the bytes = content_hash — the §2 cache key, identical to
//      the corpus loader (scripts/load_corpus_to_supabase.py) and the worker
//      (litmus/app_backend/worker.py content_hash_of).
//   3. CACHE HIT: if a papers row already exists for that hash, return its audit
//      immediately ({ id, status, cached:true }) — "the first upload pays the cost;
//      every later view is instant" (DESIGN §2). Re-uploading any of the already-
//      audited corpus papers returns its report instantly.
//   4. CACHE MISS: upload the PDF to the private "pdfs" Storage bucket and upsert a
//      papers row { content_hash, status:'queued', title: filename }. Return
//      { id, status:'queued' }. This route does NOT run the multi-minute audit
//      inline (serverless time limits) — a worker drains the queue:
//
//          python -m litmus.app_backend.worker --poll
//
//      (or a Claude managed-agents session). It selects status='queued' rows,
//      fetches each PDF from the bucket, and runs extract → audit → persist,
//      advancing status queued → extracting → auditing → confirming → done.
//
// With no service-role credentials configured, the route degrades to a local-only
// acknowledgement (so `npm run dev` works with zero env) and says so.

import { randomUUID } from "node:crypto";
import type { PaperRow } from "@/lib/supabase";
import {
  getPaperByHash,
  isAdminConfigured,
  sha256Hex,
  uploadPdf,
  upsertPaper,
} from "@/lib/supabase-admin";

// Never prerendered: this is a mutation endpoint that reads the request body.
export const dynamic = "force-dynamic";
// The audit pipeline + Supabase writes are Node-only; pin the runtime.
export const runtime = "nodejs";

const MAX_BYTES = 50 * 1024 * 1024; // 50 MB ceiling for an uploaded PDF.

// The link the gallery uses to reach an audit: the report's paper_id slug if present
// (lib/data.ts getPaper resolves it via audit_report->>paper_id), else content_hash,
// else the row uuid. All three resolve in getPaper, so any non-null one works.
function reportId(row: PaperRow): string {
  const slug = (row.audit_report as { paper_id?: string } | null)?.paper_id;
  return slug || row.content_hash || row.id;
}

export async function POST(request: Request) {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return Response.json(
      { error: "Expected a multipart/form-data body with a `file` field." },
      { status: 400 },
    );
  }

  const file = form.get("file");
  const doi = (form.get("doi") as string | null)?.trim() || null;

  if (!(file instanceof File)) {
    return Response.json({ error: "Missing `file` upload." }, { status: 400 });
  }
  if (file.size === 0) {
    return Response.json({ error: "Uploaded file is empty." }, { status: 400 });
  }
  if (file.size > MAX_BYTES) {
    return Response.json(
      { error: `File too large (${(file.size / 1024 / 1024).toFixed(1)} MB); max is 50 MB.` },
      { status: 413 },
    );
  }
  const isPdf =
    file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
  if (!isPdf) {
    return Response.json(
      { error: "Only PDF uploads are accepted." },
      { status: 415 },
    );
  }

  // Read the bytes once and hash them — the §2 cache key.
  const bytes = await file.arrayBuffer();
  const contentHash = await sha256Hex(bytes);

  // No write-path credentials: degrade gracefully so local dev still has a round-trip.
  if (!isAdminConfigured()) {
    return Response.json(
      {
        status: "queued",
        id: randomUUID(),
        content_hash: contentHash,
        cached: false,
        filename: file.name,
        bytes: file.size,
        configured: false,
        message:
          "Received, but the Supabase write path is not configured on the server (set " +
          "SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY). Nothing was persisted; no audit was queued.",
      },
      { status: 202 },
    );
  }

  try {
    // 1) Cache probe (DESIGN §2). A hit returns the existing audit instantly.
    const existing = await getPaperByHash(contentHash);
    if (existing) {
      return Response.json(
        {
          id: reportId(existing),
          status: existing.status, // typically 'done'; could be in-flight if mid-audit
          cached: true,
          content_hash: contentHash,
          title: existing.title,
          message:
            existing.status === "done"
              ? "Already audited — served from cache. Open the report below."
              : `This paper is already in the pipeline (status: ${existing.status}).`,
        },
        { status: 200 },
      );
    }

    // 2) Cache miss → stage the work. Upload the source PDF, then queue a row.
    await uploadPdf(contentHash, bytes);
    const row = await upsertPaper({
      content_hash: contentHash,
      status: "queued",
      title: file.name,
      doi,
    });

    return Response.json(
      {
        id: reportId(row),
        status: "queued",
        cached: false,
        content_hash: contentHash,
        doi,
        filename: file.name,
        bytes: file.size,
        message:
          "Queued for audit. A worker (python -m litmus.app_backend.worker --poll) will " +
          "extract, audit, and confirm it; this page reflects its live status.",
      },
      { status: 202 },
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return Response.json(
      { error: `Upload failed: ${message}` },
      { status: 502 },
    );
  }
}

// Be explicit that only POST is supported (GET/others → 405 with a hint).
export function GET() {
  return Response.json(
    { error: "Use POST with a multipart form (file, optional doi) to queue a paper." },
    { status: 405 },
  );
}
