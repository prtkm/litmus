// The audit-card page (DESIGN §2, §14). Server Component: it loads the full
// AuditReport (Supabase if configured, else fixtures) and lays out the four
// regions of a LITMUS verdict —
//
//   1. CHECKABLE FLAGS  — fail findings, each with quote → reported vs computed →
//      discrepancy → a DISTINCT trust-tier badge + severity, and a ▶ "Run it
//      yourself" button that reruns the recompute_script in-browser (Pyodide).
//   2. ROUTED TO HUMAN  — subjective dimensions, explicitly labeled "not scored".
//   3. ABSTAINED        — verifiers that declined to guess.
//   4. DROPPED-FLAG LOG — candidate flags a fresh-context pass self-retracted.
//
// FindingCard / RecomputeRunner are Client Components; everything else here is
// server-rendered. Trust tiers never collapse (DESIGN §3.6): the badge for a
// deterministic_confirmed flag is visually distinct from an advisory_assisted one.

import Link from "next/link";
import { notFound } from "next/navigation";
import { getPaper, usingLiveData } from "@/lib/data";
import { FindingCard } from "@/components/finding-card";
import { SeverityBadge, StatusBadge, TrustTierBadge } from "@/components/badges";
import { routedBucket } from "@/lib/labels";
import type { Finding, RoutedToHuman, DroppedFlag } from "@/lib/types";

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

  const flags = report.findings.filter((f) => f.status === "fail");
  const otherFindings = report.findings.filter((f) => f.status !== "fail");
  const routed = report.routed_to_human ?? [];
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
            borderColor: flags.length ? "var(--fail-border)" : "var(--pass-border)",
            background: flags.length ? "var(--fail-bg)" : "var(--pass-bg)",
            color: "var(--foreground)",
          }}
        >
          {plainVerdict({
            flags: flags.length,
            routed: routed.length,
            abstained: abstained.length,
          })}
        </p>

        {headline && (
          <p className="max-w-3xl text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
            {headline}
          </p>
        )}

        <CountStrip
          flags={flags.length}
          routed={routed.length}
          abstained={abstained.length}
          dropped={dropped.length}
        />
      </header>

      {/* 1. CHECKABLE FLAGS — the heart of the page. */}
      <Section
        title="Discrepancies you can re-run"
        blurb="Each one comes with the exact script LITMUS used. Press ▶ to re-run it in your browser and confirm the result for yourself."
      >
        {flags.length === 0 ? (
          <Empty>
            Nothing flagged. The checks that ran — recomputing the paper's own
            numbers and consistency — found no discrepancy.
          </Empty>
        ) : (
          <div className="space-y-5">
            {flags.map((f) => (
              <FindingCard key={f.verifier_id} finding={f} />
            ))}
          </div>
        )}
      </Section>

      {/* Non-fail findings (passes / inconclusive that were still surfaced). */}
      {otherFindings.length > 0 && (
        <Section
          title="Other checks that ran"
          blurb="Checks that ran without turning up a discrepancy."
        >
          <div className="space-y-3">
            {otherFindings.map((f) => (
              <MutedFindingRow key={f.verifier_id} finding={f} />
            ))}
          </div>
        </Section>
      )}

      {/* 2. FOR A HUMAN — surfaced, explicitly NOT scored. */}
      {routed.length > 0 && (
        <Section
          title="Flagged for a human reviewer"
          notScored
          blurb="LITMUS does not score these. They are points a person should weigh in on, not problems found in the paper."
        >
          <RoutedGroups routed={routed} />
        </Section>
      )}

      {/* 3. OUTSIDE AUTOMATED CHECKS — one quiet line, not a wall. These are NOT
          findings against the paper, so we de-emphasise and collapse them. */}
      {abstained.length > 0 && (
        <OutsideChecks items={abstained} />
      )}

      {/* 4. DROPPED-FLAG LOG — self-caught false positives. Shown only when
          there is something to show; otherwise it is just noise. */}
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

function CountStrip({
  flags,
  routed,
  abstained,
  dropped,
}: {
  flags: number;
  routed: number;
  abstained: number;
  dropped: number;
}) {
  const items: { label: string; value: number; tone: string }[] = [
    { label: flags === 1 ? "discrepancy" : "discrepancies", value: flags, tone: flags ? "var(--fail)" : "var(--ok)" },
    { label: "for a human", value: routed, tone: "var(--tier-human)" },
    { label: "outside automated checks", value: abstained, tone: "var(--inconclusive)" },
    { label: dropped === 1 ? "dropped on re-read" : "dropped on re-read", value: dropped, tone: "var(--faint)" },
  ];
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((it) => (
        <span
          key={it.label}
          className="inline-flex items-baseline gap-1.5 rounded-md border px-2.5 py-1 text-xs"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          <span className="font-semibold" style={{ color: it.tone }}>
            {it.value}
          </span>
          <span style={{ color: "var(--muted)" }}>{it.label}</span>
        </span>
      ))}
    </div>
  );
}

// One plain-English sentence at the top of the page, built from the report's
// own counts. Leads with what LITMUS could re-run, then what needs a person,
// then what fell outside the automated checks.
function plainVerdict({
  flags,
  routed,
  abstained,
}: {
  flags: number;
  routed: number;
  abstained: number;
}): string {
  const parts: string[] = [];

  parts.push(
    flags === 0
      ? "LITMUS re-ran its checks on this paper and found no discrepancy you'd need to act on"
      : `LITMUS re-ran its checks on this paper and confirmed ${flags} discrepanc${
          flags === 1 ? "y" : "ies"
        } you can re-run yourself`,
  );

  if (routed > 0) {
    parts.push(`${routed} point${routed === 1 ? "" : "s"} need a human`);
  }

  if (abstained > 0) {
    parts.push(
      `${abstained} claim${abstained === 1 ? "" : "s"} fall outside the current automated checks`,
    );
  }

  // Join as a single sentence: "A; B; and C."
  if (parts.length === 1) return parts[0] + ".";
  const last = parts[parts.length - 1];
  return parts.slice(0, -1).join("; ") + "; and " + last + ".";
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

// Split the routed items into genuine integrity screening (T7) and subjective
// dimensions (T8). Only show the two-bucket structure when BOTH are present;
// otherwise render a flat, compact list so the section never dominates.
function RoutedGroups({ routed }: { routed: RoutedToHuman[] }) {
  const integrity = routed.filter((r) => routedBucket(r.dimension) === "integrity");
  const subjective = routed.filter((r) => routedBucket(r.dimension) === "subjective");
  const bothPresent = integrity.length > 0 && subjective.length > 0;

  if (!bothPresent) {
    return (
      <div className="space-y-3">
        {routed.map((r, i) => (
          <RoutedRow key={`${r.dimension}-${i}`} routed={r} />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <RoutedSubgroup
        heading="Integrity screening"
        note="Automated screening signals a person should look at — not a verdict."
        items={integrity}
      />
      <RoutedSubgroup
        heading="Judgment calls"
        note="Significance, novelty, framing — questions for the field, not arithmetic."
        items={subjective}
      />
    </div>
  );
}

function RoutedSubgroup({
  heading,
  note,
  items,
}: {
  heading: string;
  note: string;
  items: RoutedToHuman[];
}) {
  return (
    <div>
      <div className="mb-2">
        <span className="text-xs font-semibold" style={{ color: "var(--tier-human)" }}>
          {heading}
        </span>
        <span className="ml-2 text-xs" style={{ color: "var(--faint)" }}>
          {note}
        </span>
      </div>
      <div className="space-y-3">
        {items.map((r, i) => (
          <RoutedRow key={`${r.dimension}-${i}`} routed={r} />
        ))}
      </div>
    </div>
  );
}

function RoutedRow({ routed }: { routed: RoutedToHuman }) {
  return (
    <div
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--tier-human-border)", background: "var(--tier-human-bg)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize"
          style={{
            color: "var(--tier-human)",
            background: "var(--surface)",
            borderColor: "var(--tier-human-border)",
          }}
        >
          {routed.dimension}
        </span>
        {routed.claim_id && (
          <span className="font-mono text-[11px]" style={{ color: "var(--faint)" }}>
            {routed.claim_id}
          </span>
        )}
      </div>
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
          Not problems with the paper — just things LITMUS can't verify on its
          own yet.
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
