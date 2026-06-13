// Human-readable labels + the CSS-variable styling for each enum value.
// Centralised so trust tiers / statuses / severities render identically
// everywhere and never collapse (DESIGN §3.6).
//
// Owner feedback: lead with plain language. The precise machine code (the enum
// value, the Tn epistemic tier) is kept as `code`/`hint` so it can ride along as
// a hover `title=` tooltip — never as the visible label on a card.

import type { FindingStatus, Severity, TrustTier, PaperStatus } from "@/lib/types";

interface Style {
  label: string; // plain-language, shown to the reader
  code: string; // the precise machine code, for a hover tooltip
  fg: string; // CSS var() for text/icon color
  bg: string;
  border: string;
  hint?: string; // short tooltip / description
}

export const TIER_STYLE: Record<TrustTier, Style> = {
  deterministic_confirmed: {
    label: "Confirmed by recomputation",
    code: "deterministic_confirmed",
    fg: "var(--tier-deterministic)",
    bg: "var(--tier-deterministic-bg)",
    border: "var(--tier-deterministic-border)",
    hint: "Re-derived from the paper's own numbers (or a trusted reference) by a deterministic check. Exact — you can re-run it yourself.",
  },
  calibrated_synthesized: {
    label: "Calibrated check",
    code: "calibrated_synthesized",
    fg: "var(--tier-calibrated)",
    bg: "var(--tier-calibrated-bg)",
    border: "var(--tier-calibrated-border)",
    hint: "A purpose-built verifier that passed a calibration gate within its declared error budget.",
  },
  advisory_assisted: {
    label: "Advisory",
    code: "advisory_assisted",
    fg: "var(--tier-advisory)",
    bg: "var(--tier-advisory-bg)",
    border: "var(--tier-advisory-border)",
    hint: "Advisory only — an assisted judgment that did not clear the bar for a confirmed flag.",
  },
  routed_to_human: {
    label: "For a human",
    code: "routed_to_human",
    fg: "var(--tier-human)",
    bg: "var(--tier-human-bg)",
    border: "var(--tier-human-border)",
    hint: "Surfaced for a human reviewer — LITMUS deliberately does not score this.",
  },
};

export const STATUS_STYLE: Record<FindingStatus, Style> = {
  fail: {
    label: "Discrepancy",
    code: "fail",
    fg: "var(--fail)",
    bg: "var(--fail-bg)",
    border: "var(--fail-border)",
    hint: "A verifier found and confirmed a discrepancy.",
  },
  pass: {
    label: "Checks out",
    code: "pass",
    fg: "var(--pass)",
    bg: "var(--pass-bg)",
    border: "var(--pass-border)",
    hint: "A verifier ran and found nothing to flag.",
  },
  inconclusive: {
    label: "Couldn't check",
    code: "inconclusive",
    fg: "var(--inconclusive)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
    hint: "The verifier could not reach a sound verdict and declined to guess.",
  },
  error: {
    label: "Couldn't check",
    code: "error",
    fg: "var(--inconclusive)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
    hint: "The verifier errored before it could reach a verdict.",
  },
};

export const SEVERITY_STYLE: Record<Severity, Style> = {
  A: {
    label: "Critical",
    code: "A",
    fg: "var(--sev-a)",
    bg: "var(--fail-bg)",
    border: "var(--fail-border)",
    hint: "Critical — a result-altering error.",
  },
  B: {
    label: "Major",
    code: "B",
    fg: "var(--sev-b)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
    hint: "Major — a material discrepancy.",
  },
  C: {
    label: "Minor",
    code: "C",
    fg: "var(--sev-c)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
    hint: "Minor — small or cosmetic.",
  },
};

// Epistemic tiers T0..T8 (DESIGN §5). On cards we prefer NOT to show the bare
// code — we show the short human phrase, and keep "T0" etc. only as a tooltip.
interface TierPhrase {
  phrase: string; // short human phrase shown to the reader
  code: string; // "T0".."T8" — for a hover tooltip only
  hint: string; // one-line description of what that tier checks
}

export const EPISTEMIC_TIER: Record<string, TierPhrase> = {
  T0: { phrase: "arithmetic", code: "T0", hint: "Internal arithmetic — a check on the paper's own numbers." },
  T1: { phrase: "known limits / constants", code: "T1", hint: "Fixed external knowledge — physical limits, constants, retraction status." },
  T2: { phrase: "internal consistency", code: "T2", hint: "Prose vs the paper's own table or figure." },
  T3: { phrase: "method", code: "T3", hint: "Whether the method was appropriate and correctly applied." },
  T4: { phrase: "claim vs evidence", code: "T4", hint: "Whether the claim's strength and scope match its evidence." },
  T5: { phrase: "vs literature", code: "T5", hint: "Consistency with established results and cited sources." },
  T6: { phrase: "reproducibility", code: "T6", hint: "Whether provided data/code reproduce the reported numbers." },
  T7: { phrase: "integrity signal", code: "T7", hint: "An integrity screening signal for a human — not a verdict." },
  T8: { phrase: "subjective", code: "T8", hint: "Irreducibly subjective — significance, novelty, taste. Not scored." },
};

/** Look up the human phrase for an epistemic tier code, tolerating unknowns. */
export function epistemicTier(code: string | null | undefined): TierPhrase | null {
  if (!code) return null;
  return EPISTEMIC_TIER[code] ?? null;
}

// Routed-to-human dimensions split into two buckets (DESIGN §5):
//   - T7 integrity screening signals (image duplication, p-curve, Benford …)
//   - T8 irreducibly subjective dimensions (significance, novelty, taste …)
// The schema only carries a free-text `dimension`, so we classify by name and
// fall back to "subjective" (the common case) when unsure.
const INTEGRITY_DIMENSIONS = new Set([
  "integrity",
  "image_integrity",
  "image_duplication",
  "duplication",
  "manipulation",
  "benford",
  "terminal_digit",
  "p_curve",
  "tortured_phrases",
  "hidden_text",
  "too_perfect",
  "plagiarism",
]);

export type RoutedBucket = "integrity" | "subjective";

export function routedBucket(dimension: string): RoutedBucket {
  return INTEGRITY_DIMENSIONS.has(dimension.toLowerCase().replace(/[\s-]+/g, "_"))
    ? "integrity"
    : "subjective";
}

// Pipeline stages (DESIGN §2). Used by /upload to explain the flow.
export const PIPELINE_STAGES: { id: PaperStatus; label: string; blurb: string }[] = [
  { id: "queued", label: "Queued", blurb: "Accepted; waiting for a worker." },
  {
    id: "extracting",
    label: "Extracting",
    blurb: "Claude reads the PDF — text, tables, figures — into a schema-validated claim graph.",
  },
  {
    id: "auditing",
    label: "Auditing",
    blurb: "The planner routes claims to verifiers; they run in parallel and judge with code.",
  },
  {
    id: "confirming",
    label: "Confirming",
    blurb: "A fresh-context pass re-checks each candidate flag and drops self-caught false positives.",
  },
  { id: "done", label: "Done", blurb: "The audit report is assembled and cached." },
];

export const VERIFIER_KIND_LABEL: Record<string, string> = {
  prebuilt: "prebuilt verifier",
  templated: "templated verifier",
  synthesized: "synthesized verifier",
  assisted: "assisted verifier",
};
