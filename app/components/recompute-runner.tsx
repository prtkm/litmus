"use client";

// The ▶ "Run it yourself" control. Runs a finding's recompute_script IN-BROWSER
// via Pyodide (DESIGN §14), captures stdout, and compares it to the declared
// expected_output: ✓ reproduced / ✗ with a diff. Only stdlib-only scripts
// (script_dependencies == []) are runnable here; anything with declared
// dependencies is shown as "native-only".

import { useState } from "react";
import { runScript, type RunResult } from "@/lib/pyodide/load";

type Phase = "idle" | "loading" | "running" | "done" | "failed";

export function RecomputeRunner({
  script,
  expectedOutput,
  dependencies,
}: {
  script: string;
  expectedOutput: string;
  dependencies: string[];
}) {
  const runnable = Array.isArray(dependencies) && dependencies.length === 0;
  const [phase, setPhase] = useState<Phase>("idle");
  const [result, setResult] = useState<RunResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  async function onRun() {
    setResult(null);
    setLoadError(null);
    // First run pays the Pyodide download; reflect that in the label.
    setPhase("loading");
    try {
      setPhase("running");
      const res = await runScript(script, expectedOutput);
      setResult(res);
      setPhase("done");
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
      setPhase("failed");
    }
  }

  if (!runnable) {
    return (
      <div className="mt-3">
        <span
          className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs"
          style={{
            borderColor: "var(--border-strong)",
            color: "var(--muted)",
            background: "var(--surface-2)",
          }}
          title={`Declares dependencies (${dependencies.join(", ")}). Reproduce it in a native Python environment.`}
        >
          native-only — depends on {dependencies.join(", ")}
        </span>
      </div>
    );
  }

  const busy = phase === "loading" || phase === "running";

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={onRun}
          disabled={busy}
          className="inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium text-white transition-opacity disabled:opacity-60"
          style={{ background: "var(--tier-deterministic)" }}
        >
          <span aria-hidden>▶</span>
          {phase === "loading"
            ? "Loading Python…"
            : phase === "running"
              ? "Running…"
              : result
                ? "Run again"
                : "Run it yourself"}
        </button>

        {phase === "loading" && (
          <span className="text-xs" style={{ color: "var(--faint)" }}>
            fetching Pyodide from the CDN (first run only)…
          </span>
        )}

        {result && !result.error && (
          <Verdict ok={result.ok} />
        )}
        {result?.error && (
          <span className="text-xs font-medium" style={{ color: "var(--fail)" }}>
            ✗ script error
          </span>
        )}
        {loadError && (
          <span className="text-xs" style={{ color: "var(--fail)" }}>
            couldn’t load Pyodide: {loadError}
          </span>
        )}
      </div>

      {result && (
        <OutputPanels result={result} expected={expectedOutput} />
      )}
    </div>
  );
}

function Verdict({ ok }: { ok: boolean }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-semibold"
      style={{
        color: ok ? "var(--pass)" : "var(--fail)",
        background: ok ? "var(--pass-bg)" : "var(--fail-bg)",
        borderColor: ok ? "var(--pass-border)" : "var(--fail-border)",
      }}
    >
      {ok ? "✓ reproduced — output matches" : "✗ output differs from expected"}
    </span>
  );
}

function OutputPanels({
  result,
  expected,
}: {
  result: RunResult;
  expected: string;
}) {
  const mismatch = !result.ok && !result.error;
  return (
    <div className="mt-3 grid gap-3 sm:grid-cols-2">
      <Panel label="your output (this browser)" tone={result.ok ? "ok" : "bad"}>
        {result.stdout || "(no output)"}
        {result.error ? `\n\n[python error]\n${result.error}` : ""}
        {result.stderr ? `\n[stderr]\n${result.stderr}` : ""}
      </Panel>
      <Panel label="expected output (from the audit)" tone="neutral">
        {expected}
      </Panel>
      {mismatch && (
        <p
          className="sm:col-span-2 text-xs"
          style={{ color: "var(--faint)" }}
        >
          The two panels differ — your run did not reproduce the recorded
          expected output. (For the bundled fixtures they should match exactly.)
        </p>
      )}
    </div>
  );
}

function Panel({
  label,
  tone,
  children,
}: {
  label: string;
  tone: "ok" | "bad" | "neutral";
  children: React.ReactNode;
}) {
  const border =
    tone === "ok"
      ? "var(--pass-border)"
      : tone === "bad"
        ? "var(--fail-border)"
        : "var(--border)";
  return (
    <div>
      <div
        className="mb-1 text-[11px] font-medium uppercase tracking-wide"
        style={{ color: "var(--faint)" }}
      >
        {label}
      </div>
      <pre
        className="overflow-x-auto rounded-md border p-3 text-xs leading-relaxed"
        style={{ borderColor: border, background: "var(--surface-2)" }}
      >
        {children}
      </pre>
    </div>
  );
}
