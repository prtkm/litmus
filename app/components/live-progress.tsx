"use client";

// The live in-flight view for a paper that is still being audited (DESIGN §2).
// A paper that has a row but no finished report yet — status queued / extracting
// / auditing / confirming, or error — renders THIS instead of 404-ing. It polls
// the lightweight GET /api/papers/[id]/status every ~2.5s and shows:
//
//   · the PIPELINE_STAGES strip (reusing lib/labels so the labels match the
//     worker's status enum exactly) highlighted to the live status;
//   · a compact, append-only feed of the worker's progress.events
//     (agent_started / persona / tool_use / tool_result / classification);
//   · an executor-honesty line — whether the audit ran on the managed-agents
//     executor or fell back to a local run (progress.executor:
//     "managed" vs "managed:fallback-local").
//
// On status==='done' it calls router.refresh() so the server re-renders the page
// and the full two-band audit report replaces this view (App Router merges the
// new RSC payload without a full reload). On status==='error' it shows the error
// text and STOPS polling. The interval is always cleared on unmount.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { PIPELINE_STAGES } from "@/lib/labels";
import type { PaperStatus } from "@/lib/types";
import type { PaperStatusInfo } from "@/lib/data";

const POLL_MS = 2500;

// --- The progress JSONB the worker writes. The worker owns the exact shape, so
// we read it DEFENSIVELY: every field is optional and we never assume more than
// the documented {step, pct, events:[{kind,payload,…}], seq, executor, error}.
interface ProgressEvent {
  kind?: string;
  payload?: unknown;
  // Common flat fields the worker may set directly on an event instead of (or in
  // addition to) payload — read leniently so a label always has something to show.
  text?: string;
  message?: string;
  label?: string;
  name?: string;
  tool?: string;
  persona?: string;
  classification?: string;
  seq?: number;
  ts?: string | number;
  [k: string]: unknown;
}

interface Progress {
  step?: string;
  pct?: number;
  events?: ProgressEvent[];
  seq?: number;
  executor?: string;
  error?: string;
  // The pipeline stage that was in flight when the audit failed (PIPELINE_STAGES has no 'error'
  // entry, so on error we use this to mark which stage failed). Written by the worker's fail().
  failed_step?: string;
}

function asProgress(p: unknown): Progress {
  return p && typeof p === "object" ? (p as Progress) : {};
}

// Terminal pipeline states — once we see one, stop the poll loop.
const TERMINAL: ReadonlySet<PaperStatus> = new Set<PaperStatus>(["done", "error"]);

export function LiveProgress({
  paperId,
  initial,
}: {
  paperId: string;
  initial: PaperStatusInfo;
}) {
  const router = useRouter();
  const [info, setInfo] = useState<PaperStatusInfo>(initial);
  // Guard against overlapping fetches and against state updates after unmount.
  const inFlight = useRef(false);
  const stopped = useRef(false);
  // Latch so we only ever call router.refresh() once when we first hit `done`.
  const refreshed = useRef(false);

  useEffect(() => {
    stopped.current = false;

    async function tick() {
      if (stopped.current || inFlight.current) return;
      inFlight.current = true;
      try {
        const res = await fetch(
          `/api/papers/${encodeURIComponent(paperId)}/status`,
          { cache: "no-store" },
        );
        if (!res.ok) return; // transient (e.g. 404 just before the row lands) — try again next tick
        const next = (await res.json()) as PaperStatusInfo;
        if (stopped.current) return;
        setInfo(next);

        if (next.status === "done") {
          // The report exists now — pull the server-rendered two-band audit.
          // router.refresh() re-renders the Server Component for this route and
          // merges the new RSC payload (App Router), swapping this view out.
          if (!refreshed.current) {
            refreshed.current = true;
            router.refresh();
          }
          stopPolling();
        } else if (next.status === "error") {
          stopPolling();
        }
      } catch {
        // Network blip — keep polling; the next tick may succeed.
      } finally {
        inFlight.current = false;
      }
    }

    function stopPolling() {
      stopped.current = true;
      if (timer) clearInterval(timer);
    }

    // If we were already handed a terminal state, don't start the loop.
    let timer: ReturnType<typeof setInterval> | undefined;
    if (initial.status === "done" && !refreshed.current) {
      refreshed.current = true;
      router.refresh();
    } else if (!TERMINAL.has(initial.status)) {
      timer = setInterval(tick, POLL_MS);
      // Kick once immediately so the first update doesn't wait a full interval.
      void tick();
    }

    return () => {
      stopped.current = true;
      if (timer) clearInterval(timer);
    };
    // paperId/initial are stable for the lifetime of this mounted page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paperId]);

  const progress = asProgress(info.progress);
  // Error text can live at the row level (info.error) or inside the progress blob.
  const errorText = info.status === "error" ? info.error ?? progress.error ?? null : null;
  const events = Array.isArray(progress.events) ? progress.events : [];

  return (
    <div className="max-w-3xl space-y-8">
      <header className="space-y-2">
        <Link href="/" className="text-xs underline" style={{ color: "var(--muted)" }}>
          ← All papers
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {info.status === "error" ? "Audit failed" : "Auditing this paper…"}
          </h1>
          {info.status !== "error" && <Spinner />}
        </div>
        <p className="text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          {info.status === "error"
            ? "The audit pipeline hit an error before it could finish. The detail is below."
            : "LITMUS is reading the paper, routing each claim to verifiers, and confirming every candidate flag before it surfaces. This view updates itself — when the audit finishes, the full report loads here automatically."}
        </p>
      </header>

      <ExecutorLine executor={progress.executor} />

      {errorText && (
        <div
          className="space-y-2 rounded-xl border p-4"
          style={{ borderColor: "var(--fail-border)", background: "var(--fail-bg)" }}
        >
          <div className="text-sm font-semibold" style={{ color: "var(--fail)" }}>
            Error
          </div>
          <pre
            className="overflow-x-auto whitespace-pre-wrap text-xs leading-relaxed"
            style={{ color: "var(--foreground)" }}
          >
            {errorText}
          </pre>
        </div>
      )}

      <StageStrip status={info.status} pct={progress.pct} failedStep={progress.failed_step} />

      {events.length > 0 && <EventFeed events={events} />}
    </div>
  );
}

function Spinner() {
  return (
    <span
      aria-label="working"
      role="status"
      className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
      style={{ color: "var(--tier-deterministic)" }}
    />
  );
}

// The executor-honesty line (DESIGN: never overstate). The worker records which
// executor actually ran the audit. "managed:fallback-local" means the managed-
// agents path was unavailable and the audit fell back to a local run — we SAY so
// rather than imply a hosted agent did the work.
function ExecutorLine({ executor }: { executor?: string }) {
  if (!executor) return null;
  const fallback = executor.includes("fallback") || executor.includes("local");
  return (
    <div
      className="flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-xs"
      style={{
        borderColor: fallback ? "var(--tier-advisory-border)" : "var(--tier-deterministic-border)",
        background: fallback ? "var(--tier-advisory-bg)" : "var(--tier-deterministic-bg)",
      }}
    >
      <span
        className="font-medium"
        style={{ color: fallback ? "var(--tier-advisory)" : "var(--tier-deterministic)" }}
      >
        {fallback ? "Local fallback" : "Managed agent"}
      </span>
      <span style={{ color: "var(--muted)" }}>
        {fallback
          ? "the managed-agents path was unavailable, so this audit is running on a local executor"
          : "this audit is running on the Claude managed-agents executor"}
      </span>
      <span className="ml-auto font-mono" style={{ color: "var(--faint)" }} title="progress.executor">
        {executor}
      </span>
    </div>
  );
}

// The pipeline strip, reusing PIPELINE_STAGES from lib/labels so the stage ids /
// labels match the worker's PaperStatus enum exactly. The live status highlights
// the current stage; earlier stages read as done, later ones as pending. An
// `error` status keeps the last-known stage but tints the strip as failed.
function StageStrip({
  status,
  pct,
  failedStep,
}: {
  status: PaperStatus;
  pct?: number;
  failedStep?: string;
}) {
  const isError = status === "error";
  // Index of the live stage. PIPELINE_STAGES has no 'error' entry, so on error findIndex(status)
  // is -1; fall back to the stage the worker recorded as in-flight when it failed (progress
  // .failed_step), else 'auditing', so the strip always marks a concrete failure point.
  let activeIdx = PIPELINE_STAGES.findIndex((s) => s.id === status);
  if (isError && activeIdx < 0) {
    activeIdx = failedStep ? PIPELINE_STAGES.findIndex((s) => s.id === failedStep) : -1;
    if (activeIdx < 0) activeIdx = PIPELINE_STAGES.findIndex((s) => s.id === "auditing");
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide" style={{ color: "var(--faint)" }}>
          Pipeline
        </h2>
        {typeof pct === "number" && !isError && (
          <span className="font-mono text-xs" style={{ color: "var(--muted)" }}>
            {Math.round(Math.max(0, Math.min(100, pct)))}%
          </span>
        )}
      </div>
      <ol className="space-y-2">
        {PIPELINE_STAGES.map((stage, i) => {
          const done = activeIdx >= 0 && i < activeIdx;
          const active = !isError && i === activeIdx;
          // On error, mark the stage matching the (last) status as the failed one.
          const failed = isError && i === activeIdx;

          const dotBg = failed
            ? "var(--fail)"
            : active
              ? "var(--tier-deterministic)"
              : done
                ? "var(--pass)"
                : "var(--surface-2)";
          const dotColor = failed || active || done ? "#fff" : "var(--faint)";
          const dotBorder = failed
            ? "var(--fail)"
            : active
              ? "var(--tier-deterministic)"
              : done
                ? "var(--pass)"
                : "var(--border-strong)";

          return (
            <li
              key={stage.id}
              className="flex gap-3 rounded-lg border p-3"
              style={{
                borderColor: active
                  ? "var(--tier-deterministic-border)"
                  : failed
                    ? "var(--fail-border)"
                    : "var(--border)",
                background: active
                  ? "var(--tier-deterministic-bg)"
                  : failed
                    ? "var(--fail-bg)"
                    : "var(--surface)",
              }}
            >
              <span
                className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold"
                style={{ background: dotBg, color: dotColor, border: `1px solid ${dotBorder}` }}
              >
                {done ? "✓" : failed ? "!" : i + 1}
              </span>
              <div>
                <div className="text-sm font-medium" style={{ color: "var(--foreground)" }}>
                  {stage.label}
                  {active && (
                    <span className="ml-2 text-xs font-normal" style={{ color: "var(--tier-deterministic)" }}>
                      ← in progress
                    </span>
                  )}
                  {failed && (
                    <span className="ml-2 text-xs font-normal" style={{ color: "var(--fail)" }}>
                      ← failed here
                    </span>
                  )}
                </div>
                <p className="text-xs leading-relaxed" style={{ color: "var(--muted)" }}>
                  {stage.blurb}
                </p>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

// A compact, newest-last feed of the worker's progress events. Each event kind
// gets a plain-language one-liner; the raw kind rides along as a muted tag. We
// read the payload leniently (string, or an object with a text/message/label/…
// field) so a row always renders something even as the worker's shape evolves.
function EventFeed({ events }: { events: ProgressEvent[] }) {
  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold uppercase tracking-wide" style={{ color: "var(--faint)" }}>
        Activity
      </h2>
      <ol className="space-y-1.5">
        {events.map((ev, i) => (
          <EventRow key={eventKey(ev, i)} ev={ev} />
        ))}
      </ol>
    </section>
  );
}

function eventKey(ev: ProgressEvent, i: number): string {
  const seq = typeof ev.seq === "number" ? ev.seq : i;
  return `${ev.kind ?? "event"}-${seq}-${i}`;
}

// Plain-language framing per documented event kind. Unknown kinds fall back to a
// humanized form so a new worker event never renders as a raw token.
const KIND_META: Record<string, { label: string; tone: "info" | "tool" | "persona" | "class" }> = {
  agent_started: { label: "Agent started", tone: "info" },
  persona: { label: "Reviewer", tone: "persona" },
  tool_use: { label: "Tool", tone: "tool" },
  tool_result: { label: "Tool result", tone: "tool" },
  classification: { label: "Classification", tone: "class" },
};

function kindMeta(kind?: string): { label: string; tone: "info" | "tool" | "persona" | "class" } {
  if (kind && KIND_META[kind]) return KIND_META[kind];
  if (!kind) return { label: "Event", tone: "info" };
  // humanize: agent_started → "Agent Started"
  const label = kind
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return { label, tone: "info" };
}

function toneColor(tone: "info" | "tool" | "persona" | "class"): string {
  switch (tone) {
    case "tool":
      return "var(--tier-deterministic)";
    case "persona":
      return "var(--tier-advisory)";
    case "class":
      return "var(--pass)";
    default:
      return "var(--muted)";
  }
}

// Pull a human string out of an event, tolerating several shapes:
//   · payload is a string                      → use it
//   · payload is an object w/ text/message/…   → use that field
//   · flat fields on the event itself          → persona / tool / classification
function eventText(ev: ProgressEvent): string {
  const p = ev.payload;
  if (typeof p === "string") return p;
  if (p && typeof p === "object") {
    const o = p as Record<string, unknown>;
    for (const k of ["text", "message", "label", "name", "summary", "tool", "persona", "classification", "value"]) {
      const v = o[k];
      if (typeof v === "string" && v.trim()) return v;
    }
  }
  for (const k of ["text", "message", "label", "name", "tool", "persona", "classification", "value"] as const) {
    const v = ev[k];
    if (typeof v === "string" && v.trim()) return v;
  }
  return "";
}

function EventRow({ ev }: { ev: ProgressEvent }) {
  const meta = kindMeta(ev.kind);
  const color = toneColor(meta.tone);
  const text = eventText(ev);
  return (
    <li
      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-md border px-3 py-1.5 text-xs"
      style={{ borderColor: "var(--border)", background: "var(--surface)" }}
    >
      <span
        className="shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide"
        style={{ color, borderColor: "var(--border-strong)", background: "var(--surface-2)" }}
        title={ev.kind ? `event kind: ${ev.kind}` : undefined}
      >
        {meta.label}
      </span>
      {text ? (
        <span style={{ color: "var(--foreground)" }}>{text}</span>
      ) : (
        <span style={{ color: "var(--faint)" }}>(no detail)</span>
      )}
    </li>
  );
}
