// Presentational badges. No state — usable in Server Components.
// Colors come from CSS variables (lib/labels.ts) so each trust tier stays
// visually distinct and never blurs (DESIGN §3.6).

import type { FindingStatus, Severity, TrustTier } from "@/lib/types";
import {
  SEVERITY_STYLE,
  STATUS_STYLE,
  TIER_STYLE,
} from "@/lib/labels";

function Pill({
  fg,
  bg,
  border,
  children,
  title,
  dot = false,
}: {
  fg: string;
  bg: string;
  border: string;
  children: React.ReactNode;
  title?: string;
  dot?: boolean;
}) {
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium leading-5 whitespace-nowrap"
      style={{ color: fg, backgroundColor: bg, borderColor: border }}
    >
      {dot && (
        <span
          aria-hidden
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: fg }}
        />
      )}
      {children}
    </span>
  );
}

export function TrustTierBadge({ tier }: { tier: TrustTier }) {
  const s = TIER_STYLE[tier];
  return (
    <Pill fg={s.fg} bg={s.bg} border={s.border} title={s.hint} dot>
      {s.label}
    </Pill>
  );
}

export function StatusBadge({ status }: { status: FindingStatus }) {
  const s = STATUS_STYLE[status];
  return (
    <Pill fg={s.fg} bg={s.bg} border={s.border}>
      {s.label}
    </Pill>
  );
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  const s = SEVERITY_STYLE[severity];
  return (
    <Pill fg={s.fg} bg={s.bg} border={s.border} title={s.hint}>
      {s.label}
    </Pill>
  );
}
