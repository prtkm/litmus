"""The LITMUS MCP server — audit capabilities as programmatic TOOLS (DESIGN §6.3, §15).

Companion to the ``litmus`` CLI (``litmus/adapters/cli.py``): the *same* framework, exposed over
the Model Context Protocol so any AI science agent — a Claude managed-agents coordinator, a Claude
Desktop session, any MCP client — can call LITMUS's checks as tools instead of shelling out.

The whole point (DESIGN §1, §3.2): **LITMUS never asks an agent to trust an LLM judgment; it ships
runnable proof.** Every tool returns structured JSON — schema-valid ``Finding`` / ``AuditReport``
dicts — and every flag carries its ``recompute_script`` + ``expected_output`` so the *calling* agent
(or a skeptical human) can reproduce the verdict in a clean, network-less sandbox itself. Code
judges, not the agent (DESIGN §3.1).

Tools exposed (FastMCP, stdio):

  * ``list_verifiers()``                  — the catalog an agent routes against.
  * ``run_verifier(verifier_id, claim, evidence)`` — one verifier's deterministic ``judge()``.
  * ``audit_claim_graph(claim_graph)``    — the full pipeline over an extracted ClaimGraph.
  * ``extract_claims(pdf_path)``          — PDF → ClaimGraph via Opus (needs ANTHROPIC_API_KEY).
  * ``audit_pdf(pdf_path)``               — extract + audit end-to-end (needs the key).
  * ``check_statistic`` / ``check_yield`` / ``check_grim`` / ``check_percent_change``
                                          — convenience wrappers that build the right Evidence and
                                            call the REAL verifier, so an agent need not hand-build
                                            a claim/evidence object for the headline checks.
  * ``calibration_scorecard()``          — each verifier's measured recall / FPR / admission, so an
                                            agent knows how much to trust each tool (DESIGN §7).
  * one auto-generated tool *per registered verifier* (``run_<id>``), from its manifest — the
    manifest is the single source for both the CLI command and the MCP tool (DESIGN §6.3).

Run over stdio::

    python -m litmus.adapters.mcp          # or the `litmus-mcp` console script

Connect an agent::

    claude mcp add litmus -- .venv/bin/python -m litmus.adapters.mcp

Resilience (DESIGN §13, §9): a tool never crashes the server. Bad input, a missing key, a broken
verifier — each returns a structured ``{"error": ...}`` result, never an exception out of the
process. The audit pipeline itself already isolates a broken verifier (LocalExecutor); this layer
adds the same discipline at the tool boundary.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from litmus.commons.registry import Registry, build_default_registry
from litmus.core.claim import (
    Claim,
    ClaimGraph,
    Evidence,
    EvidenceKind,
    EpistemicTier,
    Location,
)
from litmus.core.finding import Finding, Status
from litmus.core.schema import validate as schema_validate
from litmus.core.verifier import Verifier
from litmus.pipeline.executor import LocalExecutor

# ---------------------------------------------------------------------------
# Process-local registry + executor. Built once (auto-discovers every first-party verifier and any
# out-of-tree plugin, DESIGN §9). A load failure is recorded on the registry, never raised — the
# server stays up (DESIGN §9, §13).
# ---------------------------------------------------------------------------
_REGISTRY: Optional[Registry] = None


def get_registry() -> Registry:
    """The shared verifier registry (lazy, process-local)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_default_registry()
    return _REGISTRY


# Fresh-context confirmation OFF for single-claim tool calls: an agent calling ``run_verifier`` or a
# ``check_*`` wants the verifier's raw verdict and the script to run ITSELF (DESIGN §3.2), not a
# pre-confirmed one. ``audit_*`` builds its own confirming executor so its dropped-flag log is real
# (DESIGN §13.4).
_NO_CONFIRM = LocalExecutor(confirm=False)


mcp = FastMCP(
    "litmus",
    instructions=(
        "LITMUS audits scientific papers with executable evidence. Every flag ships a "
        "recompute_script + expected_output you can rerun yourself in a clean sandbox — verdicts "
        "are deterministic code, never LLM opinions (DESIGN §3.1, §3.2). Route a claim with "
        "list_verifiers(); judge one claim with run_verifier() or a check_* convenience tool; audit "
        "a whole paper with audit_claim_graph() or audit_pdf(); gauge trust with "
        "calibration_scorecard()."
    ),
)


# ---------------------------------------------------------------------------
# Result helpers — every tool returns a plain JSON-able dict, never raises.
# ---------------------------------------------------------------------------
def _err(message: str, **extra: Any) -> dict[str, Any]:
    """A structured error result (DESIGN §13: a tool never crashes the server)."""
    out: dict[str, Any] = {"error": message}
    out.update(extra)
    return out


def _manifest_summary(v: Verifier) -> dict[str, Any]:
    """The routing-relevant slice of a verifier's manifest (the catalog row, DESIGN §6.3)."""
    m = v.manifest
    return {
        "id": m.id,
        "version": m.version,
        "epistemic_tier": m.epistemic_tier.value,
        "kind": m.kind.value,
        "determinism": m.determinism.value,
        "consumes": list(m.consumes),
        "capability_tags": list(m.capability_tags),
        "fpr_ceiling": m.fpr_ceiling,
        "description": m.description,
    }


def _finding_result(finding: Finding) -> dict[str, Any]:
    """A Finding as a schema-valid dict, annotated so an agent can act on it without re-parsing.

    ``is_flag`` and ``reproducible`` are convenience flags; ``recompute_script`` +
    ``expected_output`` (inside ``evidence``) are the load-bearing payload — the runnable proof
    (DESIGN §3.2).
    """
    d = finding.to_dict()
    d["is_flag"] = finding.status is Status.FAIL
    # Surface the run-it-yourself payload at the top level too, so an agent doesn't have to know the
    # EvidencePacket shape to find it.
    d["recompute_script"] = finding.evidence.recompute_script
    d["expected_output"] = finding.evidence.expected_output
    d["schema_valid"] = not schema_validate(d_for_schema(d), "finding")
    return d


def d_for_schema(finding_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip the convenience keys we add so the dict validates against finding.schema.json."""
    return {k: v for k, v in finding_dict.items() if k in _FINDING_SCHEMA_KEYS}


_FINDING_SCHEMA_KEYS = {
    "verifier_id",
    "claim_id",
    "status",
    "trust_tier",
    "verifier_kind",
    "severity",
    "message",
    "discrepancy",
    "reported",
    "computed",
    "evidence",
    "details",
}


# ---------------------------------------------------------------------------
# Building blocks: turn loose tool arguments into core Claim / Evidence objects.
# ---------------------------------------------------------------------------
def _coerce_claim(claim: Any, *, default_id: str = "claim_1") -> Claim:
    """Accept a claim as a dict (full Claim shape), a bare string (its text), or None.

    A string or None still yields a usable Claim — the verifiers judge against the EVIDENCE; the
    claim mostly carries id + text + tier for the Finding. Lenient on purpose so an agent can call a
    check with minimal ceremony.
    """
    if isinstance(claim, Claim):
        return claim
    if claim is None:
        return Claim(id=default_id, text="", epistemic_tier=EpistemicTier.T0)
    if isinstance(claim, str):
        return Claim(id=default_id, text=claim, epistemic_tier=EpistemicTier.T0)
    if isinstance(claim, dict):
        d = dict(claim)
        d.setdefault("id", default_id)
        d.setdefault("text", "")
        return Claim.from_dict(d)
    raise ValueError(f"claim must be a dict, string, or null (got {type(claim).__name__})")


def _coerce_evidence_list(evidence: Any) -> list[Evidence]:
    """Accept evidence as a list of Evidence dicts, a single Evidence dict, a bare
    ``extracted_values`` dict, or None.

    The most agent-friendly form is just the ``extracted_values`` payload the verifier consumes
    (e.g. ``{"parts": [1,2], "reported_total": 4}``) — we wrap that into a single Evidence record.
    """
    if evidence is None:
        return []
    if isinstance(evidence, Evidence):
        return [evidence]
    if isinstance(evidence, dict):
        # Is this a full Evidence record (has id+kind), or just an extracted_values payload?
        if "id" in evidence and "kind" in evidence:
            return [Evidence.from_dict(evidence)]
        if "extracted_values" in evidence:
            return [_evidence_from_values(evidence.get("extracted_values") or {})]
        return [_evidence_from_values(evidence)]
    if isinstance(evidence, list):
        out: list[Evidence] = []
        for i, e in enumerate(evidence):
            if isinstance(e, Evidence):
                out.append(e)
            elif isinstance(e, dict) and "id" in e and "kind" in e:
                out.append(Evidence.from_dict(e))
            elif isinstance(e, dict) and "extracted_values" in e:
                out.append(_evidence_from_values(e.get("extracted_values") or {}, eid=f"ev_{i+1}"))
            elif isinstance(e, dict):
                out.append(_evidence_from_values(e, eid=f"ev_{i+1}"))
            else:
                raise ValueError(f"evidence[{i}] must be an object")
        return out
    raise ValueError(f"evidence must be a list or object (got {type(evidence).__name__})")


def _evidence_from_values(
    values: dict[str, Any], *, eid: str = "ev_1", kind: EvidenceKind = EvidenceKind.NUMBER
) -> Evidence:
    """Wrap a bare ``extracted_values`` dict into a single Evidence record the verifiers can read."""
    if not isinstance(values, dict):
        raise ValueError("extracted_values must be an object")
    return Evidence(id=eid, kind=kind, location=Location(section="mcp"), extracted_values=values)


def _judge(verifier_id: str, claim: Any, evidence: Any) -> dict[str, Any]:
    """Shared core for ``run_verifier`` and every auto-generated per-verifier tool.

    Resolves the verifier, coerces the loose arguments, runs the verifier's pure ``judge()``
    (DESIGN §3.1), and returns the Finding as an annotated, schema-valid dict. Any failure is a
    structured error, never an exception (DESIGN §13).
    """
    registry = get_registry()
    if verifier_id not in registry:
        return _err(
            f"unknown verifier_id {verifier_id!r}",
            known_verifier_ids=registry.ids(),
        )
    try:
        c = _coerce_claim(claim, default_id=f"claim_for_{verifier_id}")
        ev = _coerce_evidence_list(evidence)
    except ValueError as exc:
        return _err(f"could not parse claim/evidence: {exc}")
    verifier = registry.get(verifier_id)
    try:
        finding = verifier.judge(c, ev)
    except Exception as exc:  # a broken verifier never crashes the server (DESIGN §13)
        return _err(f"verifier {verifier_id!r} raised during judge(): {type(exc).__name__}: {exc}")
    return _finding_result(finding)


def _audit(graph: ClaimGraph) -> dict[str, Any]:
    """Run the full pipeline over a ClaimGraph and return a schema-valid AuditReport dict.

    Uses a CONFIRMING executor (DESIGN §13.4): every flag's recompute_script is re-run in a fresh,
    network-less sandbox and dropped if it doesn't reproduce — the dropped-flag log is the system
    catching its own false positives.
    """
    executor = LocalExecutor(confirm=True)
    report = executor.audit_graph(graph, get_registry())
    d = report.to_dict()
    d["schema_valid"] = not schema_validate(d, "audit")
    return d


# ===========================================================================
# Tools.
# ===========================================================================
@mcp.tool(
    name="list_verifiers",
    description=(
        "List every registered LITMUS verifier — the catalog an agent routes a claim against. "
        "Each entry: {id, epistemic_tier (T0..T8), kind, determinism, consumes (claim_type routing "
        "keys), capability_tags, fpr_ceiling, description}. Match a claim's type against `consumes` "
        "to pick a verifier, then call run_verifier or the matching run_<id>/check_* tool."
    ),
)
def list_verifiers() -> dict[str, Any]:
    """Return the verifier catalog (DESIGN §6.3, §9, §12)."""
    registry = get_registry()
    return {
        "count": len(registry),
        "verifiers": [_manifest_summary(v) for v in registry.all()],
        "load_errors": registry.load_errors,
    }


@mcp.tool(
    name="run_verifier",
    description=(
        "Run ONE verifier's deterministic judge() on a claim + its evidence and return the Finding "
        "(code judges, not the agent — DESIGN §3.1). `evidence` is the verifier's extracted_values "
        "payload (e.g. {'parts': [10,20,30], 'reported_total': 65} for sum_check.v1), a full "
        "Evidence object, or a list of either; `claim` may be a string, a Claim object, or null. A "
        "FAIL ships evidence.recompute_script + expected_output so you can reproduce the verdict "
        "yourself (DESIGN §3.2). See list_verifiers() for ids and the extracted_values each consumes."
    ),
)
def run_verifier(
    verifier_id: str, claim: Any = None, evidence: Any = None
) -> dict[str, Any]:
    """Judge one claim with one verifier (DESIGN §3.1)."""
    return _judge(verifier_id, claim, evidence)


@mcp.tool(
    name="audit_claim_graph",
    description=(
        "Run the FULL LITMUS pipeline over an already-extracted ClaimGraph: plan every claim, run "
        "all matched verifiers, then re-run each flag's recompute_script in a fresh network-less "
        "sandbox and DROP any that don't reproduce (DESIGN §13). Returns a schema-valid AuditReport "
        "{summary, findings (confirmed flags + passes, each with its recompute_script), "
        "dropped_flags, routed_to_human, abstained}. `claim_graph` is the ClaimGraph dict "
        "(paper_id + claims + evidence + bindings) — e.g. from extract_claims()."
    ),
)
def audit_claim_graph(claim_graph: dict[str, Any]) -> dict[str, Any]:
    """Audit a ClaimGraph end-to-end (DESIGN §13, §14)."""
    if not isinstance(claim_graph, dict):
        return _err("claim_graph must be an object (a ClaimGraph dict)")
    errs = schema_validate(claim_graph, "claim")
    if errs:
        return _err(
            "claim_graph failed schema validation (claim.schema.json)",
            validation_errors=errs[:20],
        )
    try:
        graph = ClaimGraph.from_dict(claim_graph)
    except Exception as exc:
        return _err(f"could not parse claim_graph: {type(exc).__name__}: {exc}")
    try:
        return _audit(graph)
    except Exception as exc:  # the pipeline already isolates broken verifiers; belt-and-braces
        return _err(f"audit failed: {type(exc).__name__}: {exc}")


@mcp.tool(
    name="extract_claims",
    description=(
        "Extract a schema-valid ClaimGraph from a PDF via Opus native PDF ingestion (DESIGN §11). "
        "The ONLY model-in-the-loop step — it transcribes and locates checkable claims + their "
        "evidence; it never judges. Returns the ClaimGraph dict (feed it to audit_claim_graph). "
        "Requires ANTHROPIC_API_KEY in the server environment. `pdf_path` is an absolute path to "
        "the PDF on the server host."
    ),
)
def extract_claims(pdf_path: str, paper_id: Optional[str] = None) -> dict[str, Any]:
    """PDF -> ClaimGraph via Opus (DESIGN §11). Needs ANTHROPIC_API_KEY."""
    try:
        from litmus.extract.extractor import extract_claim_graph
    except Exception as exc:
        return _err(f"extractor unavailable: {type(exc).__name__}: {exc}")
    try:
        graph = extract_claim_graph(pdf_path, paper_id=paper_id)
    except FileNotFoundError as exc:
        return _err(str(exc))
    except Exception as exc:  # ExtractionError, missing key, API failure — all structured
        return _err(f"extraction failed: {type(exc).__name__}: {exc}")
    d = graph.to_dict()
    d["schema_valid"] = not schema_validate(d, "claim")
    return d


@mcp.tool(
    name="audit_pdf",
    description=(
        "Extract a PDF (Opus, DESIGN §11) AND audit it end-to-end — extract_claims then "
        "audit_claim_graph in one call. Returns a schema-valid AuditReport whose flags each ship a "
        "recompute_script you (or a human) can rerun (DESIGN §3.2). Requires ANTHROPIC_API_KEY. "
        "`pdf_path` is an absolute path on the server host. The only model-in-the-loop step is "
        "extraction; every verdict is deterministic code (DESIGN §3.1)."
    ),
)
def audit_pdf(pdf_path: str, paper_id: Optional[str] = None) -> dict[str, Any]:
    """Extract + audit a PDF (DESIGN §13). Needs ANTHROPIC_API_KEY."""
    try:
        from litmus.extract.extractor import extract_claim_graph
    except Exception as exc:
        return _err(f"extractor unavailable: {type(exc).__name__}: {exc}")
    try:
        graph = extract_claim_graph(pdf_path, paper_id=paper_id)
    except FileNotFoundError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"extraction failed: {type(exc).__name__}: {exc}")
    try:
        return _audit(graph)
    except Exception as exc:
        return _err(f"audit failed: {type(exc).__name__}: {exc}")


# --- convenience checks: build the right Evidence, call the REAL verifier --------------------------
@mcp.tool(
    name="check_statistic",
    description=(
        "statcheck (statcheck.v1, T0): does a reported two-tailed p-value match the p recomputed "
        "from its test statistic? Pass test ('t'|'F'|'r'|'chi2'|'z'), the statistic, the degrees of "
        "freedom (df for t/r/chi2; df1 AND df2 for F; none for z), and reported_p. Returns the "
        "verifier's Finding — FAIL (with a runnable recompute_script) when the reported p can't be "
        "produced by any rounding of the statistic AND it matters; PASS otherwise. e.g. "
        "test='t', statistic=2.0, df=20, reported_p=0.04 flags (t=2.0,df=20 gives p~=0.059, ns)."
    ),
)
def check_statistic(
    test: str,
    statistic: float,
    reported_p: float,
    df: Optional[float] = None,
    df1: Optional[float] = None,
    df2: Optional[float] = None,
    decimals: Optional[int] = None,
) -> dict[str, Any]:
    """Build the statcheck Evidence and call statcheck.v1 (DESIGN §5 T0)."""
    values: dict[str, Any] = {"test": test, "statistic": statistic, "reported_p": reported_p}
    if df is not None:
        values["df"] = df
    if df1 is not None:
        values["df1"] = df1
    if df2 is not None:
        values["df2"] = df2
    if decimals is not None:
        values["decimals"] = decimals
    claim = f"{test} statistic {statistic} reported with p = {reported_p}"
    return _judge("statcheck.v1", claim, values)


@mcp.tool(
    name="check_yield",
    description=(
        "yield_check (yield_check.v1, T1): is a reported reaction yield physically possible "
        "(0 <= y <= 100) and consistent with its molar quantities? Pass reported_yield_pct, and "
        "optionally mol_product + mol_limiting_reagent to also recompute the theoretical yield. "
        "Returns the Finding — FAIL severity A for an impossible (>100% or <0%) yield, severity B "
        "for a molar mismatch, each with a runnable recompute_script (DESIGN §3.2). e.g. "
        "reported_yield_pct=142 flags as impossible."
    ),
)
def check_yield(
    reported_yield_pct: float,
    mol_product: Optional[float] = None,
    mol_limiting_reagent: Optional[float] = None,
) -> dict[str, Any]:
    """Build the yield_check Evidence and call yield_check.v1 (DESIGN §6.1 T1)."""
    values: dict[str, Any] = {"reported_yield_pct": reported_yield_pct}
    if mol_product is not None:
        values["mol_product"] = mol_product
    if mol_limiting_reagent is not None:
        values["mol_limiting_reagent"] = mol_limiting_reagent
    claim = f"The reaction proceeded in {reported_yield_pct}% yield."
    return _judge("yield_check.v1", claim, values)


@mcp.tool(
    name="check_grim",
    description=(
        "GRIM (grim.v1, T0): is a reported mean of integer-scored responses arithmetically "
        "achievable? With n participants each giving an integer response (over n_items items, "
        "default 1), the mean must be a multiple of 1/(n*n_items). Pass reported_mean, n, and "
        "optionally n_items. Returns the Finding — FAIL (severity B, with a runnable "
        "recompute_script) when no integer total reproduces the mean at its printed precision. e.g. "
        "reported_mean=3.45, n=10 is impossible (10*3.45=34.5 is not an integer)."
    ),
)
def check_grim(
    reported_mean: float, n: int, n_items: int = 1
) -> dict[str, Any]:
    """Build the GRIM Evidence and call grim.v1 (DESIGN §5 T0)."""
    values: dict[str, Any] = {"reported_mean": reported_mean, "n": n, "n_items": n_items}
    claim = f"mean = {reported_mean} (n = {n})"
    return _judge("grim.v1", claim, values)


@mcp.tool(
    name="check_percent_change",
    description=(
        "percent-change check (percent_change.v1, T0): does a reported percent change match "
        "old -> new? Pass old_value, new_value, and reported_pct_change (signed: +N increase, -N "
        "decrease). Recomputes 100*(new-old)/old, rounding-aware. Returns the Finding — FAIL "
        "(severity B, with a runnable recompute_script) on a genuine over/under-claim. e.g. "
        "old_value=50, new_value=68, reported_pct_change=40 flags (true change is +36%)."
    ),
)
def check_percent_change(
    old_value: float, new_value: float, reported_pct_change: float
) -> dict[str, Any]:
    """Build the percent_change Evidence and call percent_change.v1 (DESIGN §5 T0)."""
    values: dict[str, Any] = {
        "old_value": old_value,
        "new_value": new_value,
        "reported_pct_change": reported_pct_change,
    }
    claim = f"a {reported_pct_change}% change from {old_value} to {new_value}"
    return _judge("percent_change.v1", claim, values)


@mcp.tool(
    name="calibration_scorecard",
    description=(
        "Run the LITMUS calibration gate over every registered verifier and return each one's "
        "measured trust (DESIGN §7) — recall on planted errors, false-positive rate on clean inputs "
        "(overall + per claim_type), determinism, fresh-sandbox reproducibility, and the admission "
        "verdict (scoring | advisory | rejected). This is how an agent knows HOW MUCH to trust each "
        "tool: only a SCORING verifier may render an A/B verdict. Computed with zero human labels. "
        "(Runs each verifier's self_test in a sandbox; takes a few seconds.)"
    ),
)
def calibration_scorecard() -> dict[str, Any]:
    """The system calibration scorecard (DESIGN §7) — what verify.py measures, as JSON."""
    try:
        from litmus.core.calibration import calibrate
    except Exception as exc:
        return _err(f"calibration unavailable: {type(exc).__name__}: {exc}")
    registry = get_registry()
    cards: list[dict[str, Any]] = []
    for v in registry.all():
        try:
            cards.append(calibrate(v).to_dict())
        except Exception as exc:  # one verifier's self_test blowing up must not sink the rest
            cards.append(
                {"verifier_id": v.manifest.id, "error": f"{type(exc).__name__}: {exc}"}
            )
    n_scoring = sum(1 for c in cards if c.get("admission") == "scoring")
    return {
        "count": len(cards),
        "n_scoring": n_scoring,
        "scorecards": cards,
    }


# ---------------------------------------------------------------------------
# Auto-generated per-verifier tools (DESIGN §6.3: one manifest -> CLI command AND MCP tool).
# A generic run_verifier already exists; these add a discoverable, individually-described tool per
# verifier so an agent browsing the toolset sees each check by name, with the right consumes/tier in
# its description. The convenience check_* tools above give the headline verifiers friendly typed
# signatures; these cover ALL of them with the generic (claim, evidence) contract.
# ---------------------------------------------------------------------------
def _safe_tool_suffix(verifier_id: str) -> str:
    """Turn a verifier id (``statcheck.v1``) into a tool-name suffix (``statcheck_v1``)."""
    return re.sub(r"[^0-9a-zA-Z_]+", "_", verifier_id).strip("_")


def _make_verifier_tool(verifier_id: str):
    """Build a closure that runs ``verifier_id``'s judge() — the body of its auto-generated tool."""

    def _tool(claim: Any = None, evidence: Any = None) -> dict[str, Any]:
        return _judge(verifier_id, claim, evidence)

    return _tool


def _register_verifier_tools(server: FastMCP, registry: Registry) -> list[str]:
    """Register one ``run_<id>`` tool per verifier, generated from its manifest (DESIGN §6.3).

    Returns the list of tool names registered. Called at import time (below) so the tools exist
    before the stdio loop starts.
    """
    names: list[str] = []
    for v in registry.all():
        m = v.manifest
        tool_name = f"run_{_safe_tool_suffix(m.id)}"
        description = (
            f"Run {m.id} (tier {m.epistemic_tier.value}, {m.kind.value}) on a claim + evidence and "
            f"return its deterministic Finding (DESIGN §3.1). Consumes claim types: "
            f"{', '.join(m.consumes)}. `evidence` is the extracted_values payload this verifier "
            f"reads (or a full Evidence object / list); `claim` may be a string or object. A FAIL "
            f"ships a runnable recompute_script (DESIGN §3.2). {m.description}"
        )
        try:
            server.add_tool(
                _make_verifier_tool(m.id),
                name=tool_name,
                description=description,
            )
            names.append(tool_name)
        except Exception:
            # A duplicate / bad name must not stop the others or the server (DESIGN §9, §13).
            continue
    return names


# Register the per-verifier tools against the live registry at import time.
AUTO_TOOL_NAMES = _register_verifier_tools(mcp, get_registry())


def main() -> None:
    """Run the LITMUS MCP server over stdio (the ``litmus-mcp`` console script)."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
