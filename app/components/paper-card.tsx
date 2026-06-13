import Link from "next/link";
import type { PaperSummary } from "@/lib/types";
import { TrustTierBadge } from "@/components/badges";

export function PaperCard({ paper }: { paper: PaperSummary }) {
  const hasFlags = paper.flag_count > 0;
  return (
    <Link
      href={`/paper/${encodeURIComponent(paper.id)}`}
      className="group block rounded-xl border p-4 no-underline transition-colors hover:border-[var(--border-strong)]"
      style={{ borderColor: "var(--border)", background: "var(--surface)" }}
    >
      <div className="flex items-start justify-between gap-3">
        <span
          className="text-[11px] font-medium uppercase tracking-wide"
          style={{ color: "var(--faint)" }}
        >
          {paper.field}
        </span>
        <FlagCount count={paper.flag_count} />
      </div>

      <h2 className="mt-2 text-[15px] font-semibold leading-snug group-hover:underline">
        {paper.title}
      </h2>

      {paper.doi && (
        <p className="mt-1 font-mono text-xs" style={{ color: "var(--faint)" }}>
          {paper.doi}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {paper.trust_tiers.length > 0 ? (
          paper.trust_tiers.map((t) => <TrustTierBadge key={t} tier={t} />)
        ) : (
          <span
            className="text-xs"
            style={{ color: hasFlags ? "var(--fail)" : "var(--ok)" }}
          >
            {hasFlags ? "" : "No confirmed flags"}
          </span>
        )}
        {paper.routed_count > 0 && (
          <span className="text-xs" style={{ color: "var(--faint)" }}>
            · {paper.routed_count} routed to human
          </span>
        )}
      </div>
    </Link>
  );
}

function FlagCount({ count }: { count: number }) {
  const ok = count === 0;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold"
      style={{
        color: ok ? "var(--ok)" : "var(--fail)",
        backgroundColor: ok ? "var(--pass-bg)" : "var(--fail-bg)",
        border: `1px solid ${ok ? "var(--pass-border)" : "var(--fail-border)"}`,
      }}
    >
      {ok ? "✓ clean" : `${count} flag${count === 1 ? "" : "s"}`}
    </span>
  );
}
