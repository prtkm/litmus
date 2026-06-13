# LITMUS — Design Brief

*A self-extending, multi-domain instrument for auditing the published scientific literature with executable evidence. This is the controlling design document. It is written to be read by build agents **and** humans: every concept has an explicit contract, and the actionable build plan is the last section (§19) so a fresh agent can scroll to it.*

**Owner:** Prateek · **Repo:** `~/Git/litmus`

---

## 1. Thesis (north star)

**LITMUS audits any published paper the way a rigorous human expert would — by decomposing it into checkable assertions and re-deriving each verdict from first principles — and it proves every objection with a script you can run yourself.**

At its core, LITMUS is **a reasoning loop over a self-extending verifier library**:

> A reasoner (Opus 4.8, full paper + figures + tables + SI) decomposes a paper into a graph of claims and the evidence each rests on. For every checkable assertion it **routes to a verifier** — one that already exists (first-party or **community-contributed**), or one it **synthesizes on the fly** — and renders the verdict in deterministic code that ships runnable evidence. Verifiers that earn their place are **promoted into the library**, so the system gets more capable with every paper and every contributor.

Three things make this not "an LLM guessing at errors":
1. **The model never renders a verdict.** It extracts, locates, and reasons about *what to check*; deterministic code decides *whether it holds*.
2. **Every verifier — human-written or AI-synthesized — passes the same calibration gate** before it is trusted (catch a planted version of its own error, pass clean inputs, run deterministically, reproduce in a fresh sandbox).
3. **The library is a commons.** Domain experts in any field add verifiers through one contract; the calibration gate keeps quality uniform regardless of who (or what) wrote the check.

**Who it's for:** researchers deciding whether to trust, cite, or build on a result; meta-scientists; journals and reviewers; reproducibility teams. **Differentiator** (vs. statcheck = one check; Black Spatula / YesNoError = LLM judgments at ~10% FPR; SciScore = compliance markers): **composable + multi-domain + deterministic-recompute + autonomous re-run confirmation + community-extensible + self-synthesizing.**

---

## 2. Product shape (what the app is)

A live web app, not a static report:

1. **Upload a paper** → it triggers a **Claude multi-agent audit pipeline** (§13) that runs asynchronously (extraction → planning → parallel verification → fresh-context confirmation → synthesis-for-gaps → assembly). The UI shows live status (`queued → extracting → auditing → confirming → done`).
2. **Browse the history** → every paper LITMUS has already audited is in a gallery; click one to see its audit report — the **CHECKABLE** flags (each with a ▶ run-it-yourself recompute script) and the **ROUTE-TO-HUMAN** dimensions (surfaced, not scored), plus the **dropped-flag** log (self-caught false positives).
3. Results are **persisted and cached** (keyed by content hash / DOI): the first upload pays the audit cost; every later view is instant.

The same pipeline that powers the app also runs **locally as a CLI** for development, the calibration gate, and the discovery study (§17) — one pipeline, two execution surfaces, separated by an `ExecutorAdapter` (§15).

---

## 3. First principles (non-negotiable invariants)

Treat a violation as a build failure.

1. **Extract/locate/reason with the LLM; judge with code.** If you ever ask the model "is this wrong?", stop — extract the inputs and let deterministic code decide.
2. **No flag without executable evidence.** Every `fail` ships a `recompute_script` + `expected_output`. No script, no flag.
3. **One calibration gate for all verifiers.** Prebuilt, contributed, and synthesized verifiers are admitted by the *same* seeded-error harness (recall on planted errors, FPR on clean inputs, determinism, fresh-context reproducibility). Over its declared FPR ceiling → **demoted to advisory**, never an A/B verdict.
4. **Abstain > guess.** When extraction or binding is ambiguous, return `inconclusive`. A verifier never silently absolves.
5. **Route the subjective to humans; never score it.** Significance, novelty-worth, taste, "does the question matter" — surfaced, explicitly not scored. Refusing to fake the unverifiable is a feature.
6. **Trust tiers never collapse.** Every finding carries its tier: `deterministic_confirmed` > `calibrated_synthesized` > `advisory_assisted` > `routed_to_human`. The UI and API never blur them.
7. **Attribution is explicit.** Every verifier declares what it computes itself vs. which library it calls. Reimplement, don't wrap-and-claim.
8. **Reproducible by construction.** Recompute scripts depend only on the stdlib or a declared, pinned dependency set, so a fresh agent — or a skeptical reader — can rerun any verdict in a clean environment.

---

## 4. What "fully auditing a paper" means

A paper is a chain: **claim ← evidence (figures / tables / data) ← method ← assumptions ← provenance.** A full audit walks every link. "Is the claim supported by the data?" decomposes into five questions, each needing a different verifier and giving a different guarantee:

1. **Operationalizable?** Precise enough to check? (If not → *vague/unfalsifiable*, itself a finding.)
2. **Bound to evidence?** Does the paper point to data for it? (If not → *orphan claim*.)
3. **Numerically faithful?** Does the bound evidence, recomputed, match the claim's number?
4. **Strength/scope-faithful?** Does the evidence's strength and scope match the claim's? (over-claim, extrapolation, causal-on-observational)
5. **Method-valid?** Was the technique that produced the evidence appropriate and correctly applied?

Plus two cross-cutting checks: **external consistency** (physical law, constants, prior literature, cited sources) and **reproducibility** (rerun the provided data/code → do the numbers come back?).

---

## 5. The claim taxonomy — verifiability tiers

Every extracted assertion is tagged with an **epistemic tier**: what it takes to render a verdict, and how reproducible that verdict is. This drives routing (§12) and trust (§14).

| Tier | Class | What it checks | Verdict basis | Reproducibility |
|---|---|---|---|---|
| **T0** | Internal arithmetic | yields, elemental analysis, p-from-statistic, GRIM/GRIMMER, equation recompute, table totals, %/delta claims, df coherence, unit balance | a theorem on the paper's own numbers | exact |
| **T1** | Fixed external knowledge | physical limits, CODATA constants, atom/charge balance, citation exists/retracted, known-value lookups | theorem + trusted reference table/DB | exact (DB-versioned) |
| **T2** | Internal cross-consistency | prose vs the paper's own table/figure ("+40%" vs 50→68=36%; "the majority" vs n=180/400; "significant" but CI crosses null; abstract vs body) | LLM binds claim↔number; code judges | exact once bound |
| **T3** | Method appropriateness | right test for the design? multiple-comparison correction? power? assumptions (normality, independence, stationarity)? | reasoning + deterministic signatures | partial |
| **T4** | Claim↔evidence support | over-generalization, extrapolation beyond data range, "no effect" from underpowered null, causal language on observational design | calibrated judgment with deterministic anchors | calibrated |
| **T5** | External / literature | contradicts established results unaddressed, citation distortion (source says the opposite), novelty/prior-art | retrieval + entailment | calibrated (higher FP) |
| **T6** | Reproducibility | rerun provided data/code → numbers reproduce? raw data ↔ summary stats consistent? | deterministic **if** data/code provided | exact when runnable |
| **T7** | Integrity signals | image duplication/manipulation, Benford/terminal-digit, too-perfect agreement, tortured phrases, hidden text, p-curve | screening signal → human | signal, not verdict |
| **T8** | Irreducibly subjective | significance, importance, novelty-worth, taste | route to human | not scored |

**The frontier is figures + T2.** A vision reasoner over figures can read plotted values and check them against the table; verify error bars against the reported N/SD; detect axis truncation/dual-axis distortion; check a trend claim against the plotted data; verify a fit line matches the claimed R²/slope; detect spliced/duplicated gel/blot/micrograph panels. **Most of these collapse to "read numbers off the figure → T0/T2 deterministic check"** — an early priority.

---

## 6. The verifier model

### 6.1 Kinds of verifier (by who produces it)

| Kind | Origin | Trust | Example |
|---|---|---|---|
| **A. Prebuilt** | first-party, hardened | versioned, unit-tested, calibrated FPR | statcheck, yield, thermo bound |
| **B. Templated** | a trusted skeleton the LLM instantiates with paper-specific *content* | fixed judging code; LLM fills slots | "dimensional consistency" for *this* equation; "claim↔number" for *this* bound pair |
| **C. Synthesized** | the agent writes new logic on the fly for a one-off metric/method | earns trust via the calibration gate (§7); tagged `synthesized` | a paper's bespoke "figure of merit" recomputation |
| **D. Assisted** | needs an LLM judgment anchored deterministically | advisory unless it reduces to a deterministic core; abstains liberally | design-tag × causal-language lexicon (spin) |

**Key realization:** most "bespoke" checks are not bespoke *logic* — they are a known *form* (kind B) with paper-specific *content*. A small set of powerful templates covers a large fraction of T0–T2.

### 6.2 The promotion path (the library grows itself)

```
C (synthesized one-off)  ──recurs──▶  B (generalized template)  ──stabilizes──▶  A (hardened prebuilt)
```

The discovery study (§17) tells us empirically which one-offs recur → those become the next prebuilt batch. The library converges toward the field's real distribution of checkable error.

### 6.3 The verifier contract (the universal interface)

Every verifier — regardless of kind or author — is a self-describing package:

```
VerifierManifest:
  id, version, authors, license, provenance         # who/what wrote it
  consumes:        [claim_type | record_type]        # routing keys
  epistemic_tier:  T0..T8                             # §5 — drives trust + UI
  determinism:     deterministic | lookup | assisted | synthesized
  capability_tags: [domain, technique, entities...]  # discovery + graph routing
  fpr_ceiling:     float                              # calibration ceiling
  built_vs_borrowed: { ours: [...], libs: [...] }     # attribution (P7)
  dependencies:    [pinned, declared]                 # reproducibility (P8)

  judge(claim, evidence) -> Finding                   # PURE; the verdict
  self_test() -> [ (clean_instance, expected_pass),   # REQUIRED: the admission fuel
                   (planted_instance, expected_fail) ] # generates its own seeded errors
```

The `self_test` generator is the crux: **a verifier ships the means to calibrate itself.** No self-test → advisory only. `judge` returns a `Finding`: `{status, severity (A/B/C), discrepancy, evidence{quote, location, recompute_script, expected_output}, trust_tier, verifier_kind}`. Adapters auto-expose every manifest as a CLI command (`litmus run <id>`) and an MCP tool, from one definition.

---

## 7. The calibration kernel — the immune system

The single most important subsystem, and why the self-extending approach is safe. **It is the admission gate for every verifier, human-contributed or AI-synthesized alike.** Given a verifier + its `self_test`, it measures, with **zero human labels**:

- **G1 Recall** ≥ 0.90 on the verifier's planted errors.
- **G2 FPR** ≤ its declared ceiling on clean instances.
- **G3 Reproducibility** = 100% of emitted flags' scripts reproduce `expected_output` in a fresh, **network-less, resource-limited sandbox**.
- **G4 Determinism** — identical output across N runs (no RNG/clock/network/LLM-call inside).
- **G6 Ceiling** — measured FPR ≤ declared ceiling, **per claim_type**. Over ceiling → demoted to `advisory`.

Admitted as **scoring iff G1∧G2∧G3∧G4∧G6**, else `advisory` or rejected. **The same gate runs whether a verifier came from a chemist's pull request or the on-the-fly synthesizer** — quality is uniform by construction. The system-level harness (`verify.py`) aggregates these into one scorecard (the project's machine-checkable "done").

---

## 8. On-the-fly verifier synthesis

When the planner finds a checkable claim with no matching A/B verifier:

```
claim (no match)
  ──▶ reasoner proposes {strategy, judge_code, self_test, manifest}   # "what technique is right here?"
  ──▶ sandbox + determinism check (G4)
  ──▶ calibration kernel (G1,G2,G3,G6) on the proposed self_test
  ──▶ admit: scoring (if calibrated) | advisory (if not) | reject (non-deterministic / no self_test)
  ──▶ fresh-context confirm on the actual claim
  ──▶ emit Finding (tagged `synthesized`, full provenance)
  ──▶ enqueue for promotion review (C→B) if it recurs
```

The trust comes from the **gate**, not the LLM. A synthesized verifier that can't produce a passing self-test, or whose output varies, never reaches a verdict — it is downgraded to `inconclusive`.

---

## 9. The verifier commons — cross-field extensibility

**A chemist, economist, biologist, or astronomer adds a verifier for their field through one contract, held to the same bar as everything else.** The library becomes a commons — "statcheck for every field, contributed by every field."

- **Scaffold:** `litmus verifier new <id>` generates a manifest + `judge` + `self_test` + docs stub.
- **Local validation:** `litmus verifier test <id>` runs the calibration kernel (§7) locally and prints the verifier's own scorecard, so a contributor sees scoring-vs-advisory before submitting.
- **Submission:** PR to the first-party repo **or** an independent package discovered via entry-points (so contributors can ship out-of-tree, like a pytest plugin). **We ship in-tree first, with the entry-point mechanism designed in**, so out-of-tree works later without redesign.
- **Admission (CI):** runs the kernel; merges only on determinism + reproducibility + a defensible FPR ceiling; assigns a trust badge.
- **Trust & safety:** all contributed `judge`/`self_test` code runs in the **network-less, resource-limited sandbox** (same as synthesized verifiers) — no network, no filesystem outside scratch, bounded compute. Verifiers are signed + provenance-tagged; one that later regresses is auto-demoted.
- **Discovery:** verifiers indexed by `capability_tags` + the cross-paper graph (§10) — "which verifiers apply to claims about entity/technique X."

---

## 10. Storage — claims, audits, and the knowledge graph (decided)

**Shape is a graph; storage is files/rows — the per-paper graph is small, so it never needs a graph database.**

- **Per-paper claim graph → one JSON document, schema-validated** (`schemas/claim.schema.json`). It is the LLM's structured output (the extraction) and the source of truth for *what is in the paper*. JSON is canonical because the LLM emits it natively, it validates against a schema, and every consumer (planner, verifiers, kernel, web, API) reads it. **YAML/Markdown are views, never the store** — a YAML projection for a human hand-authoring/correcting ground truth, Markdown for rendering.
- **Per-paper audit → a second JSON document** (`schemas/audit.schema.json`), the verdicts. It is **derived** by running the verifiers over the claim graph, so re-running the library regenerates it without re-extracting.
- **Two homes for the same shapes:**
  - The **discovery-study corpus** lives as **files in the repo** (`study/corpus/claims/*.json`, `study/corpus/audits/*.json`) — curated, versioned, diff-able.
  - The **live app's user uploads** persist in **Supabase** — `papers` rows carry `claim_graph jsonb` + `audit_report jsonb` + status; PDFs in Supabase Storage. Same JSON schemas.
- **The "world model" (Kosmos-style cross-paper graph) is a *derived index*, not a source of truth.** It is built by ingesting the per-paper JSON, rebuildable from them, and enables the cross-paper use-cases (retraction blast-radius; cross-paper contradiction on the same quantity; verifier-routing by entity/domain; dependency propagation; field rigor map). Start it **in-memory / embedded** (networkx or DuckDB) over the corpus; build it **only when those use-cases are in scope**. No graph server, no Supabase dependency for it. (Per-paper provenance — claims→evidence→verifier→verdict — is just the audit JSON rendered as a graph in the UI.)

---

## 11. Extraction & the Claim IR

The LLM front-end turns a PDF into structured, checkable assertions — **the only model-in-the-loop step for *finding* things; judging is always code.**

- **Input:** full paper — body text, tables, **figures (vision)**, equations, SI. Opus 4.8 (1M context + vision).
- **Output:** a `ClaimGraph`:
  ```
  Claim:   { id, text, location{section,page,char_span,quote}, epistemic_tier (proposed),
             predicate (operationalized), strength, scope, evidence_refs[], confidence }
  Evidence:{ id, kind: table|figure|dataset|equation|statistic|number,
             location, extracted_values{...}, confidence }
  Binding: claim ──rests_on──▶ evidence
  ```
- **Rules:** the extractor only *transcribes and locates* — never judges correctness, records `null` rather than inventing a missing value. Low-confidence extractions are flagged so the planner can downgrade or abstain.

---

## 12. The planner

Given the `ClaimGraph`, for each claim: (1) read the proposed tier; (2) **retrieve** candidate verifiers from the commons by `consumes` + `capability_tags` (+ graph routing); (3) if matched → run (reconcile if several); (4) if none and checkable → **synthesize** (§8); (5) if T8 → route to human, don't score; (6) if ambiguous/low-confidence → abstain. The planner is **deterministic control flow over an LLM-proposed plan** — dynamic only at extraction + synthesis, which keeps each run reproducible and gradeable.

---

## 13. The audit pipeline (multi-agent)

One upload → this pipeline, run as a multi-agent workflow:

```
1. Extraction agent (Opus vision)  → ClaimGraph                          (§11)
2. Planner                         → per-claim route/synthesize/abstain  (§12)
3. Parallel verifier subagents     → run matched verifiers (deterministic judging)
   + synthesis subagents for gaps  → propose+calibrate a one-off verifier (§7,§8)
4. Fresh-context confirm subagents → re-run each flag's recompute_script in a clean
                                     sandbox; DROP any that don't reproduce (logged)
5. Assembler                       → audit report (provenance graph) → persist
```

- **Locally** (dev / study / gate): a Python driver runs this with parallel subprocess/thread workers + bounded API calls. No external services.
- **Hosted** (the live app): the same pipeline runs inside a **Claude managed-agents session** (coordinator + parallel subagents; the session's sandbox is also where recompute scripts + synthesized verifiers execute). The session writes the audit report back to Supabase.
- Both are reached through one `ExecutorAdapter` interface (§15), so the pipeline code is identical; only the *where-it-runs* differs.

---

## 14. Output — the audit report

- **Per-paper report** = the provenance graph rendered two ways:
  - **CHECKABLE:** every confirmed flag — quote, reported-vs-recomputed discrepancy, `recompute_script`, and a ▶ run-it button (in-browser via Pyodide for stdlib/scipy scripts; native for lookup/network scripts).
  - **ROUTE-TO-HUMAN:** subjective dimensions, explicitly not scored; plus the **abstained** list and the **dropped-flag** log (fresh-context self-caught false positives — the autonomy evidence).
- **Every finding carries its trust tier** (P6). The UI never blurs `deterministic_confirmed` and `advisory_assisted`.
- **Machine-readable:** validates against the published JSON schema; the same artifact drives the UI and any downstream API.

---

## 15. Architecture & dependencies (decided)

```
litmus/                         # the framework — plain local Python, no external services
  core/  claim.py finding.py verifier.py calibration.py sandbox.py provenance.py
  extract/                      # PDF (text+figures+tables+SI) -> ClaimGraph (Opus 4.8 vision)
  plan/                         # planner: route | synthesize | abstain | route-to-human
  synth/                        # on-the-fly verifier synthesis loop
  commons/                      # registry + contribution SDK (`litmus verifier new/test`)
  verifiers/                    # first-party verifiers, each a self-describing package
  pipeline/  driver.py executor.py   # the multi-agent pipeline + ExecutorAdapter
  study/                        # the discovery-study harness + corpus (files) + benchmark
  adapters/                     # CLI + MCP auto-exposure from the manifest
  verify.py                     # system-level calibration scorecard (the gate)
  schemas/                      # claim.schema.json, audit.schema.json
app/                            # Next.js (App Router) on Vercel — UI + API routes
  (upload, browse gallery, audit-card page, live status)
```

**External services (and exactly what each is for):**

| Dependency | Used for | Scope |
|---|---|---|
| **Anthropic API** (`claude-opus-4-8`) | the reasoner — extraction, planning, synthesis. **Never** for verdicts. | framework + app |
| **Vercel + Next.js** | the web app: upload UI, browse gallery, audit-card pages, API routes (accept uploads, read results). | app |
| **Supabase** | Postgres (`papers` rows: metadata + `claim_graph jsonb` + `audit_report jsonb` + status), Storage (uploaded PDFs), Realtime (live status to the UI). | app |
| **Claude managed agents** | the **executor** for the upload→audit pipeline (long-running, multi-agent, sandboxed). Reached only through `ExecutorAdapter`. | app only |

**Not used:** no separate worker host (managed agents run the Python pipeline), no graph database/server (the cross-paper index is embedded), no auth for the first cut (public gallery; Supabase Auth can be added later).

**The `ExecutorAdapter` seam (important):** the pipeline (§13) is written once and called through `ExecutorAdapter` with two implementations — `LocalExecutor` (subprocess/thread workers + direct API calls; used by the CLI, the gate, and the discovery study) and `ManagedAgentExecutor` (runs the pipeline inside a managed-agents session; used by the live app). **The framework and the study never depend on managed agents.** Only `ManagedAgentExecutor` does. A build agent working on the core can ignore managed agents entirely.

- **Runtime:** Python 3.12 (framework); Node/Next.js (app). Verifiers are entry-point plugins + MCP tools. Web: static where possible; in-browser Pyodide for portable recompute scripts.
- **Credentials** live in the shell environment (`.zshrc`): `ANTHROPIC_API_KEY`, the Supabase URL + keys, the Vercel token. Never commit them.

---

## 16. Non-goals & guardrails (DQ risks)

- **Not** a dashboard-as-product; the verdicts + scripts are the product. The UI is a thin viewer over the audit JSON.
- **Not** basic RAG; **not** an image-only analyzer; **not** Streamlit.
- **No AI-text detection** (high FPR, biased, evadable — the wrong question).
- **No** medical/clinical advice or any per-person judgment.
- **No** novelty-as-prior-art retrieval as a *scored* verdict (retrieval-heavy, high FP) — advisory only.
- Verdicts on real authors' papers must be **accurate**; never fabricate an error in a real paper to make a demo.

---

## 17. The discovery study — domains & weighting (decided)

We discover the verifier taxonomy empirically, the way statcheck came from studying psychology papers. It runs as **Track 1** of the build (§19) — on the reasoner directly, in parallel with the framework build — and produces the archetype taxonomy + the verifier coverage map + a candidate benchmark.

- **Weighting: 2/3 chemistry, 1/3 cross-domain.** Chemistry is the anchor — the owner can critically evaluate every flag (domain expertise), it's the most novel wedge (physical-consistency audits have no prior art), and it yields the cleanest A-tier flags (impossible yields, EA mismatches). The 1/3 cross-domain slice (psychology stats, economics reproducibility, biology/medicine, a thin physics/ML probe) **demonstrates generalizability** and reveals which verifier *types* recur across fields (the signal that decides prebuild vs. template vs. synthesize).
- **Sources:** open-access with available data/SI where possible — ChemRxiv / Beilstein / RSC-open / ACS-open / PLOS (chem); PsyArXiv (psych); open economics working papers (econ); bioRxiv/medRxiv (bio). Include a few known-flawed papers (retracted / PubPeer'd) as **positive controls** and high-rigor papers as **negative controls**.
- **Caveat (load-bearing):** the per-paper "issues" are *reasoner judgments*, so the taxonomy is high-value but the benchmark is **candidate**, not gold-standard. A label becomes gold-standard only once a **deterministic verifier (from the build) or a human confirms it** — otherwise we'd be using LLM judgments to validate an LLM-extraction system (circular).

---

## 18. Locked decisions

1. **Name & repo:** LITMUS, at `~/Git/litmus`.
2. **Per-paper storage:** one canonical JSON claim-graph + one derived JSON audit, schema-validated. YAML/MD are views. Study corpus = repo files; live uploads = Supabase. (§10)
3. **Cross-paper "world model":** a derived, embedded index over the per-paper JSON, built only when cross-paper use-cases are in scope. No graph server. (§10)
4. **Commons:** in-tree first-party verifiers, designed for out-of-tree plugins later. (§9)
5. **Dependencies:** Anthropic API + Vercel + Supabase + Claude managed agents (executor, app-only). No separate worker host, no graph DB, no auth in the first cut. (§15)
6. **Executor:** one `ExecutorAdapter`; `LocalExecutor` for framework/study/gate, `ManagedAgentExecutor` for the live app. (§13, §15)
7. **Discovery study:** 2/3 chemistry, 1/3 cross-domain, open-access, with positive/negative controls. (§17)

*Still to confirm with the owner before the relevant workstream: (a) reproducibility (T6) depth — how far to invest in actually rerunning provided data/code in the first cut; (b) per-run budget for live Opus spend (extraction/synthesis/study).*

---
---

# 19. BUILD PLAN — two workflows in parallel

> **Primer for a fresh build agent.** LITMUS is a Python framework + a Next.js/Vercel app + a Supabase backend. The framework runs locally (no external services). The app's upload→audit runs inside **Claude Managed Agents** — Anthropic's hosted agent harness (beta header `managed-agents-2026-04-01`): you create an *agent* (model + system prompt + tools), an *environment* (a cloud sandbox), and a *session*, then send events and stream the agent's tool-use/results until idle. It runs long, multi-agent, async work in a sandbox with no infra to host. Docs: https://platform.claude.com/docs/en/managed-agents/overview — **verify exact API field names against the live docs before coding.** You only need it for the `ManagedAgentExecutor` (Track 2, WS-H); everything else is plain local Python. The reasoner model id is `claude-opus-4-8`. Credentials are in the shell env (`.zshrc`); never commit them.

Two workflows run **concurrently from day one** — they have almost no build-time dependency on each other. Discovery runs on the reasoner directly and **reprioritizes** the build rather than blocking it.

## Track 1 — Discovery workflow (the study, §17)

Runs immediately, in parallel, on the reasoner directly (no framework needed). Map → reduce:
1. **Source** 30–50 open-access papers, **2/3 chemistry + 1/3 cross-domain**, with positive/negative controls (§17). Surface the list for owner review before deep auditing.
2. **Per-paper catalog (map):** one agent per paper — decompose into the `ClaimGraph`; per claim record `{tier, what verification is possible, what verifier it would need, does one exist yet, candidate verdict, confidence, quote}`.
3. **Cross-paper cluster (reduce):** synthesize into **issue archetypes**, frequency-ranked per field.
4. **Deliverables:** archetype taxonomy + verifier coverage map + candidate benchmark.
- **GATE:** reproducible; artifacts versioned under `study/`.

## Track 2 — Build workflow (the framework + the app)

**WS-A builds first and sequential** (the kernel is the immune system *and* the score). Once green, WS-B…WS-I fan out:

- **WS-A · Core + calibration kernel** *(first, sequential)* — Claim/Finding/Verifier contracts, schemas, the sandbox, the seeded-error admission kernel, `verify.py`. **GATE:** admits a trivial reference verifier, rejects a non-deterministic one; prints a scorecard; recall/FPR/reproduce/determinism measured with zero human labels.
- **WS-B · Extraction + Claim IR** — PDF (text+figures+tables+SI) → `ClaimGraph` via Opus 4.8 vision. **GATE:** on a held-out paper, schema-valid claims+evidence+bindings with quotes; abstains rather than inventing values.
- **WS-C · Verifier commons + SDK** — registry, `litmus verifier new/test`, entry-point discovery, contributed-code sandboxing. **GATE:** an out-of-author verifier passes the kernel locally and is admitted as scoring.
- **WS-D · First-party verifiers (T0–T1)** — recompute core (stats, chem, physics, units, citation/retraction), each shipping a `self_test`. **GATE:** each meets its FPR ceiling; the suite passes the system scorecard.
- **WS-E · Figure-reading + T2 cross-consistency** *(novelty frontier)* — vision value-extraction from figures; prose-vs-table/figure binding + recompute. **GATE:** reproduces a known prose-vs-table contradiction and a figure-vs-table mismatch, each with a runnable script.
- **WS-F · Synthesis loop** — propose→sandbox→calibrate→admit→confirm→promote. **GATE:** synthesizes a verifier for a bespoke metric, calibrates it, confirms a flag; rejects a non-deterministic candidate.
- **WS-G · Pipeline + provenance graph** — the multi-agent pipeline driver (§13), the `ExecutorAdapter` + `LocalExecutor`, the per-paper provenance graph, and a minimal embedded cross-paper index. **GATE:** `litmus audit <pdf>` end-to-end locally → schema-valid audit report with confirmed, reproducible flags + a dropped-flag log.
- **WS-H · App backend (Supabase + managed-agents executor)** — Supabase schema (`papers` + status + jsonb + Storage), the upload API, `ManagedAgentExecutor` (run the pipeline in a managed-agents session → write the audit report to Supabase), Realtime status. **GATE:** an uploaded PDF runs through a managed-agents session and its audit report lands in Supabase, status transitions live.
- **WS-I · App frontend (Vercel)** — upload UI, the browse gallery (papers already audited), the audit-card page (CHECKABLE w/ ▶run + ROUTE-TO-HUMAN + dropped-flag log), live status. **GATE:** deployed to Vercel; upload→audit→view works end-to-end; a recompute script runs in-browser.

## Convergence

- Track 1's **coverage map reprioritizes Track 2's verifier queue** (WS-D/E build the highest-frequency archetypes first). Track 2 starts on the obvious T0–T1 checks immediately and re-plans as Track 1 lands. Neither blocks the other.
- Track 1's **candidate benchmark**, once its labels are confirmed by Track 2's deterministic verifiers (or a human), becomes the **external gold-standard eval** for the whole system.

## System-level done (the rubric)

The calibration scorecard is green across the admitted library; the discovery benchmark exists and LITMUS's measured recall/FPR *against that external, confirmed benchmark* is reported (not just against its own injectors); an uploaded paper produces a schema-valid audit report (confirmed, reproducible flags + dropped-flag log) viewable in the app; and a contributed verifier and a synthesized verifier each pass the same gate.

---

*End of brief.*
