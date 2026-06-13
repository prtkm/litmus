"use client";

// Upload a paper for auditing (DESIGN §2). A file picker + the pipeline
// explanation (queued → extracting → auditing → confirming → done) POSTing to
// /api/upload. The route handler is a STUB today: it returns {status, id} and
// leaves a TODO for the WS-H managed-agents pipeline. So this page accepts a
// PDF, shows what *will* happen to it, and reports the queued id — no audit yet.

import { useState } from "react";
import Link from "next/link";
import { PIPELINE_STAGES } from "@/lib/labels";

interface UploadResponse {
  status: string;
  id: string;
  message?: string;
}

type State =
  | { phase: "idle" }
  | { phase: "submitting" }
  | { phase: "queued"; res: UploadResponse }
  | { phase: "error"; message: string };

export default function UploadPage() {
  const [file, setFile] = useState<File | null>(null);
  const [doi, setDoi] = useState("");
  const [state, setState] = useState<State>({ phase: "idle" });

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setState({ phase: "submitting" });
    try {
      const form = new FormData();
      form.append("file", file);
      if (doi.trim()) form.append("doi", doi.trim());

      const res = await fetch("/api/upload", { method: "POST", body: form });
      const body = (await res.json()) as UploadResponse & { error?: string };
      if (!res.ok) {
        throw new Error(body.error || `Upload failed (${res.status})`);
      }
      setState({ phase: "queued", res: body });
    } catch (err) {
      setState({
        phase: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  const submitting = state.phase === "submitting";

  return (
    <div className="max-w-2xl space-y-8">
      <header className="space-y-2">
        <Link href="/" className="text-xs underline" style={{ color: "var(--muted)" }}>
          ← All papers
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">Audit a new paper</h1>
        <p className="text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          Upload a paper PDF (optionally with its DOI). LITMUS extracts a
          claim graph, routes each claim to verifiers, and confirms every
          candidate flag on a fresh read before it surfaces.
        </p>
      </header>

      {state.phase === "queued" ? (
        <QueuedPanel res={state.res} />
      ) : (
        <form
          onSubmit={onSubmit}
          className="space-y-4 rounded-xl border p-5"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          <div>
            <label
              htmlFor="file"
              className="mb-1.5 block text-xs font-medium uppercase tracking-wide"
              style={{ color: "var(--faint)" }}
            >
              Paper PDF
            </label>
            <input
              id="file"
              name="file"
              type="file"
              accept="application/pdf,.pdf"
              required
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="block w-full text-sm file:mr-3 file:rounded-md file:border file:px-3 file:py-1.5 file:text-sm"
              style={{ color: "var(--foreground)" }}
            />
            {file && (
              <p className="mt-1.5 text-xs" style={{ color: "var(--faint)" }}>
                {file.name} · {(file.size / 1024 / 1024).toFixed(2)} MB
              </p>
            )}
          </div>

          <div>
            <label
              htmlFor="doi"
              className="mb-1.5 block text-xs font-medium uppercase tracking-wide"
              style={{ color: "var(--faint)" }}
            >
              DOI <span className="normal-case">(optional)</span>
            </label>
            <input
              id="doi"
              name="doi"
              type="text"
              placeholder="10.1021/example.0000"
              value={doi}
              onChange={(e) => setDoi(e.target.value)}
              className="block w-full rounded-md border px-3 py-1.5 text-sm font-mono"
              style={{
                borderColor: "var(--border-strong)",
                background: "var(--surface-2)",
                color: "var(--foreground)",
              }}
            />
            <p className="mt-1.5 text-xs" style={{ color: "var(--faint)" }}>
              Used as the cache key (content hash / DOI) so the same paper is
              never audited twice.
            </p>
          </div>

          <button
            type="submit"
            disabled={!file || submitting}
            className="inline-flex items-center gap-2 rounded-md px-4 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
            style={{ background: "var(--tier-deterministic)" }}
          >
            {submitting ? "Queuing…" : "Queue for audit"}
          </button>

          {state.phase === "error" && (
            <p className="text-sm" style={{ color: "var(--fail)" }}>
              {state.message}
            </p>
          )}
        </form>
      )}

      <Pipeline activeId={state.phase === "queued" ? "queued" : null} />
    </div>
  );
}

function QueuedPanel({ res }: { res: UploadResponse }) {
  return (
    <div
      className="space-y-3 rounded-xl border p-5"
      style={{ borderColor: "var(--pass-border)", background: "var(--pass-bg)" }}
    >
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold" style={{ color: "var(--pass)" }}>
          ✓ Queued
        </span>
      </div>
      <p className="text-sm leading-relaxed" style={{ color: "var(--foreground)" }}>
        {res.message ??
          "Your paper has been accepted. The audit pipeline is not wired up in this build, so it will sit at the queued stage."}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt style={{ color: "var(--faint)" }}>id</dt>
        <dd className="font-mono" style={{ color: "var(--foreground)" }}>
          {res.id}
        </dd>
        <dt style={{ color: "var(--faint)" }}>status</dt>
        <dd className="font-mono" style={{ color: "var(--foreground)" }}>
          {res.status}
        </dd>
      </dl>
      <Link href="/" className="inline-block text-sm underline" style={{ color: "var(--muted)" }}>
        Back to the gallery
      </Link>
    </div>
  );
}

function Pipeline({ activeId }: { activeId: string | null }) {
  return (
    <section>
      <h2
        className="mb-3 text-xs font-semibold uppercase tracking-wide"
        style={{ color: "var(--faint)" }}
      >
        What happens next
      </h2>
      <ol className="space-y-2">
        {PIPELINE_STAGES.map((stage, i) => {
          const active = activeId === stage.id;
          return (
            <li
              key={stage.id}
              className="flex gap-3 rounded-lg border p-3"
              style={{
                borderColor: active ? "var(--tier-deterministic-border)" : "var(--border)",
                background: active ? "var(--tier-deterministic-bg)" : "var(--surface)",
              }}
            >
              <span
                className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold"
                style={{
                  background: active ? "var(--tier-deterministic)" : "var(--surface-2)",
                  color: active ? "#fff" : "var(--faint)",
                  border: `1px solid ${active ? "var(--tier-deterministic)" : "var(--border-strong)"}`,
                }}
              >
                {i + 1}
              </span>
              <div>
                <div className="text-sm font-medium" style={{ color: "var(--foreground)" }}>
                  {stage.label}
                  {active && (
                    <span className="ml-2 text-xs font-normal" style={{ color: "var(--tier-deterministic)" }}>
                      ← you are here
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
