// Browser/server Supabase client. Reads the public URL + publishable key from
// env. Both the gallery and the audit pages are public in the first cut (no
// auth — DESIGN §15), so the publishable (anon) key with a public SELECT RLS
// policy is all the read path needs.
//
// This module never throws at import time when env is absent: callers (lib/data.ts)
// check `isSupabaseConfigured()` first and fall back to local fixtures so that
// `npm run dev` works with nothing configured.

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

export function isSupabaseConfigured(): boolean {
  return Boolean(url && key);
}

let cached: SupabaseClient | null = null;

/** Returns a Supabase client, or null when env vars are not configured. */
export function getSupabase(): SupabaseClient | null {
  if (!isSupabaseConfigured()) return null;
  if (!cached) {
    cached = createClient(url as string, key as string, {
      auth: { persistSession: false },
    });
  }
  return cached;
}

// Row shape of the `papers` table (see supabase/migrations/0001_init.sql).
export interface PaperRow {
  id: string;
  content_hash: string | null;
  doi: string | null;
  title: string | null;
  field: string | null;
  status: string;
  claim_graph: unknown | null;
  audit_report: unknown | null;
  created_at: string;
}
