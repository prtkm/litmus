#!/usr/bin/env bash
# Safe Vercel deploy for the LITMUS app.
#
# WHY THIS EXISTS: the `litmus` Vercel project has NO Supabase env vars set, so a bare
# `vercel deploy` builds in FIXTURES mode — the homepage shows 3 demo papers instead of the 31 live
# ones and every Supabase path breaks (this has reverted prod twice). This wrapper ALWAYS injects
# the two PUBLIC (RLS-protected) NEXT_PUBLIC_SUPABASE_* vars at build AND runtime so the deploy is
# connected to Supabase. See ../../CLAUDE.md.
#
# Usage:
#   ./scripts/deploy.sh           # preview deploy (verify 31 papers, then promote)
#   ./scripts/deploy.sh --prod    # production deploy
# Note: no `-u` — macOS bash 3.2 errors on an empty `"${TARGET[@]}"` under set -u. The explicit
# `: "${VAR:?...}"` checks below still guard every required variable.
set -eo pipefail

: "${VERCEL_TOKEN:?VERCEL_TOKEN must be set in the environment}"
# Reads (client + server): public, RLS-protected — safe to inline.
: "${NEXT_PUBLIC_SUPABASE_URL:?NEXT_PUBLIC_SUPABASE_URL must be set in the environment}"
: "${NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY:?NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY must be set in the environment}"
# Writes (server only, the /api/upload path): SUPABASE_URL + the service-role secret. Passed via
# --env (runtime), NEVER --build-env, so the secret is not inlined into the client bundle.
: "${SUPABASE_URL:?SUPABASE_URL must be set in the environment}"
: "${SUPABASE_SECRET_KEY:?SUPABASE_SECRET_KEY must be set in the environment}"

cd "$(dirname "$0")/.."   # the app/ dir (linked to the Vercel project)

TARGET=()
if [[ "${1:-}" == "--prod" || "${1:-}" == "prod" || "${1:-}" == "production" ]]; then
  TARGET=(--prod)
  echo "▶ PRODUCTION deploy — make sure a preview already verified 31 papers."
else
  echo "▶ preview deploy — open the URL and confirm it shows 31 papers (NOT '3 papers / local fixtures')."
fi

exec npx --yes vercel@latest deploy "${TARGET[@]}" \
  --build-env NEXT_PUBLIC_SUPABASE_URL="$NEXT_PUBLIC_SUPABASE_URL" \
  --build-env NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY="$NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY" \
  --env NEXT_PUBLIC_SUPABASE_URL="$NEXT_PUBLIC_SUPABASE_URL" \
  --env NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY="$NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY" \
  --env SUPABASE_URL="$SUPABASE_URL" \
  --env SUPABASE_SECRET_KEY="$SUPABASE_SECRET_KEY" \
  --token "$VERCEL_TOKEN" --yes
