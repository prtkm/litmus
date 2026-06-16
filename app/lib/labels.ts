// Human-readable labels + the CSS-variable styling for each enum value.
// Centralised so trust tiers / statuses / severities render identically
// everywhere and never collapse (DESIGN §3.6).
//
// Owner feedback: lead with plain language. The precise machine code (the enum
// value, the Tn epistemic tier) is kept as `code`/`hint` so it can ride along as
// a hover `title=` tooltip — never as the visible label on a card.

import type { FindingStatus, Severity, TrustTier, PaperStatus, IssueCategory } from "@/lib/types";

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
    hint: "Minor — small or presentation-level (e.g. a one-unit granularity gap); weigh it with any paper-level pattern.",
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
  T0: { phrase: "Arithmetic", code: "T0", hint: "Internal arithmetic — a check on the paper's own numbers." },
  T1: { phrase: "Known limit/constant", code: "T1", hint: "Fixed external knowledge — physical limits, constants, retraction status." },
  T2: { phrase: "Internal consistency", code: "T2", hint: "Prose vs the paper's own table or figure." },
  T3: { phrase: "Method", code: "T3", hint: "Whether the method was appropriate and correctly applied." },
  T4: { phrase: "Claim vs evidence", code: "T4", hint: "Whether the claim's strength and scope match its evidence." },
  T5: { phrase: "Vs literature", code: "T5", hint: "Consistency with established results and cited sources." },
  T6: { phrase: "Reproducibility", code: "T6", hint: "Whether provided data/code reproduce the reported numbers." },
  T7: { phrase: "Research-integrity signal", code: "T7", hint: "An integrity screening signal for a human — not a verdict." },
  T8: { phrase: "Subjective judgment", code: "T8", hint: "Irreducibly subjective — significance, novelty, taste. Not scored." },
};

/**
 * Look up the human phrase for an epistemic tier code, tolerating unknowns.
 * Accepts a bare code ("T7") or the prefixed form ("tier:T7"); never returns
 * the raw "Tn" to the caller for rendering — only `phrase` is reader-facing.
 */
export function epistemicTier(code: string | null | undefined): TierPhrase | null {
  if (!code) return null;
  const key = code.replace(/^tier:/i, "").trim().toUpperCase();
  return EPISTEMIC_TIER[key] ?? null;
}

// Routed-to-human items carry a free-text `dimension` (DESIGN §5). The pipeline
// emits it in a "<bucket>:<rest>" form — `tier:T7`, `advisory:method`,
// `subjective:novelty` — or, in older fixtures, a bare word like `significance`.
// A non-expert reader must NEVER see the raw code, so every dimension is mapped
// to a plain-language {label, blurb} via dimensionLabel() below.

export interface DimensionLabel {
  label: string; // plain-language, shown to the reader
  blurb: string; // one-line human explanation of what this means
  code: string; // the raw dimension string — for a hover tooltip only
}

// Title-case a raw token, stripping colons / underscores / dashes. Used as the
// last-resort label for any dimension we haven't seen, so we never render the
// bare code even for unknown future values.
function humanize(raw: string): string {
  const cleaned = raw
    .replace(/^[a-z]+:/i, "") // drop a leading "bucket:" prefix
    .replace(/[_-]+/g, " ")
    .replace(/:/g, " ")
    .trim();
  if (!cleaned) return "Other";
  return cleaned
    .split(/\s+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// Fixed advisory dimensions (reviewer concerns — not scored). Keyed by the part
// after "advisory:" AND by the equivalent verifier-kind name the planner may use.
const ADVISORY: Record<string, { label: string; blurb: string }> = {
  method: { label: "Method concern", blurb: "A reviewer-style note that the method may not fully fit the question — not a confirmed error." },
  methodologist: { label: "Method concern", blurb: "A reviewer-style note that the method may not fully fit the question — not a confirmed error." },
  overreach: { label: "Possible over-reach", blurb: "The claim may reach further than the evidence shown supports." },
  "claims-auditor": { label: "Possible over-reach", blurb: "The claim may reach further than the evidence shown supports." },
  plausibility: { label: "Plausibility concern", blurb: "A reviewer flagged the result as surprising and worth a closer look." },
  "domain-expert": { label: "Plausibility concern", blurb: "A reviewer flagged the result as surprising and worth a closer look." },
  skeptic: { label: "Reviewer caution", blurb: "A general note of caution raised on a careful read." },
  integrity: { label: "Integrity signal", blurb: "An automated screening signal a person should look at — not a finding against the paper." },
  "integrity-screener": { label: "Integrity signal", blurb: "An automated screening signal a person should look at — not a finding against the paper." },
};

// Fixed subjective dimensions (judgment calls — explicitly not scored).
const SUBJECTIVE: Record<string, { label: string }> = {
  significance: { label: "Significance" },
  novelty: { label: "Novelty" },
};

/**
 * Map a raw routed `dimension` string to reader-facing language.
 * Parses the "<bucket>:<rest>" form and returns {label, blurb, code} for every
 * documented value; falls back to a humanized title-case of the raw string for
 * anything unseen. The raw code is returned only as `code` (for a tooltip) — it
 * is never part of `label` or `blurb`.
 */
export function dimensionLabel(dimension: string): DimensionLabel {
  const raw = (dimension ?? "").trim();
  const lower = raw.toLowerCase();
  const colon = lower.indexOf(":");
  const bucket = colon >= 0 ? lower.slice(0, colon) : "";
  const rest = colon >= 0 ? lower.slice(colon + 1).trim() : lower;

  // tier:T7 / T7  and  tier:T8 / T8  → epistemic-tier phrasing.
  if (bucket === "tier" || /^t[0-9]$/.test(lower)) {
    const t = epistemicTier(rest || lower);
    if (t) return { label: t.phrase, blurb: t.hint, code: raw };
  }

  // advisory:<x> — reviewer concerns, not scored.
  if (bucket === "advisory") {
    const hit = ADVISORY[rest];
    if (hit) return { label: hit.label, blurb: hit.blurb, code: raw };
    return {
      label: humanize(rest),
      blurb: "A reviewer-style concern raised for a human to weigh — not a confirmed error.",
      code: raw,
    };
  }

  // subjective:<x> — judgment calls, "(not scored)".
  if (bucket === "subjective") {
    const hit = SUBJECTIVE[rest];
    const base = hit ? hit.label : humanize(rest);
    return {
      label: `${base} (not scored)`,
      blurb: "A judgment for the field — LITMUS surfaces it but does not score it.",
      code: raw,
    };
  }

  // No recognised prefix. Older fixtures use bare words like "significance" /
  // "novelty" (subjective by default). Map the known ones; otherwise humanize.
  const subj = SUBJECTIVE[lower];
  if (subj) {
    return {
      label: `${subj.label} (not scored)`,
      blurb: "A judgment for the field — LITMUS surfaces it but does not score it.",
      code: raw,
    };
  }
  const adv = ADVISORY[lower];
  if (adv) return { label: adv.label, blurb: adv.blurb, code: raw };

  return {
    label: humanize(raw),
    blurb: "Surfaced for a human reviewer — LITMUS does not score this.",
    code: raw,
  };
}

// Two reader-facing sections (DESIGN §5):
//   - "advisory" → reviewer concerns (advisory:*)
//   - "human"    → routed to a person (subjective:* and tier:T7/T8, plus bare
//                  subjective words like "significance"/"novelty")
export type RoutedGroup = "advisory" | "human";

export function routedGroup(dimension: string): RoutedGroup {
  return (dimension ?? "").trim().toLowerCase().startsWith("advisory:")
    ? "advisory"
    : "human";
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

/**
 * Readable name for a verifier kind, tolerating unknown values. Falls back to a
 * humanized form (e.g. "foo_bar" → "Foo Bar verifier") so a raw enum token never
 * reaches the reader even for a kind we haven't mapped.
 */
export function verifierKindLabel(kind: string | null | undefined): string {
  if (!kind) return "verifier";
  return VERIFIER_KIND_LABEL[kind] ?? `${humanize(kind)} verifier`;
}

// --- Issue categorization (DESIGN §4, §5) ------------------------------------
// Summarize an audit by CATEGORY so both the deterministic catch and the
// non-deterministic review are legible at a glance — "N quantitative issues ·
// N overclaims · N method concerns" — instead of one undifferentiated number.

export const CATEGORY_META: Record<
  IssueCategory,
  { one: string; many: string; blurb: string; kind: "deterministic" | "review"; fg: string; bg: string; border: string }
> = {
  quantitative: { one: "quantitative issue", many: "quantitative issues", blurb: "A recomputed numeric error — re-run the script yourself to reproduce it.", kind: "deterministic", fg: "var(--fail)", bg: "var(--fail-bg)", border: "var(--fail-border)" },
  overclaim: { one: "overclaim", many: "overclaims", blurb: "The claim reaches further than the evidence shown supports.", kind: "review", fg: "var(--tier-advisory)", bg: "var(--tier-advisory-bg)", border: "var(--tier-advisory-border)" },
  method: { one: "method concern", many: "method concerns", blurb: "The method may not fully fit the question — test choice, power, assumptions.", kind: "review", fg: "var(--tier-advisory)", bg: "var(--tier-advisory-bg)", border: "var(--tier-advisory-border)" },
  plausibility: { one: "plausibility concern", many: "plausibility concerns", blurb: "A result a domain reviewer flagged as surprising and worth a closer look.", kind: "review", fg: "var(--tier-advisory)", bg: "var(--tier-advisory-bg)", border: "var(--tier-advisory-border)" },
  integrity: { one: "integrity signal", many: "integrity signals", blurb: "An automated research-integrity screening signal for a human — not a finding.", kind: "review", fg: "var(--tier-human)", bg: "var(--tier-human-bg)", border: "var(--tier-human-border)" },
  subjective: { one: "subjective call", many: "subjective calls", blurb: "Significance / novelty — surfaced for a human, explicitly not scored.", kind: "review", fg: "var(--tier-human)", bg: "var(--tier-human-bg)", border: "var(--tier-human-border)" },
};

export const CATEGORY_ORDER: IssueCategory[] = [
  "quantitative", "overclaim", "method", "plausibility", "integrity", "subjective",
];

// A routed item whose NOTE reads as "reviewed, nothing wrong" is a transparency
// note on a clean claim — count it as reviewed, NOT as a concern (otherwise every
// clean claim inflates the overclaim count, which is exactly the noise to avoid).
export function isReviewedClean(note?: string): boolean {
  if (!note) return false;
  const n = note.toLowerCase();
  return (
    /\bno over-?reach\b/.test(n) ||
    /\bno (issue|concern|problem|error)s?\b/.test(n) ||
    /\bsynthesis candidate\b/.test(n) ||
    /\bnothing (wrong|to flag|amiss)\b/.test(n) ||
    /\breviewed\b[^.]*\bclean\b/.test(n)
  );
}

// Map one routed-to-human item → an issue category, or null if it's reviewed-clean.
function routedToCategory(dimension: string, note?: string): IssueCategory | null {
  const d = (dimension ?? "").trim().toLowerCase();
  const rest = d.includes(":") ? d.slice(d.indexOf(":") + 1) : d;
  if (d.startsWith("subjective:") || d === "tier:t8" || /^t8$/.test(d) || rest === "significance" || rest === "novelty")
    return "subjective";
  if (d.includes("integrity") || d === "tier:t7" || /^t7$/.test(d)) return "integrity";
  if (rest.includes("method")) return "method";
  if (rest.includes("domain") || rest.includes("plausib")) return "plausibility";
  // over-reach / claims-auditor / skeptic / any other advisory → a genuine concern
  // unless its note is a reviewed-clean transparency note.
  return isReviewedClean(note) ? null : "overclaim";
}

export interface AuditCategorization {
  counts: Record<IssueCategory, number>;
  order: IssueCategory[]; // categories with count > 0, in display order
  total: number; // sum of all category counts (things actually worth attention)
  quantitative: number; // deterministic catches
  reviews: number; // non-deterministic concerns (overclaim+method+plausibility+integrity)
  subjective: number;
  passes: number; // deterministic checks that passed
  reviewedClean: number; // claims reviewed with nothing wrong
}

export function categorize(report: {
  findings?: { status: string; trust_tier?: string; details?: Record<string, unknown> }[];
  routed_to_human?: { dimension: string; note?: string }[];
}): AuditCategorization {
  const counts: Record<IssueCategory, number> = {
    quantitative: 0, overclaim: 0, method: 0, plausibility: 0, integrity: 0, subjective: 0,
  };
  let passes = 0;
  let reviewedClean = 0;
  let grimScreeningCluster = false; // a borderline GRIM cluster counts as ONE integrity signal, not N
  for (const f of report.findings ?? []) {
    if (f.status === "pass") { passes++; continue; }
    if (f.status !== "fail") continue;
    const tier = f.trust_tier;
    // Only a recomputation-grade fail is a confirmed "quantitative issue". A fail that was re-tiered
    // by a relevance gate (e.g. a marginal GRIM cluster -> routed_to_human screening note) is a
    // data-integrity *signal*, not a hard error — count it as integrity, and only once per cluster.
    const hard = tier === undefined || tier === "deterministic_confirmed" || tier === "calibrated_synthesized";
    if (hard) { counts.quantitative++; continue; }
    if (f.details?.grim_screening_note) grimScreeningCluster = true;
    else counts.integrity++;
  }
  if (grimScreeningCluster) counts.integrity++;
  for (const r of report.routed_to_human ?? []) {
    const c = routedToCategory(r.dimension, r.note);
    if (c === null) reviewedClean++;
    else counts[c]++;
  }
  const order = CATEGORY_ORDER.filter((c) => counts[c] > 0);
  const total = order.reduce((s, c) => s + counts[c], 0);
  const reviews = counts.overclaim + counts.method + counts.plausibility + counts.integrity;
  return { counts, order, total, quantitative: counts.quantitative, reviews, subjective: counts.subjective, passes, reviewedClean };
}

// "3 overclaims" / "1 quantitative issue" — count + correctly-pluralized label.
export function categoryCount(c: IssueCategory, n: number): string {
  const m = CATEGORY_META[c];
  return `${n} ${n === 1 ? m.one : m.many}`;
}
