import Link from "next/link";
import type { PaperSummary, IssueCategory } from "@/lib/types";
import { CATEGORY_META, CATEGORY_ORDER } from "@/lib/labels";

// A gallery card summarizes an audit by CATEGORY (DESIGN §4, §5): both the
// deterministic catches ("quantitative issues" — reproducible) and the
// non-deterministic review ("overclaims", "method concerns", "integrity",
// "subjective") are shown as counts, so the value of each is legible at a glance.
export function PaperCard({ paper }: { paper: PaperSummary }) {
  const counts = paper.categories ?? ({} as Record<IssueCategory, number>);
  const cats = CATEGORY_ORDER.filter((c) => (counts[c] ?? 0) > 0);
  const quant = counts.quantitative ?? 0;
  const total = cats.reduce((s, c) => s + (counts[c] ?? 0), 0);

  return (
    <Link
      href={`/paper/${encodeURIComponent(paper.id)}`}
      className="group block rounded-xl border p-4 no-underline transition-colors hover:border-[var(--border-strong)]"
      style={{ borderColor: "var(--border)", background: "var(--surface)" }}
    >
      <div className="flex items-start justify-between gap-3">
        <span className="text-[11px] font-medium uppercase tracking-wide" style={{ color: "var(--faint)" }}>
          {paper.field}
        </span>
        <Headline quant={quant} total={total} />
      </div>

      <h2 className="mt-2 text-[15px] font-semibold leading-snug group-hover:underline">
        {paper.title}
      </h2>
      {paper.doi && (
        <p className="mt-1 font-mono text-xs" style={{ color: "var(--faint)" }}>
          {paper.doi}
        </p>
      )}

      {cats.length > 0 ? (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {cats.map((c) => (
            <CategoryChip key={c} cat={c} n={counts[c]} />
          ))}
        </div>
      ) : (
        <p className="mt-2.5 text-xs" style={{ color: "var(--ok)" }}>
          Nothing flagged — checks ran and the review found no concerns.
        </p>
      )}

      {(paper.passes > 0 || paper.reviewed_clean > 0) && (
        <p className="mt-2 text-[11px]" style={{ color: "var(--faint)" }}>
          {[
            paper.passes > 0 ? `${paper.passes} check${paper.passes === 1 ? "" : "s"} passed` : null,
            paper.reviewed_clean > 0 ? `${paper.reviewed_clean} reviewed, clean` : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
    </Link>
  );
}

// Top-right headline: lead with the deterministic catches (the hard, reproducible
// signal); else the count of reviewer concerns; else clean.
function Headline({ quant, total }: { quant: number; total: number }) {
  if (quant > 0) {
    return (
      <Pill fg="var(--fail)" bg="var(--fail-bg)" border="var(--fail-border)" title="Reproducible numeric errors — re-run them yourself">
        {quant} to re-run
      </Pill>
    );
  }
  if (total > 0) {
    return (
      <Pill fg="var(--tier-advisory)" bg="var(--tier-advisory-bg)" border="var(--tier-advisory-border)" title="Reviewer concerns to weigh">
        {total} to review
      </Pill>
    );
  }
  return (
    <Pill fg="var(--ok)" bg="var(--pass-bg)" border="var(--pass-border)">
      ✓ clean
    </Pill>
  );
}

function Pill({ children, fg, bg, border, title }: { children: React.ReactNode; fg: string; bg: string; border: string; title?: string }) {
  return (
    <span
      className="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold"
      style={{ color: fg, backgroundColor: bg, border: `1px solid ${border}` }}
      title={title}
    >
      {children}
    </span>
  );
}

function CategoryChip({ cat, n }: { cat: IssueCategory; n: number }) {
  const m = CATEGORY_META[cat];
  return (
    <span
      className="inline-flex items-baseline gap-1 rounded-md border px-2 py-0.5 text-[11px]"
      style={{ borderColor: m.border, background: m.bg }}
      title={m.blurb}
    >
      <span className="font-semibold" style={{ color: m.fg }}>
        {n}
      </span>
      <span style={{ color: "var(--muted)" }}>{n === 1 ? m.one : m.many}</span>
    </span>
  );
}
