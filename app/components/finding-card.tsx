"use client";

// A single CHECKABLE finding (DESIGN §14): quote → reported vs computed →
// discrepancy → trust-tier badge + severity → the recompute script with a ▶
// run-it-yourself button. This is the heart of the product: a verdict you can
// re-derive, not just read.

import { useState } from "react";
import type { Finding } from "@/lib/types";
import { SeverityBadge, StatusBadge, TrustTierBadge } from "@/components/badges";
import { RecomputeRunner } from "@/components/recompute-runner";
import { verifierKindLabel, epistemicTier } from "@/lib/labels";

// Decimal places in a number printed plainly (6.62 -> 2, 7 -> 0). Used to render a long recomputed
// float at the SAME precision the paper reported, so e.g. a GRIM "recomputed" reads "6.61", not the
// raw "6.612903225806452" (which looks like a third-decimal rounding nit, not the real claim).
function decimalsOf(v: unknown): number | null {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  const s = String(v);
  const i = s.indexOf(".");
  return i < 0 ? 0 : s.length - i - 1;
}

// What to show in the "Recomputed" box. Prefer the verifier's own formatted value (grim's
// details.nearest_mean_str); else round a long float to the reported value's precision.
function recomputedDisplay(finding: Finding): unknown {
  const pretty = finding.details?.nearest_mean_str as string | undefined;
  if (typeof pretty === "string" && pretty) return pretty;
  const c = finding.computed;
  if (typeof c === "number" && Number.isFinite(c) && !Number.isInteger(c)) {
    const dec = decimalsOf(finding.reported);
    if (dec !== null && dec < 6) return c.toFixed(dec);
  }
  return c;
}

export function FindingCard({ finding }: { finding: Finding }) {
  const ev = finding.evidence ?? {};
  const script = ev.recompute_script ?? "";
  const expected = ev.expected_output ?? "";
  const deps = ev.script_dependencies ?? [];
  const tier = epistemicTier(
    (finding.details?.epistemic_tier as string | undefined) ?? null,
  );
  const [showScript, setShowScript] = useState(false);

  return (
    <article
      className="rounded-xl border"
      style={{ borderColor: "var(--fail-border)", background: "var(--surface)" }}
    >
      <div className="border-b p-4" style={{ borderColor: "var(--border)" }}>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={finding.status} />
          {finding.severity && <SeverityBadge severity={finding.severity} />}
          <TrustTierBadge tier={finding.trust_tier} />
          {tier && (
            <span
              className="text-[11px]"
              style={{ color: "var(--faint)" }}
              title={`${tier.hint} (tier ${tier.code})`}
            >
              {tier.phrase} check
            </span>
          )}
          <span
            className="ml-auto font-mono text-[11px]"
            style={{ color: "var(--faint)" }}
            title={`verifier ${finding.verifier_id} · ${verifierKindLabel(finding.verifier_kind)}`}
          >
            {verifierKindLabel(finding.verifier_kind)}
          </span>
        </div>

        {finding.message && (
          <p className="mt-3 text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
            {finding.message}
          </p>
        )}
      </div>

      <div className="space-y-4 p-4">
        {ev.quote && (
          <Block label="Quote from the paper">
            <blockquote
              className="border-l-2 pl-3 text-sm italic leading-relaxed"
              style={{ borderColor: "var(--border-strong)", color: "var(--muted)" }}
            >
              “{ev.quote}”
            </blockquote>
            {ev.location && (
              <p className="mt-1 text-xs" style={{ color: "var(--faint)" }}>
                {locationLine(ev.location)}
              </p>
            )}
          </Block>
        )}

        {(finding.reported !== undefined || finding.computed !== undefined) && (
          <div className="grid gap-3 sm:grid-cols-2">
            <ValueBox label="Reported" value={finding.reported} tone="reported" />
            <ValueBox label="Recomputed" value={recomputedDisplay(finding)} tone="computed" />
          </div>
        )}

        {finding.discrepancy && (
          <Block label="Discrepancy">
            <p className="text-sm font-medium" style={{ color: "var(--fail)" }}>
              {finding.discrepancy}
            </p>
          </Block>
        )}

        {script && (
          <div>
            <div className="flex items-center justify-between">
              <span
                className="text-[11px] font-medium uppercase tracking-wide"
                style={{ color: "var(--faint)" }}
              >
                Recompute script
              </span>
              <button
                type="button"
                onClick={() => setShowScript((v) => !v)}
                className="text-xs underline"
                style={{ color: "var(--muted)" }}
              >
                {showScript ? "hide" : "show"} script
              </button>
            </div>
            {showScript && (
              <pre
                className="mt-2 overflow-x-auto rounded-md border p-3 text-xs leading-relaxed"
                style={{ borderColor: "var(--border)", background: "var(--surface-2)" }}
              >
                {script}
              </pre>
            )}
            <RecomputeRunner
              script={script}
              expectedOutput={expected}
              dependencies={deps}
            />
          </div>
        )}
      </div>
    </article>
  );
}

function Block({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div
        className="mb-1 text-[11px] font-medium uppercase tracking-wide"
        style={{ color: "var(--faint)" }}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

function ValueBox({
  label,
  value,
  tone,
}: {
  label: string;
  value: unknown;
  tone: "reported" | "computed";
}) {
  return (
    <div
      className="rounded-md border p-3"
      style={{
        borderColor: tone === "reported" ? "var(--fail-border)" : "var(--pass-border)",
        background: tone === "reported" ? "var(--fail-bg)" : "var(--pass-bg)",
      }}
    >
      <div
        className="text-[11px] font-medium uppercase tracking-wide"
        style={{ color: "var(--faint)" }}
      >
        {label}
      </div>
      <div
        className="mt-1 font-mono text-sm font-semibold"
        style={{ color: tone === "reported" ? "var(--fail)" : "var(--pass)" }}
      >
        {formatValue(value)}
      </div>
    </div>
  );
}

function formatValue(v: unknown): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function locationLine(loc: NonNullable<Finding["evidence"]>["location"]): string {
  if (!loc) return "";
  const parts: string[] = [];
  if (loc.section) parts.push(loc.section);
  if (typeof loc.page === "number") parts.push(`p. ${loc.page}`);
  if (loc.char_span) parts.push(`chars ${loc.char_span[0]}–${loc.char_span[1]}`);
  return parts.join(" · ");
}
