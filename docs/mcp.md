# LITMUS as tools — the MCP server

LITMUS audits scientific papers and **proves every objection with a script you can run yourself.**
This server exposes that capability over the [Model Context Protocol](https://modelcontextprotocol.io)
so any AI science agent — a Claude managed-agents coordinator, a Claude Desktop session, your own
MCP client — can call LITMUS's checks as tools.

The differentiator, and the reason to wire LITMUS into an agent rather than another paper-checker:

> **Every verdict ships a recompute script.** LITMUS never asks your agent to trust an LLM's "this
> looks wrong." A flag is produced by deterministic code (DESIGN §3.1) and carries a self-contained
> `recompute_script` + the `expected_output` it must print. Your agent — or a skeptical human —
> reruns it in a clean, network-less sandbox and confirms the verdict independently (DESIGN §3.2:
> *no script, no flag*). The audit is falsifiable, not an opinion.

Concretely, when `check_statistic(test="t", statistic=2.0, df=20, reported_p=0.04)` flags a paper,
the response isn't "the p-value seems off" — it's a `status: "fail"`, the recomputed range
`p ∈ [0.0537, 0.0653]`, **and** a ~40-line stdlib Python program that re-derives that range and
reprints the discrepancy. Run it; you get the same answer. That is the contract for every tool here.

---

## What you get

| Tool | What it returns |
|------|-----------------|
| `list_verifiers()` | The catalog: every verifier's `{id, epistemic_tier, kind, determinism, consumes, capability_tags, fpr_ceiling, description}`. Route a claim by matching its type against `consumes`. |
| `run_verifier(verifier_id, claim, evidence)` | One verifier's deterministic `judge()` → a schema-valid `Finding`. |
| `audit_claim_graph(claim_graph)` | The **full pipeline** over an extracted ClaimGraph: plan → run all verifiers → re-run each flag's script in a fresh sandbox and **drop any that don't reproduce** → a schema-valid `AuditReport`. |
| `extract_claims(pdf_path)` | PDF → ClaimGraph via Opus (the only model-in-the-loop step; it transcribes and locates, never judges). *Needs `ANTHROPIC_API_KEY`.* |
| `audit_pdf(pdf_path)` | `extract_claims` + `audit_claim_graph` end-to-end → `AuditReport`. *Needs `ANTHROPIC_API_KEY`.* |
| `check_statistic` / `check_yield` / `check_grim` / `check_percent_change` | Convenience wrappers for the headline checks — pass plain numbers, they build the right `Evidence` and call the **real** verifier. |
| `calibration_scorecard()` | Each verifier's measured recall / FPR / determinism / reproducibility / admission (`scoring` \| `advisory` \| `rejected`), computed with zero human labels (DESIGN §7). How much to trust each tool — only a `scoring` verifier may render an A/B verdict. |
| `run_<id>` (auto-generated, one per verifier) | The same `judge()` as `run_verifier`, but a discoverable per-verifier tool generated from each manifest (DESIGN §6.3: one definition → CLI command **and** MCP tool). |

Every result is a plain JSON object. A bad call (unknown verifier, malformed evidence, missing API
key, a broken verifier) returns `{"error": "..."}` — the server never crashes (DESIGN §13).

### Trust tiers never collapse (DESIGN §3.6)

Each `Finding` carries a `trust_tier`: `deterministic_confirmed` > `calibrated_synthesized` >
`advisory_assisted` > `routed_to_human`. Your agent should treat them differently — a
`deterministic_confirmed` severity-A flag (e.g. a >100% yield) is a hard result; an
`advisory_assisted` one is a lead to check. Don't flatten them.

---

## Connect an agent

### Claude Code

```bash
claude mcp add litmus -- .venv/bin/python -m litmus.adapters.mcp
```

(For the extract/audit-PDF tools, make sure `ANTHROPIC_API_KEY` is set in the environment the
server inherits.)

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "litmus": {
      "command": "/absolute/path/to/litmus/.venv/bin/python",
      "args": ["-m", "litmus.adapters.mcp"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

The installed `litmus-mcp` console script is equivalent to `python -m litmus.adapters.mcp` and can
be used as the `command` instead.

### Programmatic MCP client (Python)

```python
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "litmus.adapters.mcp"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. See what's available.
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            # 2. Check a reported statistic.
            result = await session.call_tool(
                "check_statistic",
                {"test": "t", "statistic": 2.0, "df": 20, "reported_p": 0.04},
            )
            finding = result.structuredContent  # the Finding dict
            print(finding["status"], finding["discrepancy"])

            # 3. Reproduce the verdict yourself (the whole point).
            #    finding["recompute_script"] is a self-contained stdlib program;
            #    running it prints finding["expected_output"].


asyncio.run(main())
```

---

## Worked tool calls

### 1. `check_statistic` catches an inconsistent p-value

A paper reports `t(20) = 2.0, p = .04` and calls the result significant. But `t = 2.0` at `df = 20`
gives a two-tailed `p ≈ 0.059` — not significant. The reported p can't be produced by any rounding
of the statistic, **and** it flips the .05 decision, so LITMUS flags it severity B.

**Call**

```json
{ "test": "t", "statistic": 2.0, "df": 20, "reported_p": 0.04 }
```

**Response** (abridged — `recompute_script` is the full stdlib program)

```json
{
  "verifier_id": "statcheck.v1",
  "status": "fail",
  "is_flag": true,
  "severity": "B",
  "trust_tier": "deterministic_confirmed",
  "message": "reported p-value disagrees with the statistic AND flips the .05 significance decision",
  "discrepancy": "reported p=0.04 but t statistic 2 (df=20) admits p in [0.053705,0.065334] — crosses .05",
  "reported": 0.04,
  "computed": 0.059266,
  "evidence": {
    "recompute_script": "# LITMUS recompute script for statcheck.v1 ...\nimport math\n... (pure-stdlib t/F/chi2/z CDFs) ...\nif flag:\n    print('INCONSISTENT test=t stat=2 df=20 reported_p=0.04 achievable_p=[0.053705,0.065334]')\nelse:\n    print('OK')\n",
    "expected_output": "INCONSISTENT test=t stat=2 df=20 reported_p=0.04 achievable_p=[0.053705,0.065334]"
  },
  "schema_valid": true
}
```

Save `evidence.recompute_script` to a file and run it: it prints exactly `expected_output`. That is
the verdict, reproduced — no LITMUS, no model, just stdlib Python.

A *correctly* reported statistic passes. `check_statistic(test="t", statistic=2.5, df=18,
reported_p=0.02)` returns `{"status": "pass", "is_flag": false}` — `t = 2.5, df = 18` is `p ≈ 0.0223`,
and `0.02` is a correct rounding (statcheck is rounding-aware; it prefers PASS over a trivial flag).

### 2. `check_yield` catches a physically impossible yield

```json
{ "reported_yield_pct": 142 }
```

```json
{
  "verifier_id": "yield_check.v1",
  "status": "fail",
  "severity": "A",
  "trust_tier": "deterministic_confirmed",
  "message": "yield exceeds 100% stoichiometric maximum — impossible",
  "discrepancy": "reported 142% yield exceeds the 100% stoichiometric maximum",
  "reported": 142,
  "computed": 100.0,
  "evidence": {
    "recompute_script": "# LITMUS recompute script for yield_check.v1 ...\nif REPORTED_YIELD_PCT > MAX_YIELD + TOLERANCE:\n    print('IMPOSSIBLE_YIELD reported=142 max=100')\n...",
    "expected_output": "IMPOSSIBLE_YIELD reported=142 max=100"
  },
  "schema_valid": true
}
```

Severity A: a hard physical bound, not an argument. Pass `mol_product` and `mol_limiting_reagent`
too and it additionally recomputes the theoretical yield and flags a molar mismatch (severity B).

### 3. `audit_claim_graph` audits a whole paper — flags with run-it-yourself scripts

Feed a ClaimGraph (from `extract_claims`, or hand-built). The pipeline runs every matched verifier,
then **re-runs each flag's `recompute_script` in a fresh sandbox and drops any that don't reproduce**
(DESIGN §13.4) — the `dropped_flags` log is the system catching its own false positives.

**Call**

```json
{
  "claim_graph": {
    "schema_version": "1.0",
    "paper_id": "demo",
    "claims": [
      { "id": "c1", "text": "The components sum to 65.", "epistemic_tier": "T0",
        "predicate": "sum(parts) == reported_total", "evidence_refs": ["e1"],
        "location": { "section": "Table 1", "quote": "Total: 65" } }
    ],
    "evidence": [
      { "id": "e1", "kind": "table",
        "extracted_values": { "parts": [10, 20, 30], "reported_total": 65 },
        "location": { "section": "Table 1", "quote": "10, 20, 30; total 65" } }
    ],
    "bindings": [ { "claim_id": "c1", "evidence_id": "e1", "relation": "rests_on" } ]
  }
}
```

**Response** (abridged)

```json
{
  "paper_id": "demo",
  "summary": {
    "n_findings": 1, "n_flags": 1, "n_dropped": 0,
    "n_routed_to_human": 0, "n_abstained": 0,
    "flags_by_trust_tier": { "deterministic_confirmed": 1 },
    "flags_by_severity": { "B": 1 }
  },
  "findings": [
    {
      "verifier_id": "sum_check.v1",
      "status": "fail",
      "severity": "B",
      "trust_tier": "deterministic_confirmed",
      "discrepancy": "reported 65 but parts sum to 60",
      "evidence": {
        "recompute_script": "# LITMUS recompute script for sum_check.v1 ...\nif abs(computed - REPORTED_TOTAL) <= TOLERANCE:\n    print('OK')\nelse:\n    print('MISMATCH reported=65 computed=60')\n",
        "expected_output": "MISMATCH reported=65 computed=60"
      }
    }
  ],
  "dropped_flags": [],
  "routed_to_human": [],
  "abstained": [],
  "schema_valid": true
}
```

`n_dropped: 0` means the flag survived fresh-context confirmation — its script reproduced in a clean
sandbox, so it's `deterministic_confirmed`. `audit_pdf(pdf_path)` does the same end-to-end starting
from a PDF (extraction via Opus, then this pipeline).

### 4. `calibration_scorecard` — how much to trust each tool

Before relying on a verdict, ask how the tool calibrates (DESIGN §7). Only a `scoring` verifier may
render an A/B verdict; an `advisory` one surfaces leads but not graded verdicts.

```json
{
  "count": 9,
  "n_scoring": 9,
  "scorecards": [
    {
      "verifier_id": "sum_check.v1",
      "epistemic_tier": "T0",
      "recall": 1.0,
      "fpr_overall": 0.0,
      "deterministic": true,
      "reproducibility": 1.0,
      "admission": "scoring",
      "reasons": ["G1 ∧ G2 ∧ G3 ∧ G4 ∧ G6 all pass -> SCORING"]
    }
  ]
}
```

`recall` is measured on the verifier's own planted errors; `fpr_overall` on its clean inputs;
`reproducibility` is the fraction of emitted flags whose scripts reproduce in the sandbox. All with
zero human labels.

---

## Notes

- **Routing.** Match a claim's type against each verifier's `consumes` (from `list_verifiers`). The
  current library spans T0/T1 numeric and consistency checks (statcheck, GRIM, percent-change,
  sum/table totals, reaction yield, mass balance, unit consistency, prose-vs-table,
  figure-vs-table). The library is self-extending (DESIGN §6.2, §8) — `list_verifiers` is always the
  live source.
- **Evidence shape.** Each verifier reads a specific `extracted_values` payload (e.g. sum_check:
  `{"parts": [...], "reported_total": n}`); the per-verifier `consumes` and `description` document
  it. The `check_*` convenience tools spare you that for the headline checks. Key matching is lenient
  where it can be (DESIGN §11), so ordinary extraction variance still binds.
- **Determinism.** Every verdict and every emitted script is deterministic — no RNG, clock, network,
  or LLM in the judging path (DESIGN §3.1, §7 G4). That's what makes the scripts reproducible for
  your agent.
- **The sandbox.** Recompute scripts are stdlib-only (or a declared, pinned dependency set) so they
  run in a clean, network-less environment. LITMUS confirms its own flags this way before reporting
  them; your agent can do the same with the `recompute_script` it receives.
