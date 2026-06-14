-- LITMUS app backend · live progress for the managed-agents auditor (DESIGN §13, §15, WS-H).
-- The managed-agents executor streams the coordinator session (tool calls, persona review,
-- per-claim classification) and coalesces it into a small `progress` jsonb the gallery/audit
-- page polls. `error` carries a terminal failure message; `updated_at` lets the poller cheaply
-- detect movement. Idempotent (add-only, `if not exists`) — safe to re-run.
--
-- TRANSPORT IS POLLING. `public.papers` is intentionally NOT added to the `supabase_realtime`
-- publication: the frontend polls these columns on an interval (DESIGN §15) rather than
-- subscribing to Realtime. Do not `alter publication supabase_realtime add table public.papers`.

alter table public.papers add column if not exists progress jsonb;
alter table public.papers add column if not exists error text;
alter table public.papers add column if not exists updated_at timestamptz not null default now();

comment on column public.papers.progress is
  'Live audit progress (coalesced, throttled) written by the managed-agents worker: '
  '{step, pct, events:[...], seq, executor}. Polled by the page — papers is intentionally NOT '
  'in the supabase_realtime publication (transport is polling, DESIGN §15).';
comment on column public.papers.error is
  'Terminal failure message when status=error (best-effort, set by the worker).';
comment on column public.papers.updated_at is
  'Last write time; lets the poller detect movement without diffing the whole row.';
