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

        {headline && (
          <p
            className="max-w-3xl rounded-lg border p-3 text-sm leading-relaxed"
            style={{
              borderColor: flags.length ? "var(--fail-border)" : "var(--pass-border)",
              background: flags.length ? "var(--fail-bg)" : "var(--pass-bg)",
              color: "var(--foreground)",
            }}
          >
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
        title="Checkable flags"
        blurb="Each flag carries a trust tier and an executable recompute script. Press ▶ to rerun it in your browser and confirm the verdict for yourself."
      >
        {flags.length === 0 ? (
          <Empty>
            No confirmed flags. The deterministic and calibrated verifiers that
            ran found nothing to flag.
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
          title="Other verifier results"
          blurb="Verifiers that ran without raising a confirmed flag."
        >
          <div className="space-y-3">
            {otherFindings.map((f) => (
              <MutedFindingRow key={f.verifier_id} finding={f} />
            ))}
          </div>
        </Section>
      )}

      {/* 2. ROUTED TO HUMAN — surfaced, explicitly NOT scored. */}
      <Section
        title="Routed to a human"
        notScored
        blurb="Subjective dimensions — significance, novelty, framing. LITMUS surfaces these for a human reviewer and deliberately does not render a verdict (DESIGN §3.5)."
      >
        {routed.length === 0 ? (
          <Empty>Nothing was routed to a human for this paper.</Empty>
        ) : (
          <div className="space-y-3">
            {routed.map((r, i) => (
              <RoutedRow key={`${r.dimension}-${i}`} routed={r} />
            ))}
          </div>
        )}
      </Section>

      {/* 3. ABSTAINED — declined to guess. */}
      {abstained.length > 0 && (
        <Section
          title="Abstained"
          blurb="Verifiers that could not reach a sound verdict on the available evidence and declined to guess (DESIGN §3.4)."
        >
          <div className="space-y-3">
            {abstained.map((f) => (
              <AbstainedRow key={f.verifier_id} finding={f} />
            ))}
          </div>
        </Section>
      )}

      {/* 4. DROPPED-FLAG LOG — self-caught false positives. */}
      <Section
        title="Dropped-flag log"
        blurb="Candidate flags a fresh-context confirmation pass re-read and retracted as false positives (DESIGN §13). Shown for transparency — these are NOT findings against the paper."
      >
        {dropped.length === 0 ? (
          <Empty>No candidate flags were dropped on re-read.</Empty>
        ) : (
          <div className="space-y-3">
            {dropped.map((d, i) => (
              <DroppedRow key={`${d.finding.verifier_id}-${i}`} dropped={d} />
            ))}
          </div>
        )}
      </Section>
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
    { label: flags === 1 ? "confirmed flag" : "confirmed flags", value: flags, tone: flags ? "var(--fail)" : "var(--ok)" },
    { label: "routed to human", value: routed, tone: "var(--tier-human)" },
    { label: "abstained", value: abstained, tone: "var(--inconclusive)" },
    { label: dropped === 1 ? "dropped flag" : "dropped flags", value: dropped, tone: "var(--faint)" },
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

function AbstainedRow({ finding }: { finding: Finding }) {
  const ev = finding.evidence ?? {};
  return (
    <div
      className="rounded-lg border p-4"
      style={{ borderColor: "var(--inconclusive-border)", background: "var(--inconclusive-bg)" }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={finding.status} />
        <TrustTierBadge tier={finding.trust_tier} />
        <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--faint)" }}>
          {finding.verifier_id}
        </span>
      </div>
      {finding.message && (
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
          {finding.message}
        </p>
      )}
      {ev.quote && (
        <blockquote
          className="mt-3 border-l-2 pl-3 text-sm italic leading-relaxed"
          style={{ borderColor: "var(--border-strong)", color: "var(--muted)" }}
        >
          “{ev.quote}”
        </blockquote>
      )}
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
