// Human-readable labels + the CSS-variable styling for each enum value.
// Centralised so trust tiers / statuses / severities render identically
// everywhere and never collapse (DESIGN §3.6).

import type { FindingStatus, Severity, TrustTier, PaperStatus } from "@/lib/types";

interface Style {
  label: string;
  fg: string; // CSS var() for text/icon color
  bg: string;
  border: string;
  hint?: string; // short tooltip / description
}

export const TIER_STYLE: Record<TrustTier, Style> = {
  deterministic_confirmed: {
    label: "Deterministic",
    fg: "var(--tier-deterministic)",
    bg: "var(--tier-deterministic-bg)",
    border: "var(--tier-deterministic-border)",
    hint: "Theorem-grade: a deterministic check on the paper's own numbers (or a trusted reference). Exact and rerunnable.",
  },
  calibrated_synthesized: {
    label: "Calibrated",
    fg: "var(--tier-calibrated)",
    bg: "var(--tier-calibrated-bg)",
    border: "var(--tier-calibrated-border)",
    hint: "A synthesized verifier that passed the calibration gate within its declared error budget.",
  },
  advisory_assisted: {
    label: "Advisory",
    fg: "var(--tier-advisory)",
    bg: "var(--tier-advisory-bg)",
    border: "var(--tier-advisory-border)",
    hint: "Advisory only — assisted judgment that did not clear the gate for an A/B verdict. Not a confirmed flag.",
  },
  routed_to_human: {
    label: "Routed to human",
    fg: "var(--tier-human)",
    bg: "var(--tier-human-bg)",
    border: "var(--tier-human-border)",
    hint: "Subjective dimension — surfaced for a human, explicitly not scored.",
  },
};

export const STATUS_STYLE: Record<FindingStatus, Style> = {
  fail: {
    label: "Fail",
    fg: "var(--fail)",
    bg: "var(--fail-bg)",
    border: "var(--fail-border)",
  },
  pass: {
    label: "Pass",
    fg: "var(--pass)",
    bg: "var(--pass-bg)",
    border: "var(--pass-border)",
  },
  inconclusive: {
    label: "Inconclusive",
    fg: "var(--inconclusive)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
  },
  error: {
    label: "Error",
    fg: "var(--inconclusive)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
  },
};

export const SEVERITY_STYLE: Record<Severity, Style> = {
  A: {
    label: "Severity A",
    fg: "var(--sev-a)",
    bg: "var(--fail-bg)",
    border: "var(--fail-border)",
    hint: "Most severe — a result-altering error.",
  },
  B: {
    label: "Severity B",
    fg: "var(--sev-b)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
    hint: "Material discrepancy.",
  },
  C: {
    label: "Severity C",
    fg: "var(--sev-c)",
    bg: "var(--inconclusive-bg)",
    border: "var(--inconclusive-border)",
    hint: "Minor / cosmetic.",
  },
};

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
