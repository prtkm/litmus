// POST /api/upload — accept a paper PDF and "queue" it (DESIGN §2, WS-H).
//
// STUB. This handler validates the multipart body and returns
// {status:"queued", id} so the upload UI has a real round-trip. It does NOT yet
// run the audit: persisting the PDF to the "pdfs" Storage bucket, inserting the
// papers row, and kicking off the managed-agents extraction → audit → confirm
// pipeline are the WS-H work (see TODO below).

import { randomUUID } from "node:crypto";

// Never prerendered: this is a mutation endpoint that reads the request body.
export const dynamic = "force-dynamic";

const MAX_BYTES = 50 * 1024 * 1024; // 50 MB ceiling for an uploaded PDF.

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

  // A provisional id for the queued paper. WS-H will replace this with the row
  // id from the papers insert (keyed on the content hash / DOI for dedupe).
  const id = randomUUID();

  // TODO(WS-H — managed-agents pipeline): wire the real audit flow here.
  //   1. Stream `file` into the private "pdfs" Storage bucket.
  //   2. Compute the content hash; upsert a papers row (content_hash, doi,
  //      status='queued') using the service-role client (server-only key).
  //      Return the existing row's id on a cache hit instead of re-auditing.
  //   3. Trigger the managed-agents session: extract → audit → confirm, writing
  //      claim_graph + audit_report back to the row and advancing `status`
  //      (queued → extracting → auditing → confirming → done).
  // Until then we just acknowledge receipt so the UI has a real round-trip.

  return Response.json(
    {
      status: "queued",
      id,
      doi,
      filename: file.name,
      bytes: file.size,
      message:
        "Received. The audit pipeline (WS-H) is not wired up in this build, so this paper stays queued — no claim graph or audit report is produced yet.",
    },
    { status: 202 },
  );
}

// Be explicit that only POST is supported (GET/others → 405 with a hint).
export function GET() {
  return Response.json(
    { error: "Use POST with a multipart form (file, optional doi) to queue a paper." },
    { status: 405 },
  );
}
