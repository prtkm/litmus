// Local fixtures — 3 realistic AuditReports that VALIDATE against
// litmus/schemas/audit.schema.json. These let the UI be real before the
// managed-agents pipeline (WS-H) exists. They are also the default data source
// when no Supabase env vars are configured (see lib/data.ts).

import type { AuditReport } from "@/lib/types";
import yield142 from "./yield-142.json";
import proseTableMismatch from "./prose-table-mismatch.json";
import highRigorClean from "./high-rigor-clean.json";

// Cast through unknown: the JSON is schema-validated at build by
// scripts/validate-fixtures.mjs, so we trust it matches AuditReport here.
export const FIXTURES: AuditReport[] = [
  yield142 as unknown as AuditReport,
  proseTableMismatch as unknown as AuditReport,
  highRigorClean as unknown as AuditReport,
];

export const FIXTURE_BY_ID: Record<string, AuditReport> = Object.fromEntries(
  FIXTURES.map((r) => [r.paper_id, r]),
);
