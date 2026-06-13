// Server-only Supabase admin client (the WRITE path — DESIGN §2, §15).
//
// The browser/read client (lib/supabase.ts) uses the publishable key + the public
// SELECT RLS policy. It CANNOT insert rows or write to Storage. The upload route
// needs the service-role key, which bypasses RLS — so this module talks to the
// Supabase REST + Storage APIs directly with `fetch` (mirroring the Python
// litmus/app_backend/supabase_io.py). It is imported ONLY from server code
// (app/api/upload/route.ts); never import it into a Client Component — the key
// must never reach the browser.
//
// Env (accepts both the app's documented names and the Python/shell names so the
// route works whichever the deployer has set):
//   URL: SUPABASE_URL | NEXT_PUBLIC_SUPABASE_URL
//   service-role key: SUPABASE_SERVICE_ROLE_KEY | SUPABASE_SECRET_KEY
//
// NOTE: this is server-only by construction — the service-role key is read from a
// non-NEXT_PUBLIC_ env var, so it is never inlined into the client bundle, and this
// module is imported only from the upload Route Handler (server runtime). Do not
// import it into a Client Component.
import type { PaperRow } from "@/lib/supabase";

const PAPERS_TABLE = "papers";
const PDFS_BUCKET = "pdfs";

function readUrl(): string | null {
  return (
    process.env.SUPABASE_URL ||
    process.env.NEXT_PUBLIC_SUPABASE_URL ||
    null
  );
}

function readServiceKey(): string | null {
  return (
    process.env.SUPABASE_SERVICE_ROLE_KEY ||
    process.env.SUPABASE_SECRET_KEY ||
    null
  );
}

/** True when the server has both a Supabase URL and a service-role key configured. */
export function isAdminConfigured(): boolean {
  return Boolean(readUrl() && readServiceKey());
}

interface AdminEnv {
  base: string;
  key: string;
}

function env(): AdminEnv {
  const base = readUrl();
  const key = readServiceKey();
  if (!base || !key) {
    throw new Error(
      "Supabase write path not configured: set SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_URL) and " +
        "SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_SECRET_KEY) in the server environment.",
    );
  }
  return { base: base.replace(/\/+$/, ""), key };
}

function headers(extra?: Record<string, string>): Record<string, string> {
  const { key } = env();
  // PostgREST wants the key in BOTH apikey and the bearer token; the service role bypasses RLS.
  return {
    apikey: key,
    Authorization: `Bearer ${key}`,
    "Content-Type": "application/json",
    ...(extra ?? {}),
  };
}

/** The object path a paper's PDF lives at in the `pdfs` bucket — keyed by content hash so it
 *  matches the worker's PDF_OBJECT_TEMPLATE (`<hash>.pdf`). */
export function pdfObjectPath(contentHash: string): string {
  return `${contentHash}.pdf`;
}

/** sha256 of the uploaded PDF bytes — the DESIGN §2 cache key, identical to the Python loader
 *  (scripts/load_corpus_to_supabase.py) and the worker (content_hash_of). Uses Web Crypto. */
export async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  // Pass the ArrayBuffer straight to Web Crypto. Callers hash `await file.arrayBuffer()`,
  // which is already an ArrayBuffer; taking it directly avoids the strict-lib mismatch
  // between Uint8Array<ArrayBufferLike> and BufferSource.
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Look up an existing paper by content hash (the cache probe). Returns the row or null. */
export async function getPaperByHash(contentHash: string): Promise<PaperRow | null> {
  const { base } = env();
  const url = `${base}/rest/v1/${PAPERS_TABLE}?content_hash=eq.${encodeURIComponent(
    contentHash,
  )}&limit=1`;
  const resp = await fetch(url, { headers: headers(), cache: "no-store" });
  if (!resp.ok) {
    throw new Error(`Supabase getPaperByHash → HTTP ${resp.status}: ${await safeBody(resp)}`);
  }
  const rows = (await resp.json()) as PaperRow[];
  return Array.isArray(rows) && rows.length ? rows[0] : null;
}

/** Upload the PDF bytes to the private `pdfs` Storage bucket (service role, upsert). */
export async function uploadPdf(contentHash: string, bytes: ArrayBuffer): Promise<void> {
  const { base, key } = env();
  const url = `${base}/storage/v1/object/${PDFS_BUCKET}/${pdfObjectPath(contentHash)}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/pdf",
      "x-upsert": "true",
    },
    body: bytes,
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`Supabase uploadPdf → HTTP ${resp.status}: ${await safeBody(resp)}`);
  }
}

interface UpsertFields {
  content_hash: string;
  status?: string;
  title?: string | null;
  field?: string | null;
  doi?: string | null;
}

/** Upsert a `papers` row keyed on content_hash (merge-duplicates) and return the stored row. */
export async function upsertPaper(fields: UpsertFields): Promise<PaperRow> {
  const { base } = env();
  const url = `${base}/rest/v1/${PAPERS_TABLE}?on_conflict=content_hash`;
  const resp = await fetch(url, {
    method: "POST",
    headers: headers({
      Prefer: "resolution=merge-duplicates,return=representation",
    }),
    body: JSON.stringify(fields),
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`Supabase upsertPaper → HTTP ${resp.status}: ${await safeBody(resp)}`);
  }
  const rows = (await resp.json()) as PaperRow[];
  if (!Array.isArray(rows) || !rows.length) {
    throw new Error("Supabase upsertPaper returned no row");
  }
  return rows[0];
}

async function safeBody(resp: Response): Promise<string> {
  try {
    return (await resp.text()).slice(0, 300);
  } catch {
    return "<no body>";
  }
}
