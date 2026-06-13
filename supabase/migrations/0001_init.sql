-- LITMUS app backend (DESIGN §10, §15, WS-H).
-- One table of audited papers + their structured outputs (claim graph + audit
-- report as jsonb). Public, read-only gallery in the first cut: no auth, a
-- single public SELECT policy (DESIGN §15). Writes happen server-side from the
-- managed-agents executor using the service role, which bypasses RLS.

create extension if not exists "pgcrypto";

create table if not exists public.papers (
  id            uuid primary key default gen_random_uuid(),
  content_hash  text unique,                 -- §2 cache key (content hash / DOI)
  doi           text,
  title         text,
  field         text,
  status        text not null default 'queued', -- queued|extracting|auditing|confirming|done|error
  claim_graph   jsonb,                        -- validates against claim.schema.json
  audit_report  jsonb,                        -- validates against audit.schema.json
  created_at    timestamptz not null default now()
);

create index if not exists papers_created_at_idx on public.papers (created_at desc);
create index if not exists papers_status_idx on public.papers (status);

-- Row Level Security: on, with a public read policy for the gallery.
alter table public.papers enable row level security;

-- Public can read every paper (public gallery, no auth — DESIGN §15).
drop policy if exists "papers public read" on public.papers;
create policy "papers public read"
  on public.papers
  for select
  to anon, authenticated
  using (true);

-- NOTE (Storage): the upload path (WS-H) needs a Storage bucket named "pdfs"
-- to hold uploaded source PDFs (and SI). Create it in the Supabase dashboard or
-- via the CLI:
--   insert into storage.buckets (id, name, public) values ('pdfs', 'pdfs', false);
-- Keep it private; the pipeline reads it with the service role. PDFs are the
-- source the managed-agents pipeline ingests; the derived claim_graph +
-- audit_report land back in this table.

comment on table public.papers is
  'LITMUS audited papers. claim_graph/audit_report are schema-validated jsonb. Source PDFs live in the private Storage bucket "pdfs".';
