// Lazy, singleton Pyodide loader. Pyodide is large (~10 MB), so we only fetch it
// the first time a reader clicks ▶ "Run it yourself", and reuse the one
// interpreter for every subsequent run on the page.
//
// Loaded from the official jsDelivr CDN (DESIGN §14): in-browser recompute for
// stdlib/scipy scripts. Scripts with declared dependencies are NOT run here
// (the UI shows "native-only" instead).

// Pin a version so the loaded indexURL and the injected <script> agree.
const PYODIDE_VERSION = "0.28.3";
const CDN_BASE = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

// Minimal shape of the pieces of the Pyodide API we touch.
export interface PyodideInterface {
  runPythonAsync: (code: string) => Promise<unknown>;
  setStdout: (opts: { batched?: (s: string) => void }) => void;
  setStderr: (opts: { batched?: (s: string) => void }) => void;
}

declare global {
  interface Window {
    loadPyodide?: (opts: { indexURL: string }) => Promise<PyodideInterface>;
  }
}

let pyodidePromise: Promise<PyodideInterface> | null = null;

function injectScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(
      `script[data-pyodide]`,
    );
    if (existing) {
      if (existing.dataset.loaded === "true") return resolve();
      existing.addEventListener("load", () => resolve());
      existing.addEventListener("error", () =>
        reject(new Error("Failed to load Pyodide script")),
      );
      return;
    }
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.dataset.pyodide = "true";
    s.addEventListener("load", () => {
      s.dataset.loaded = "true";
      resolve();
    });
    s.addEventListener("error", () =>
      reject(new Error("Failed to load Pyodide from CDN")),
    );
    document.head.appendChild(s);
  });
}

/** Load (or reuse) the Pyodide interpreter. Browser-only. */
export function getPyodide(): Promise<PyodideInterface> {
  if (typeof window === "undefined") {
    return Promise.reject(new Error("Pyodide is browser-only"));
  }
  if (!pyodidePromise) {
    pyodidePromise = (async () => {
      await injectScript(`${CDN_BASE}pyodide.js`);
      if (!window.loadPyodide) {
        throw new Error("loadPyodide not available after script load");
      }
      return window.loadPyodide({ indexURL: CDN_BASE });
    })().catch((e) => {
      // Reset so a later click can retry after a transient CDN failure.
      pyodidePromise = null;
      throw e;
    });
  }
  return pyodidePromise;
}

export interface RunResult {
  ok: boolean; // stdout === expected_output
  stdout: string;
  stderr: string;
  error?: string; // python exception text, if the script threw
}

/**
 * Run `script`, capture stdout/stderr, and compare stdout to `expected`.
 * Comparison tolerates a single trailing-newline difference (expected_output
 * may or may not end in "\n").
 */
export async function runScript(
  script: string,
  expected: string,
): Promise<RunResult> {
  const py = await getPyodide();
  let stdout = "";
  let stderr = "";
  py.setStdout({ batched: (s) => (stdout += s + "\n") });
  py.setStderr({ batched: (s) => (stderr += s + "\n") });

  let error: string | undefined;
  try {
    await py.runPythonAsync(script);
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  const norm = (s: string) => s.replace(/\n+$/, "");
  const ok = !error && norm(stdout) === norm(expected);
  return { ok, stdout, stderr, error };
}
