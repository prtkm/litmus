// LITMUS data contract — TypeScript mirror of litmus/schemas/audit.schema.json
// and claim.schema.json. These are the shapes the UI renders. The schemas are
// LOCKED (DESIGN §14); keep this file in lock-step with them.

export type FindingStatus = "pass" | "fail" | "inconclusive" | "error";

// DESIGN §3.6 / P6 — trust tiers never collapse. Ordered high → low.
export type TrustTier =
  | "deterministic_confirmed"
  | "calibrated_synthesized"
  | "advisory_assisted"
  | "routed_to_human";

export type VerifierKind = "prebuilt" | "templated" | "synthesized" | "assisted";

// Issue categories for the at-a-glance summary (DESIGN §4, §5). BOTH the
// deterministic catch (quantitative) and the non-deterministic review
// (overclaim / method / plausibility / integrity / subjective) are first-class —
// the card and verdict show a count per category, never one undifferentiated number.
export type IssueCategory =
  | "quantitative" // deterministic, reproducible numeric error (a FAIL finding)
  | "overclaim" // claim reaches past its evidence (T4)
  | "method" // method appropriateness (T3)
  | "plausibility" // domain-plausibility concern
  | "integrity" // research-integrity screening signal (T7)
  | "subjective"; // significance / novelty — not scored (T8)

// Severity A (most severe) → C. Null only on non-fail findings.
export type Severity = "A" | "B" | "C";

// Evidence is `type: object` in the schema (open), but for `fail` findings the
// schema REQUIRES recompute_script + expected_output. We model the union of
// fields the UI consumes; recompute_script/expected_output are present on fails.
export interface Evidence {
  quote?: string | null;
  location?: {
    section?: string | null;
    page?: number | null;
    char_span?: [number, number] | null;
    quote?: string | null;
  } | null;
  recompute_script?: string;
  expected_output?: string;
  // Declared, pinned deps (DESIGN §3.8). Empty array ⇒ stdlib-only ⇒ runnable
  // in-browser via Pyodide. Non-empty ⇒ "native-only" in the viewer.
  script_dependencies?: string[];
  [key: string]: unknown;
}

export interface Finding {
  verifier_id: string;
  claim_id?: string | null;
  status: FindingStatus;
  trust_tier: TrustTier;
  verifier_kind: VerifierKind;
  severity?: Severity | null;
  message?: string;
  discrepancy?: string | null;
  reported?: unknown;
  computed?: unknown;
  evidence?: Evidence;
  details?: Record<string, unknown>;
}

export interface RoutedToHuman {
  claim_id?: string | null;
  dimension: string; // e.g. "significance", "novelty" — surfaced, not scored
  note?: string;
  quote?: string | null;
}

export interface DroppedFlag {
  finding: Finding; // a flag a fresh-context pass retracted (false positive)
  reason: string;
}

export interface AuditReport {
  schema_version?: string;
  paper_id: string;
  meta?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  findings: Finding[];
  dropped_flags?: DroppedFlag[];
  routed_to_human?: RoutedToHuman[];
  abstained?: Finding[];
}

// Gallery index entry — derived view over an AuditReport for the card list.
export interface PaperSummary {
  id: string;
  title: string;
  field: string;
  doi?: string | null;
  status: PaperStatus;
  flag_count: number; // # of fail findings (= categories.quantitative)
  // Counts per issue category — the card renders these as "N quantitative · N overclaims · …"
  categories: Record<IssueCategory, number>;
  passes: number; // deterministic checks that passed
  reviewed_clean: number; // claims a reviewer read and found nothing wrong with
  trust_tiers: TrustTier[]; // distinct tiers present across findings
  routed_count: number;
}

// Pipeline status (DESIGN §2): queued → extracting → auditing → confirming → done.
export type PaperStatus =
  | "queued"
  | "extracting"
  | "auditing"
  | "confirming"
  | "done"
  | "error";
