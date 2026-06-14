# LITMUS — project instructions

LITMUS audits published scientific papers with executable evidence: the LLM extracts/locates
claims; deterministic verifiers render every verdict; each flag ships a runnable recompute script.
Python framework + calibration kernel (`litmus/`), Next.js app (`app/`), Supabase backend.

## ⛔ DEPLOY RULE — READ BEFORE ANY `vercel deploy` (this has burned us twice)

The Vercel project **`litmus`** (`prj_IOGU66iEdMtu2ahtdmpfMAJ94q8J`, team `team_6GqVoFXEikJSPDscM7pMnZQW`)
does **NOT** have the Supabase env vars set as project env vars. So a bare `vercel deploy`
(or `npx vercel deploy`) builds the app in **FIXTURES MODE** — the homepage shows **3 bundled demo
papers instead of the 31 live papers**, and every Supabase-backed path (gallery, paper pages, the
live upload/progress flow) silently breaks. The data is fine in Supabase; the *build* lacks the read
config. This looks like "prod got reverted / data lost." It is the #1 recurring footgun here.

**Every deploy MUST inject these two PUBLIC (RLS-protected, safe to pass) vars at build AND runtime:**

```
NEXT_PUBLIC_SUPABASE_URL                 = https://sgeggibnrsirodhgqzxf.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY     = (the sb_publishable_… anon key; in the shell env)
```

**Always deploy via the wrapper, never a bare `vercel deploy`:**

```sh
cd app
./scripts/deploy.sh            # preview  — verify it shows 31 papers + the upload path
./scripts/deploy.sh --prod     # promote  — only after the preview verified clean
```

The wrapper reads the two `NEXT_PUBLIC_SUPABASE_*` values from the shell env and passes them via
`--build-env` AND `--env`. If you must run the CLI by hand, you MUST include all four flags
(`--build-env X --build-env Y --env X --env Y`).

**Procedure (non-negotiable):**
1. Deploy a **preview** first. Open it. Confirm the homepage says **31 papers** (NOT "3 papers /
   local fixtures") and a paper page loads from Supabase. If it shows fixtures, the env wasn't
   applied — FIX THAT, do not promote.
2. Only then `--prod`.
3. If prod ever shows fixtures: `cd app && npx vercel rollback <last-good-deployment-url> --token "$VERCEL_TOKEN"`.

A permanent fix (do once, with the owner's go-ahead): set those two vars as **project** env vars in
the Vercel dashboard / `vercel env add` for production+preview, after which a bare deploy is safe.
Until then, the wrapper is the only safe path.

## Tests

`pytest tests/` runs in ~22s; live API/managed-agents tests are deselected by default (they make
real Anthropic calls with long timeouts). Run them explicitly with `pytest -m live`
(needs `ANTHROPIC_API_KEY` + the managed-agents beta). See `tests/conftest.py`.

## Managed-agents upload worker

The Vercel app only queues uploads; the multi-minute managed-agents audit runs in a **separate
long-running worker**, not in a Vercel function (functions time out, and the payload limit is
~4.5 MB so large PDFs can't even POST to `/api/upload`). Run the drainer against the prod queue:

```sh
.venv/bin/python -u -m litmus.app_backend.worker --poll --managed --interval 5
```

It creates the coordinator Agent + persona sub-agents + cloud Environment once, then audits each
`status='queued'` paper, streaming progress into `papers.progress` (which the page polls). Use
`-u` (unbuffered) so its log lines flush.
