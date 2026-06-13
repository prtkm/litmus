// The audit-card page (DESIGN §2, §14). Server Component: it loads the full
// AuditReport (Supabase if configured, else fixtures) and lays it out so the
// reader can always tell WHICH KIND of evidence they are looking at. The page
// mirrors how the auditor works (P1: judge the checkable with code; reason over
// the rest) by splitting the verdict into two clearly-named bands plus a tail:
//
//   BAND 1 — "Verified by recomputation"  (the DETERMINISTIC layer; trust tiers
//      deterministic_confirmed / calibrated_synthesized). The CHECKABLE FLAGS
//      (status=fail), each with quote → reported vs computed → discrepancy → a
//      DISTINCT trust-tier badge + severity, and the ▶ "Run it yourself" button
//      that reruns the recompute_script in-browser (Pyodide). Plus a compact
//      "confirmed correct" summary of the deterministic PASS findings. The ▶ run
//      button lives ONLY here — deterministic findings ship a recompute_script;
//      advisory items do not.
//   BAND 2 — "Expert review"  (the NON-DETERMINISTIC layer; trust tier
//      advisory_assisted). The advisory routed concerns (dimension advisory:* —
//      "Method concern" / "Possible over-reach" / "Plausibility concern" / …)
//      plus any advisory_assisted findings. Reasoned judgment, not computation —
//      no run button, visually lower-key so it never reads as a confirmed error.
//   TAIL — "Routed to a human (not scored)" (subjective:* + tier:T7/T8), then the
//      collapsed ABSTAINED list and the DROPPED-FLAG log (fresh-context
//      self-caught false positives — the autonomy evidence).
//
// FindingCard / RecomputeRunner are Client Components; everything else here is
// server-rendered. Trust tiers never collapse (DESIGN §3.6): the badge for a
// deterministic_confirmed flag is visually distinct from an advisory_assisted one.

import Link from "next/link";
import { notFound } from "next/navigation";
import { getPaper, usingLiveData } from "@/lib/data";
import { FindingCard } from "@/components/finding-card";
import { SeverityBadge, StatusBadge, TrustTierBadge } from "@/components/badges";
import {
  dimensionLabel,
  routedGroup,
  isReviewedClean,
  categorize,
  CATEGORY_META,
  type AuditCategorization,
} from "@/lib/labels";
import type {
  Finding,
  RoutedToHuman,
  DroppedFlag,
  TrustTier,
} from "@/lib/types";

// A finding belongs in the DETERMINISTIC band (band 1) when its trust tier is
// recomputation-grade (P6 ordering). Everything else (advisory_assisted, or the
// rare routed_to_human tier on a finding) is reasoned judgment → band 2.
const DETERMINISTIC_TIERS: ReadonlySet<TrustTier> = new Set<TrustTier>([
  "deterministic_confirmed",
  "calibrated_synthesized",
]);

function isDeterministic(f: Finding): boolean {
  return DETERMINISTIC_TIERS.has(f.trust_tier);
}

// Read fresh on each request when Supabase is live; fixtures are static.
export const dynamic = "force-dynamic";

export default async function PaperPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const report = await getPaper(decodeURIComponent(id));
  if (!report) notFound();

  const meta = (report.meta ?? {}) as Record<string, unknown>;
  const summary = (report.summary ?? {}) as Record<string, unknown>;

  // --- Partition by KIND of evidence, matching how the auditor works. -------
  // Band 1 (deterministic): findings whose trust tier is recomputation-grade.
  //   · checkableFlags — status=fail, the ▶ run-it-yourself cards.
  //   · confirmedPasses — status=pass, the compact "confirmed correct" summary.
  // Band 2 (expert review): advisory_assisted findings (reasoned, no script) and
  //   the advisory:* routed concerns.
  const deterministic = report.findings.filter(isDeterministic);
  const reasonedFindings = report.findings.filter((f) => !isDeterministic(f));

  const checkableFlags = deterministic.filter((f) => f.status === "fail");
  const confirmedPasses = deterministic.filter((f) => f.status === "pass");
  // Deterministic findings that neither failed nor passed cleanly (inconclusive
  // / error). Not flags, not confirmations — kept as a quiet "ran, no verdict".
  const deterministicOther = deterministic.filter(
    (f) => f.status !== "fail" && f.status !== "pass",
  );

  // Routed items split into the advisory concerns (→ band 2) and the items a
  // person must judge (→ tail). routedGroup() already does this split.
  const routed = report.routed_to_human ?? [];
  const advisoryAll = routed.filter((r) => routedGroup(r.dimension) === "advisory");
  // Split advisory items into GENUINE concerns vs "reviewed, nothing wrong"
  // transparency notes — the latter are COUNTED, not listed, so a clean claim
  // never inflates the concern list (the exact noise the owner flagged).
  const advisoryRouted = advisoryAll.filter((r) => !isReviewedClean(r.note));
  const reviewedCleanRouted = advisoryAll.length - advisoryRouted.length;
  const humanRouted = routed.filter((r) => routedGroup(r.dimension) === "human");

  // Band 2's reviewer-concern count = advisory findings + GENUINE advisory routed items.
  const reviewerConcerns = reasonedFindings.length + advisoryRouted.length;

  // Category breakdown for the at-a-glance strip (deterministic + non-deterministic).
  const cat = categorize(report);

  const abstained = report.abstained ?? [];
  const dropped = report.dropped_flags ?? [];

  const title = (meta.title as string) ?? report.paper_id;
  const field = (meta.field as string) ?? null;
  const doi = (meta.doi as string) ?? null;
  const authors = (meta.authors as string) ?? null;
  const venue = (meta.venue as string) ?? null;
  const headline = (summary.headline as string) ?? null;

  return (
    <div className="space-y-10">
      <header className="space-y-3">
        <Link href="/" className="text-xs underline" style={{ color: "var(--muted)" }}>
          ← All papers
        </Link>

        {field && (
          <div
            className="text-[11px] font-medium uppercase tracking-wide"
            style={{ color: "var(--faint)" }}
          >
            {field}
          </div>
        )}

        <h1 className="text-2xl font-semibold leading-snug tracking-tight">
          {title}
        </h1>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" style={{ color: "var(--faint)" }}>
          {authors && <span>{authors}</span>}
          {venue && <span>· {venue}</span>}
          {doi && (
            <a
              href={`https://doi.org/${doi}`}
              target="_blank"
              rel="noreferrer"
              className="font-mono underline"
            >
              {doi}
            </a>
          )}
          <span aria-hidden>·</span>
          <span>{usingLiveData() ? "live (Supabase)" : "local fixture"}</span>
        </div>

        <p
          className="max-w-3xl rounded-lg border p-3 text-sm leading-relaxed"
          style={{
            borderColor: checkableFlags.length ? "var(--fail-border)" : "var(--pass-border)",
            background: checkableFlags.length ? "var(--fail-bg)" : "var(--pass-bg)",
            color: "var(--foreground)",
          }}
        >
          {plainVerdict({
            verified: deterministic.length,
            flagged: checkableFlags.length,
            concerns: reviewerConcerns,
            human: humanRouted.length,
          })}
        </p>

        {headline && (
          <p className="max-w-3xl text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
            {headline}
          </p>
        )}

        <CategoryStrip
          cat={cat}
          verified={deterministic.length}
          reviewedClean={cat.reviewedClean + reviewedCleanRouted}
        />
      </header>

      {/* ─────────────────────────────────────────────────────────────────
          BAND 1 — VERIFIED BY RECOMPUTATION (the deterministic layer).
          A machine re-ran these and you can too: every flag ships a script.
          This is the high-trust, reproducible band — distinct blue accent.
          ───────────────────────────────────────────────────────────────── */}
      <Band
        kind="deterministic"
        title="Verified by recomputation"
        microcopy="LITMUS re-ran these checks in code. Every flag ships a script you can run yourself to reproduce the result — this is computation, not opinion."
      >
        {/* The checkable flags — the heart of the page. ▶ run button lives here
            and ONLY here (each carries a recompute_script). */}
        {checkableFlags.length === 0 ? (
          <Empty>
            Nothing flagged. The deterministic checks that ran — recomputing the
            paper&apos;s own numbers and consistency — found no discrepancy.
          </Empty>
        ) : (
          <div className="space-y-5">
            {checkableFlags.map((f) => (
              <FindingCard key={f.verifier_id} finding={f} />
            ))}
          </div>
        )}

        {/* Compact "confirmed correct" summary of the deterministic PASSES. */}
        {confirmedPasses.length > 0 && (
          <ConfirmedCorrect findings={confirmedPasses} />
        )}

        {/* Deterministic checks that ran but reached no verdict (rare). Quiet. */}
        {deterministicOther.length > 0 && (
          <div className="mt-4 space-y-3">
            {deterministicOther.map((f) => (
              <MutedFindingRow key={f.verifier_id} finding={f} />
            ))}
          </div>
        )}
      </Band>

      {/* ─────────────────────────────────────────────────────────────────
          BAND 2 — EXPERT REVIEW (the non-deterministic layer).
          Reasoned concerns from a multi-perspective read. Nothing to re-run —
          judgment, not computation. Lower-key amber accent so it never reads
          as a confirmed error. Rendered only when there is something to show.
          ───────────────────────────────────────────────────────────────── */}
      {reviewerConcerns > 0 && (
        <Band
          kind="review"
          title="Expert review"
          microcopy="Reasoned concerns from a careful multi-perspective read — methodologist, domain expert, skeptic. These are judgment, not computation: there's nothing to re-run, so weigh them yourself."
        >
          <div className="space-y-3">
            {/* advisory_assisted findings — reasoned, no recompute_script, so
                no run button (rendered as a concern row, not a FindingCard). */}
            {reasonedFindings.map((f) => (
              <ReviewFindingRow key={f.verifier_id} finding={f} />
            ))}
            {/* advisory:* routed concerns — methodologist / claims-auditor /
                domain-expert / skeptic notes. Also no run button. */}
            {advisoryRouted.map((r, i) => (
              <ReviewConcernRow key={`${r.dimension}-${i}`} routed={r} />
            ))}
            {reviewedCleanRouted > 0 && (
              <p className="pt-1 text-[11px]" style={{ color: "var(--faint)" }}>
                + {reviewedCleanRouted} more claim{reviewedCleanRouted === 1 ? "" : "s"} reviewed with no concern.
              </p>
            )}
          </div>
        </Band>
      )}

      {/* ─────────────────────────────────────────────────────────────────
          TAIL — kept as-is in spirit:
            · Routed to a human (not scored): subjective:* + tier:T7/T8.
            · Abstained: claims outside the current automated checks (collapsed).
            · Dropped-flag log: fresh-context self-caught false positives.
          ───────────────────────────────────────────────────────────────── */}
      {humanRouted.length > 0 && (
        <Section
          title="Routed to a human (not scored)"
          notScored
          blurb="LITMUS does not score these. They are points a person should weigh in on — judgment calls and integrity signals — not problems found in the paper."
        >
          <div className="space-y-3">
            {humanRouted.map((r, i) => (
              <RoutedRow key={`${r.dimension}-${i}`} routed={r} />
            ))}
          </div>
        </Section>
      )}

      {abstained.length > 0 && <OutsideChecks items={abstained} />}

      {dropped.length > 0 && (
        <Section
          title="Caught and dropped on re-read"
          blurb="Possible issues that a second, fresh read re-checked and withdrew as false alarms. Shown for transparency — these are NOT problems with the paper."
        >
          <div className="space-y-3">
            {dropped.map((d, i) => (
              <DroppedRow key={`${d.finding.verifier_id}-${i}`} dropped={d} />
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

// The category strip summarizes the audit by ISSUE CATEGORY so both kinds of
// value are legible at a glance: the deterministic catches ("quantitative
// issues") AND the non-deterministic review ("overclaims" / "method concerns" /
// "integrity signals" / "subjective"). Reviewed-clean transparency notes are NOT
// shown as concerns — they roll into the quiet "reviewed, clean" footer line.
function CategoryStrip({
  cat,
  verified,
  reviewedClean,
}: {
  cat: AuditCategorization;
  verified: number;
  reviewedClean: number;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {cat.order.length === 0 ? (
          <span
            className="inline-flex items-center rounded-md border px-2.5 py-1 text-xs"
            style={{ borderColor: "var(--pass-border)", background: "var(--pass-bg)", color: "var(--ok)" }}
          >
            No issues — the checks ran and the review found no concerns.
          </span>
        ) : (
          cat.order.map((c) => {
            const m = CATEGORY_META[c];
            return (
              <span
                key={c}
                className="inline-flex items-baseline gap-1.5 rounded-md border px-2.5 py-1 text-xs"
                style={{ borderColor: m.border, background: m.bg }}
                title={m.blurb}
              >
                <span className="font-semibold" style={{ color: m.fg }}>
                  {cat.counts[c]}
                </span>
                <span style={{ color: "var(--muted)" }}>
                  {cat.counts[c] === 1 ? m.one : m.many}
                </span>
              </span>
            );
          })
        )}
      </div>
      <p className="text-[11px]" style={{ color: "var(--faint)" }}>
        {verified} check{verified === 1 ? "" : "s"} verified by recomputation
        {reviewedClean > 0
          ? ` · ${reviewedClean} claim${reviewedClean === 1 ? "" : "s"} reviewed, clean`
          : ""}
      </p>
    </div>
  );
}

// One plain-English sentence at the top of the page, built from the report's
// own counts and structured around the two bands: how much was VERIFIED BY
// RECOMPUTATION (and how much of that was flagged), how many REVIEWER CONCERNS
// the expert read raised, and how many items were ROUTED TO A HUMAN.
function plainVerdict({
  verified,
  flagged,
  concerns,
  human,
}: {
  verified: number;
  flagged: number;
  concerns: number;
  human: number;
}): string {
  const parts: string[] = [];

  if (verified === 0) {
    parts.push("No checks could be verified by recomputation on this paper");
  } else if (flagged === 0) {
    parts.push(
      `${verified} check${verified === 1 ? "" : "s"} verified by recomputation, none flagged`,
    );
  } else {
    parts.push(
      `${verified} check${verified === 1 ? "" : "s"} verified by recomputation (${flagged} flagged)`,
    );
  }

  if (concerns > 0) {
    parts.push(`${concerns} reviewer concern${concerns === 1 ? "" : "s"}`);
  }

  if (human > 0) {
    parts.push(`${human} routed to a human`);
  }

  // Join as a single sentence: "A, B, and C."
  if (parts.length === 1) return parts[0] + ".";
  const last = parts[parts.length - 1];
  return parts.slice(0, -1).join(", ") + ", and " + last + ".";
}

function Section({
  title,
  blurb,
  notScored = false,
  children,
}: {
  title: string;
  blurb: string;
  notScored?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-3 border-b pb-2" style={{ borderColor: "var(--border)" }}>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide">{title}</h2>
          {notScored && (
            <span
              className="rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide"
              style={{
                color: "var(--tier-human)",
                background: "var(--tier-human-bg)",
                borderColor: "var(--tier-human-border)",
              }}
            >
              not scored
            </span>
          )}
        </div>
        <p className="mt-1 max-w-3xl text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
          {blurb}
        </p>
      </div>
      {children}
    </section>
  );
}

// A top-level BAND — the page's primary structural unit. Two kinds, deliberately
// distinct so a reader instantly knows which kind of evidence they're looking at:
//
//   kind="deterministic" → blue (--tier-deterministic), a "↻ recomputed" cue, a
//     "reproducible" chip. "A machine re-ran this and you can too."
//   kind="review"        → amber (--tier-advisory), an "✎ judgment" cue, an
//     "advisory" chip, lower-key tint. "An expert reviewer flagged a concern."
//
// The band carries a tinted, accent-bordered header with the title, a one-line
// microcopy, and a left accent rail down the whole band so the two never blur.
function Band({
  kind,
  title,
  microcopy,
  children,
}: {
  kind: "deterministic" | "review";
  title: string;
  microcopy: string;
  children: React.ReactNode;
}) {
  const det = kind === "deterministic";
  const accent = det ? "var(--tier-deterministic)" : "var(--tier-advisory)";
  const tintBg = det ? "var(--tier-deterministic-bg)" : "var(--tier-advisory-bg)";
  const tintBorder = det
    ? "var(--tier-deterministic-border)"
    : "var(--tier-advisory-border)";
  const icon = det ? "↻" : "✎";
  const chip = det ? "reproducible" : "advisory";

  return (
    <section
      className="overflow-hidden rounded-xl border"
      style={{ borderColor: tintBorder }}
    >
      {/* Tinted header — the band's identity. */}
      <div
        className="border-b px-4 py-3"
        style={{ background: tintBg, borderColor: tintBorder }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span
            aria-hidden
            className="flex h-5 w-5 items-center justify-center rounded-full text-xs font-bold"
            style={{ color: accent, border: `1px solid ${accent}` }}
          >
            {icon}
          </span>
          <h2 className="text-sm font-semibold uppercase tracking-wide" style={{ color: accent }}>
            {title}
          </h2>
          <span
            className="rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide"
            style={{ color: accent, background: "var(--surface)", borderColor: tintBorder }}
          >
            {chip}
          </span>
        </div>
        <p className="mt-1.5 max-w-3xl text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
          {microcopy}
        </p>
      </div>
      {/* Body, with a left accent rail tying it to the header. */}
      <div className="border-l-2 p-4" style={{ borderColor: accent }}>
        {children}
      </div>
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-lg border border-dashed p-4 text-sm"
      style={{ borderColor: "var(--border-strong)", color: "var(--faint)" }}
    >
      {children}
    </div>
  );
}

// The compact "confirmed correct" summary of the deterministic PASS findings:
// a one-line count + an expandable list. Server-rendered via native <details>
// so it stays inside this Server Component (no client JS needed). Each row shows
// its distinct trust-tier badge so the recomputation grade never blurs.
function ConfirmedCorrect({ findings }: { findings: Finding[] }) {
  const n = findings.length;
  return (
    <details className="group mt-5">
      <summary
        className="flex cursor-pointer list-none items-center gap-2 rounded-lg border px-3 py-2 text-sm"
        style={{
          borderColor: "var(--pass-border)",
          background: "var(--pass-bg)",
          color: "var(--foreground)",
        }}
      >
        <span aria-hidden className="transition-transform group-open:rotate-90" style={{ color: "var(--pass)" }}>
          ›
        </span>
        <span className="font-semibold" style={{ color: "var(--pass)" }}>
          {n}
        </span>
        <span>
          {n === 1 ? "check" : "checks"} recomputed and confirmed correct — no
          discrepancy. Expand to see {n === 1 ? "it" : "each"}.
        </span>
      </summary>
      <div className="mt-3 space-y-2 pl-4">
        {findings.map((f) => (
          <div
            key={f.verifier_id}
            className="rounded-md border p-3"
            style={{ borderColor: "var(--border)", background: "var(--surface)" }}
          >
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge status={f.status} />
              <TrustTierBadge tier={f.trust_tier} />
              <span
                className="ml-auto font-mono text-[11px]"
                style={{ color: "var(--faint)" }}
                title={`verifier ${f.verifier_id}`}
              >
                {f.verifier_id}
              </span>
            </div>
            {f.message && (
              <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
                {f.message}
              </p>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}

function MutedFindingRow({ finding }: { finding: Finding }) {
  return (
    <div
      className="rounded-lg border p-3"
      style={{ borderColor: "var(--border)", background: "var(--surface)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={finding.status} />
        <TrustTierBadge tier={finding.trust_tier} />
        <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--faint)" }}>
          {finding.verifier_id}
        </span>
      </div>
      {finding.message && (
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          {finding.message}
        </p>
      )}
    </div>
  );
}

// BAND 2 — an advisory_assisted FINDING rendered as a reviewer concern. This is
// the non-deterministic layer: there is no recompute_script, so there is NO run
// button. Amber accent + a "judgment, not a flag" cue keep it visually distinct
// from the confirmed errors in band 1.
function ReviewFindingRow({ finding }: { finding: Finding }) {
  const ev = finding.evidence ?? {};
  return (
    <div
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--tier-advisory-border)", background: "var(--tier-advisory-bg)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full border px-2.5 py-0.5 text-xs font-medium"
          style={{
            color: "var(--tier-advisory)",
            background: "var(--surface)",
            borderColor: "var(--tier-advisory-border)",
          }}
          title="A reasoned concern — judgment, not a recomputed flag. Nothing to re-run."
        >
          Reviewer concern
        </span>
        {finding.severity && <SeverityBadge severity={finding.severity} />}
        <TrustTierBadge tier={finding.trust_tier} />
        <span
          className="ml-auto font-mono text-[11px]"
          style={{ color: "var(--faint)" }}
          title={`verifier ${finding.verifier_id}`}
        >
          {finding.verifier_id}
        </span>
      </div>
      {ev.quote && (
        <blockquote
          className="mt-3 border-l-2 pl-3 text-sm italic leading-relaxed"
          style={{ borderColor: "var(--border-strong)", color: "var(--muted)" }}
        >
          “{ev.quote}”
        </blockquote>
      )}
      {finding.message && (
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
          {finding.message}
        </p>
      )}
      {/* No ▶ run button: advisory findings carry no recompute_script. */}
    </div>
  );
}

// BAND 2 — an advisory:* routed concern (methodologist / claims-auditor /
// domain-expert / skeptic). Same amber, judgment-not-computation framing; NO run
// button. The raw dimension code is never shown — only its plain-language label,
// with the code tucked into a hover tooltip (built on lib/labels dimensionLabel).
function ReviewConcernRow({ routed }: { routed: RoutedToHuman }) {
  const dim = dimensionLabel(routed.dimension);
  return (
    <div
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--tier-advisory-border)", background: "var(--tier-advisory-bg)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full border px-2.5 py-0.5 text-xs font-medium"
          style={{
            color: "var(--tier-advisory)",
            background: "var(--surface)",
            borderColor: "var(--tier-advisory-border)",
          }}
          title={dim.code ? `${dim.blurb} (code: ${dim.code})` : dim.blurb}
        >
          {dim.label}
        </span>
        {routed.claim_id && (
          <span className="font-mono text-[11px]" style={{ color: "var(--faint)" }} title="claim id">
            {routed.claim_id}
          </span>
        )}
      </div>
      <p className="mt-2 text-xs leading-relaxed" style={{ color: "var(--faint)" }}>
        {dim.blurb}
      </p>
      {routed.quote && (
        <blockquote
          className="mt-3 border-l-2 pl-3 text-sm italic leading-relaxed"
          style={{ borderColor: "var(--border-strong)", color: "var(--muted)" }}
        >
          “{routed.quote}”
        </blockquote>
      )}
      {routed.note && (
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
          {routed.note}
        </p>
      )}
      {/* No ▶ run button: there is nothing to re-run — weigh it yourself. */}
    </div>
  );
}

// TAIL — a "routed to a human (not scored)" item: subjective:* + tier:T7/T8.
// Slate accent (--tier-human), distinct from band 2's amber, so "needs a
// person's judgment" never blurs with "an advisory reviewer concern".
function RoutedRow({ routed }: { routed: RoutedToHuman }) {
  const dim = dimensionLabel(routed.dimension);
  return (
    <div
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--tier-human-border)", background: "var(--tier-human-bg)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full border px-2.5 py-0.5 text-xs font-medium"
          style={{
            color: "var(--tier-human)",
            background: "var(--surface)",
            borderColor: "var(--tier-human-border)",
          }}
          title={dim.code ? `${dim.blurb} (code: ${dim.code})` : dim.blurb}
        >
          {dim.label}
        </span>
        {routed.claim_id && (
          <span className="font-mono text-[11px]" style={{ color: "var(--faint)" }} title="claim id">
            {routed.claim_id}
          </span>
        )}
      </div>
      <p className="mt-2 text-xs leading-relaxed" style={{ color: "var(--faint)" }}>
        {dim.blurb}
      </p>
      {routed.quote && (
        <blockquote
          className="mt-3 border-l-2 pl-3 text-sm italic leading-relaxed"
          style={{ borderColor: "var(--border-strong)", color: "var(--muted)" }}
        >
          “{routed.quote}”
        </blockquote>
      )}
      {routed.note && (
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
          {routed.note}
        </p>
      )}
    </div>
  );
}

// The old "Abstained" wall, collapsed to a single quiet, muted line. These are
// NOT findings against the paper — they are claims the current automated checks
// don't cover yet. De-emphasised, click-to-expand for anyone who wants detail.
function OutsideChecks({ items }: { items: Finding[] }) {
  const n = items.length;
  return (
    <details className="group mt-2">
      <summary
        className="flex cursor-pointer list-none items-center gap-2 text-xs leading-relaxed"
        style={{ color: "var(--faint)" }}
      >
        <span aria-hidden className="transition-transform group-open:rotate-90">
          ›
        </span>
        <span>
          {n} claim{n === 1 ? "" : "s"} fall outside the current automated checks.
          Not problems with the paper — just things LITMUS can&apos;t verify on
          its own yet.
        </span>
      </summary>
      <div className="mt-3 space-y-2 pl-4">
        {items.map((f) => (
          <OutsideRow key={f.verifier_id} finding={f} />
        ))}
      </div>
    </details>
  );
}

function OutsideRow({ finding }: { finding: Finding }) {
  const ev = finding.evidence ?? {};
  return (
    <div
      className="rounded-md border p-3 text-xs leading-relaxed"
      style={{ borderColor: "var(--border)", background: "var(--surface-2)", color: "var(--muted)" }}
    >
      {ev.quote && (
        <blockquote
          className="border-l-2 pl-3 italic"
          style={{ borderColor: "var(--border-strong)" }}
        >
          “{ev.quote}”
        </blockquote>
      )}
      {finding.message && <p className={ev.quote ? "mt-2" : ""}>{finding.message}</p>}
    </div>
  );
}

function DroppedRow({ dropped }: { dropped: DroppedFlag }) {
  const f = dropped.finding;
  return (
    <div
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--border)", background: "var(--surface-2)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full border px-2.5 py-0.5 text-xs font-medium line-through"
          style={{ color: "var(--faint)", background: "var(--surface)", borderColor: "var(--border-strong)" }}
        >
          retracted
        </span>
        {f.severity && <SeverityBadge severity={f.severity} />}
        <TrustTierBadge tier={f.trust_tier} />
        <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--faint)" }}>
          {f.verifier_id}
        </span>
      </div>
      <p className="mt-3 text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
        <span className="font-medium" style={{ color: "var(--muted)" }}>
          Why it was dropped:{" "}
        </span>
        {dropped.reason}
      </p>
      {f.message && (
        <p className="mt-2 text-xs leading-relaxed" style={{ color: "var(--faint)" }}>
          {f.message}
        </p>
      )}
    </div>
  );
}
