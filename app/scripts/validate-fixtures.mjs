#!/usr/bin/env node
// Validate every fixture AuditReport against the LOCKED audit.schema.json.
// Also, as a contract check, verify that each stdlib-only `fail` finding's
// recompute_script actually prints its declared expected_output — the ✓/✗ the
// in-browser Pyodide runner depends on. Run: node scripts/validate-fixtures.mjs

import { readFileSync, readdirSync, writeFileSync, rmSync, mkdtempSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { execFileSync } from "node:child_process";
import Ajv from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

const __dirname = dirname(fileURLToPath(import.meta.url));
const appRoot = resolve(__dirname, "..");
const repoRoot = resolve(appRoot, "..");

const schemaPath = join(repoRoot, "litmus", "schemas", "audit.schema.json");
const fixturesDir = join(appRoot, "lib", "fixtures");

const schema = JSON.parse(readFileSync(schemaPath, "utf8"));
const ajv = new Ajv({ allErrors: true, strict: false });
addFormats(ajv);
const validate = ajv.compile(schema);

const files = readdirSync(fixturesDir).filter((f) => f.endsWith(".json"));
if (files.length === 0) {
  console.error("No JSON fixtures found in", fixturesDir);
  process.exit(1);
}

let failures = 0;
let scriptsChecked = 0;

// Collect every fail-finding (top-level + dropped_flags) that is stdlib-only,
// so we can confirm the script→expected_output contract the UI promises.
function collectRunnableFindings(report) {
  const out = [];
  const consider = (finding, origin) => {
    if (!finding || finding.status !== "fail") return;
    const ev = finding.evidence || {};
    const deps = ev.script_dependencies || [];
    if (Array.isArray(deps) && deps.length === 0 && ev.recompute_script) {
      out.push({ finding, origin });
    }
  };
  for (const f of report.findings || []) consider(f, "findings");
  for (const d of report.dropped_flags || []) consider(d.finding, "dropped_flags");
  return out;
}

let tmp;
try {
  tmp = mkdtempSync(join(tmpdir(), "litmus-fixtures-"));

  for (const file of files) {
    const full = join(fixturesDir, file);
    let report;
    try {
      report = JSON.parse(readFileSync(full, "utf8"));
    } catch (e) {
      console.error(`✗ ${file}: invalid JSON — ${e.message}`);
      failures++;
      continue;
    }

    const ok = validate(report);
    if (!ok) {
      console.error(`✗ ${file}: FAILS schema`);
      for (const err of validate.errors ?? []) {
        console.error(`    ${err.instancePath || "/"} ${err.message}`);
      }
      failures++;
      continue;
    }

    // Schema passed — now the recompute-script contract.
    let scriptNotes = [];
    for (const { finding, origin } of collectRunnableFindings(report)) {
      scriptsChecked++;
      const scriptFile = join(tmp, `${report.paper_id}-${finding.verifier_id}.py`);
      writeFileSync(scriptFile, finding.evidence.recompute_script);
      let actual;
      try {
        actual = execFileSync("python3", [scriptFile], {
          encoding: "utf8",
          timeout: 15000,
        });
      } catch (e) {
        console.error(
          `✗ ${file}: recompute_script for ${finding.verifier_id} (${origin}) errored — ${e.message}`,
        );
        failures++;
        continue;
      }
      const expected = finding.evidence.expected_output ?? "";
      if (actual !== expected) {
        console.error(
          `✗ ${file}: ${finding.verifier_id} (${origin}) expected_output mismatch`,
        );
        console.error(`    --- expected ---\n${expected}`);
        console.error(`    --- actual ---\n${actual}`);
        failures++;
      } else {
        scriptNotes.push(`${finding.verifier_id}✓`);
      }
    }

    const tail = scriptNotes.length ? `  [scripts: ${scriptNotes.join(" ")}]` : "";
    console.log(`✓ ${file}: valid against audit.schema.json${tail}`);
  }
} finally {
  if (tmp) rmSync(tmp, { recursive: true, force: true });
}

console.log(
  `\n${files.length - failures}/${files.length} fixtures valid; ${scriptsChecked} recompute script(s) checked.`,
);
process.exit(failures === 0 ? 0 : 1);
