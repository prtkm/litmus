"""WS-H · the managed-agents auditor (DESIGN §13, §15, §19 Track 2).

The *hosted* half of the ``ExecutorAdapter`` seam (DESIGN §15). The owner's architecture, built
here against the live managed-agents API (beta ``managed-agents-2026-04-01``):

A **COORDINATOR** Agent (``multiagent:{type:"coordinator", agents:[personas]}``) runs one audit
session that **combines deterministic verifier TOOLS with non-deterministic multi-persona LLM
review**:

  1. It works from the extracted **ClaimGraph** (claims ← evidence ← location/quote, DESIGN §11) —
     the only model-in-the-loop step for *finding* things (DESIGN §13 step 1).
  2. For each **CHECKABLE** claim (T0-T2/T6) it CALLS a deterministic verifier **tool**
     (``run_verifier``). It MUST NEVER judge a checkable claim itself — the host runs the real
     ``registry.get(id).judge()`` and returns the Finding (DESIGN §3.1). Flags are re-run via
     ``confirm_recompute`` in the network-less sandbox and dropped if they don't reproduce (§13.4).
  3. It convenes **persona sub-agents** to review the non-deterministic dimensions: SKEPTIC,
     DOMAIN-EXPERT, METHODOLOGIST (T3), CLAIMS-AUDITOR (T4), INTEGRITY-SCREENER (T7). Personas
     REASON and surface concerns but DO NOT override deterministic verdicts (DESIGN §3.1, §3.6).
  4. It CLASSIFIES every claim — CORRECT | FLAGGABLE | SUBJECTIVE/ROUTE-TO-HUMAN — and emits a
     final structured JSON.

The host (:func:`run_managed_audit`) streams the session, answers every ``agent.custom_tool_use``
with the **real** verifier result, captures the deterministic tool records, and assembles a
schema-valid :class:`~litmus.core.provenance.AuditReport` from them. **The tool records are the
source of truth for checkable verdicts; the coordinator's classification only adds the subjective /
routed dimensions** (DESIGN §3.1, §3.6).

``ManagedAgentExecutor`` (in :mod:`litmus.pipeline.executor`) calls :func:`run_managed_audit`.
``audit_pdf`` extracts the ClaimGraph (Opus vision, DESIGN §11) then runs the coordinator session.
A :class:`~litmus.pipeline.executor.LocalExecutor` fallback keeps the seam real if the beta is
unavailable; ``meta["executor"]`` records which path ran (``"managed"`` vs
``"managed:fallback-local"``). Persistence to Supabase is via
:mod:`litmus.app_backend.supabase_io` (status queued→extracting→auditing→confirming→done).
"""

from __future__ import annotations

import json
import os
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from litmus.commons.registry import Registry, build_default_registry
from litmus.core.claim import Claim, ClaimGraph
from litmus.core.finding import (
    EvidencePacket,
    Finding,
    Severity,
    Status,
    TrustTier,
    VerifierKind,
)
from litmus.core.provenance import AuditReport, DroppedFlag, RoutedItem
from litmus.pipeline.executor import LocalExecutor
from litmus.pipeline.managed_tools import CUSTOM_TOOL_NAMES, VerifierToolHost, custom_tool_defs

# Beta surface for managed agents (SKILL.md: the SDK sets the header automatically on
# client.beta.{agents,environments,sessions,...} calls; only raw curl needs it explicitly).
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
DEFAULT_MODEL = "claude-opus-4-8"

# How long to let one managed session run before giving up and falling back (seconds). A fresh
# session's first turn includes sandbox cold-start before the agent acts, and the coordinator
# fans out to persona sub-agents, so keep this generous.
DEFAULT_SESSION_TIMEOUT_S = 900.0

# Sentinel the coordinator wraps its final structured output in, so the host can extract it from
# a transcript that may also contain prose / persona summaries.
FINAL_MARKER = "LITMUS_AUDIT"


# --------------------------------------------------------------------------------------------
# Persona sub-agents (DESIGN §13). They REASON about the non-deterministic dimensions and
# surface concerns; they NEVER render a deterministic verdict (that is the verifier tools' job).
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    system: str


PERSONAS: list[Persona] = [
    Persona(
        "skeptic",
        "Skeptic",
        "You are the SKEPTIC on a scientific-paper audit panel. Adversarially try to REFUTE the "
        "paper's central claims and find the weakest link — the assumption that, if wrong, "
        "collapses the result; the alternative explanation the authors did not rule out; the "
        "place the conclusion outruns the evidence. You REASON and surface concerns; you do NOT "
        "decide whether a numeric/checkable claim holds — deterministic verifier tools do that "
        "(DESIGN §3.1). Be concrete and cite the claim id and quote. If the paper is sound, say so.",
    ),
    Persona(
        "domain_expert",
        "Domain Expert",
        "You are the DOMAIN EXPERT on the audit panel. Judge field-level plausibility: are the "
        "magnitudes, mechanisms, units, and comparisons sensible for this field? Does anything "
        "violate established domain knowledge or look physically/biologically implausible? You "
        "surface concerns with reasoning; you never render a deterministic verdict on a checkable "
        "number (the verifier tools do — DESIGN §3.1). Cite claim ids and quotes.",
    ),
    Persona(
        "methodologist",
        "Methodologist",
        "You are the METHODOLOGIST (DESIGN §5 T3). Assess method appropriateness: was the right "
        "statistical test used for the design? Were multiple comparisons corrected? Is the study "
        "powered? Are assumptions (normality, independence, stationarity, causal identification) "
        "met? Surface method concerns as REASONING, not as a deterministic verdict — you flag "
        "*candidates* for human review; deterministic checks belong to the verifier tools "
        "(DESIGN §3.1). Cite claim ids.",
    ),
    Persona(
        "claims_auditor",
        "Claims Auditor",
        "You are the CLAIMS AUDITOR (DESIGN §5 T4). For each headline claim, check whether its "
        "STRENGTH and SCOPE match the evidence: over-generalization, extrapolation beyond the "
        "data range, causal language on an observational design, 'no effect' from an underpowered "
        "null, an abstract that overstates the body. This is calibrated judgement, surfaced for "
        "human review — never a deterministic verdict (DESIGN §3.1, §3.5). Cite the claim id, its "
        "stated strength/scope, and the quote.",
    ),
    Persona(
        "integrity_screener",
        "Integrity Screener",
        "You are the INTEGRITY SCREENER (DESIGN §5 T7). Surface integrity SIGNALS only — never a "
        "verdict: image duplication/manipulation hints, terminal-digit/Benford anomalies, "
        "too-perfect agreement, tortured phrasing, inconsistent reported Ns. A signal routes to a "
        "human (DESIGN §3.5); you do not accuse, you flag for review. Cite what you saw and where.",
    ),
]


def _coordinator_system() -> str:
    """The coordinator Agent's system prompt: combine deterministic tools + persona review,
    classify every claim, emit one structured result. Hard invariant inlined (DESIGN §3.1)."""
    persona_lines = "\n".join(f"      - {p.name}: {p.system.split('.')[0]}." for p in PERSONAS)
    return textwrap.dedent(
        f"""\
        You are the COORDINATOR of the LITMUS audit panel, auditing one scientific paper inside a
        sandboxed managed-agents session. LITMUS combines DETERMINISTIC verifier tools with
        non-deterministic multi-persona review. You orchestrate both and emit ONE structured result.

        THE HARD INVARIANT (DESIGN §3.1): for any CHECKABLE claim (tiers T0, T1, T2, T6 — internal
        arithmetic, fixed-knowledge lookups, internal cross-consistency, reproducibility) you MUST
        NOT decide whether it holds. You call the `run_verifier` custom tool; the host runs the real
        verifier CODE and returns the verdict (a Finding). The tool decides, never you. You only
        decide WHAT to check and route the inputs.

        YOUR PROCEDURE
        1. You are given the extracted ClaimGraph (claims, evidence, bindings) as JSON. Each claim
           has a proposed epistemic_tier; each evidence record has extracted_values with the
           transcribed numbers (often under canonical keys like
           {{"test":"t","statistic":3.6,"df":41,"reported_p":0.013}} or
           {{"reported_yield_pct":92}} or {{"parts":[...], "reported_total":...}}).
        2. Call `list_verifiers` once to see the available deterministic verifiers, what each
           consumes, and its tier.
        3. For EVERY checkable claim (T0/T1/T2/T6) that has the numbers a verifier needs, call
           `run_verifier` with the verifier_id, the claim, and the evidence it rests on (pass the
           evidence records verbatim — keep the canonical keys in extracted_values). Try the
           verifier(s) whose `consumes`/tier match. If `run_verifier` returns status "fail", call
           `confirm_recompute` with the returned recompute_script + expected_output to confirm the
           flag reproduces in the fresh sandbox; a flag that does not reproduce is a self-caught
           false positive — drop it (DESIGN §13.4). If every applicable verifier returns
           "inconclusive", the claim is not deterministically coverable here — record it as
           subjective/abstained with a short reason, do NOT invent a verdict.
        4. Convene the persona sub-agents to review the NON-deterministic dimensions and surface
           concerns (they reason; they never override a tool verdict):
        {persona_lines}
           Use their input for method (T3), strength/scope over-reach (T4), field plausibility,
           the weakest link, and integrity signals (T7) — these route to a human, they are not
           scored (DESIGN §3.5).
        5. CLASSIFY every claim into exactly one of:
             - "correct"   : a deterministic verifier ran and returned pass.
             - "flaggable" : a deterministic verifier returned fail AND it reproduced via
                             confirm_recompute (include the verifier_id and the claim id).
             - "subjective": not deterministically scorable here (T3/T4/T5/T7/T8, or no verifier
                             covered it / inputs missing) — route to human or abstain, with a reason.

        OUTPUT — when done, emit EXACTLY ONE line that begins with `{FINAL_MARKER} ` followed by a
        single-line JSON object, no code fences, with this shape:

        {FINAL_MARKER} {{"claims":[{{"claim_id":"c1","classification":"correct|flaggable|subjective",
          "verifier_id":"<id or null>","status":"pass|fail|inconclusive|null","reproduced":true|false|null,
          "reason":"<short>"}}, ...],
          "routed_to_human":[{{"claim_id":"c4 or null","dimension":"method|strength_scope|integrity:...|
          significance|novelty","note":"<persona concern>","quote":"<verbatim or null>"}}, ...],
          "panel_summary":"<2-3 sentences on the weakest link and overall soundness>"}}

        Every checkable verdict in your output must come from a `run_verifier` call you actually
        made — never assert pass/fail for a checkable claim without the tool. Be thorough but do not
        loop: one verifier attempt per applicable claim is enough.
        """
    )


# --------------------------------------------------------------------------------------------
# Resource handles + setup (Agent once, Session every run — SKILL.md "the one rule").
# --------------------------------------------------------------------------------------------
@dataclass
class ManagedResources:
    """The persisted control-plane handles. Create the coordinator agent + persona sub-agents +
    environment ONCE and reuse the ids (SKILL.md gotcha #2: don't recreate on the hot path)."""

    agent_id: str
    environment_id: str
    persona_agent_ids: dict[str, str] = field(default_factory=dict)


def _client(api_key: Optional[str] = None):
    """Construct the Anthropic SDK client (reads ANTHROPIC_API_KEY if not passed)."""
    import anthropic  # imported lazily so the framework never hard-depends on it

    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def _create_persona_agents(client: Any, *, model: str) -> dict[str, str]:
    """Create the five persona sub-agents and return {persona_key: agent_id} (DESIGN §13).

    Each is a plain Agent (model + system, no tools) — the coordinator delegates to them via the
    multiagent coordinator; they reason in isolated threads sharing the session's container.
    """
    ids: dict[str, str] = {}
    for p in PERSONAS:
        agent = client.beta.agents.create(
            name=f"LITMUS {p.name}",
            model=model,
            system=p.system,
        )
        ids[p.key] = agent.id
    return ids


def ensure_resources(
    client: Any,
    *,
    resources: Optional[ManagedResources] = None,
    model: str = DEFAULT_MODEL,
    networking: str = "limited",
) -> ManagedResources:
    """Create (or reuse) the LITMUS coordinator Agent (+ persona sub-agents) and Environment.

    ``resources`` short-circuits to reuse already-created handles (the app persists these).
    Otherwise reads ``LITMUS_AGENT_ID`` / ``LITMUS_ENVIRONMENT_ID`` from the env, and only creates
    fresh ones when neither is available.

    The environment is a **cloud** sandbox (DESIGN §15). The recompute confirmation runs in the
    network-less recompute sandbox host-side (via the ``confirm_recompute`` tool), so the cloud
    environment defaults to ``limited`` networking with package managers off (DESIGN §15: "do not
    weaken it").
    """
    if resources is not None:
        return resources

    agent_id = os.environ.get("LITMUS_AGENT_ID")
    environment_id = os.environ.get("LITMUS_ENVIRONMENT_ID")
    persona_ids: dict[str, str] = {}

    if environment_id is None:
        if networking == "unrestricted":
            net_cfg: dict[str, Any] = {"type": "unrestricted"}
        else:
            net_cfg = {"type": "limited", "allow_package_managers": False, "allow_mcp_servers": False}
        environment = client.beta.environments.create(
            name="litmus-audit-env",
            config={"type": "cloud", "networking": net_cfg},
        )
        environment_id = environment.id

    if agent_id is None:
        persona_ids = _create_persona_agents(client, model=model)
        agent = client.beta.agents.create(
            name="LITMUS Audit Coordinator",
            model=model,
            system=_coordinator_system(),
            tools=custom_tool_defs(),  # the deterministic verifier tools (host runs them)
            multiagent={
                "type": "coordinator",
                "agents": [{"type": "agent", "id": aid} for aid in persona_ids.values()],
            },
        )
        agent_id = agent.id

    return ManagedResources(
        agent_id=agent_id, environment_id=environment_id, persona_agent_ids=persona_ids
    )


# --------------------------------------------------------------------------------------------
# The session driver: stream, answer every custom-tool call with REAL verifier code, collect
# the coordinator's final structured output.
# --------------------------------------------------------------------------------------------
@dataclass
class _SessionRun:
    session_id: Optional[str]
    final: Optional[dict[str, Any]]
    transcript: str
    host: VerifierToolHost
    error: Optional[str] = None


def _claimgraph_brief(graph: ClaimGraph) -> str:
    """The ClaimGraph as compact JSON for the coordinator (claims + evidence + bindings)."""
    return json.dumps(graph.to_dict(), separators=(",", ":"))


def _run_audit_session(
    client: Any,
    resources: ManagedResources,
    graph: ClaimGraph,
    host: VerifierToolHost,
    *,
    timeout_s: float = DEFAULT_SESSION_TIMEOUT_S,
    on_event: Optional[Callable[[str, Any], None]] = None,
) -> _SessionRun:
    """Drive one coordinator session over ``graph``: stream-first, answer every custom_tool_use
    with the host's REAL verifier result, break on terminal idle (SKILL.md gotchas #3/#4/#5)."""
    session = client.beta.sessions.create(
        agent=resources.agent_id,
        environment_id=resources.environment_id,
        title=f"LITMUS audit · {graph.paper_id}",
    )

    def _emit(kind: str, payload: Any) -> None:
        if on_event is not None:
            try:
                on_event(kind, payload)
            except Exception:
                pass

    # The coordinator session exists — surface it before the kickoff so a live trace / the app's
    # progress feed has the session + paper to show while the first turn cold-starts.
    _emit("agent_started", {"session_id": session.id, "paper_id": graph.paper_id})

    kickoff = textwrap.dedent(
        f"""\
        Audit this paper. Its extracted ClaimGraph (claims, evidence with transcribed numbers, and
        bindings) follows as JSON. Run deterministic verifier tools on every checkable claim, convene
        the persona panel for the non-deterministic dimensions, classify every claim, and emit the
        single `{FINAL_MARKER} {{...}}` result line. Remember the hard invariant: never judge a
        checkable claim yourself — call `run_verifier`.

        ClaimGraph for {graph.paper_id!r}:
        {_claimgraph_brief(graph)}
        """
    )

    transcript_parts: list[str] = []
    deadline = time.monotonic() + timeout_s
    error: Optional[str] = None

    # ONE stream, held open for the whole session. We flush custom-tool results INLINE (without
    # closing the stream) the moment the session idles on `requires_action` — closing and reopening
    # the stream between tool batches loses the `agent.custom_tool_use` events that fire in the gap
    # (SSE has no replay, SKILL.md gotcha #6), which strands tool calls unanswered and hangs the
    # session. Keeping the stream open means every tool call the agent makes is delivered to us.
    pending: list[Any] = []

    def _flush(reason_log: str) -> None:
        """Answer every buffered custom-tool call with the REAL host result (SKILL.md gotcha #5).

        Each call's host result is a JSON string (the tool-result content); we parse it best-effort
        to surface a ``tool_result`` event (tool name + verdict status) for the live progress feed,
        then send all results on the same open stream."""
        if not pending:
            return
        results: list[dict[str, Any]] = []
        for call in pending:
            name = getattr(call, "name", "")
            result_text = host.handle(name, getattr(call, "input", {}) or {})
            _emit("tool_result", _tool_result_payload(name, result_text))
            results.append(
                {
                    "type": "user.custom_tool_result",
                    "custom_tool_use_id": call.id,
                    "content": [{"type": "text", "text": result_text}],
                }
            )
        pending.clear()
        client.beta.sessions.events.send(session_id=session.id, events=results)

    try:
        with client.beta.sessions.events.stream(session_id=session.id) as stream:
            # Stream-first, THEN send the kickoff (SKILL.md gotcha #3).
            client.beta.sessions.events.send(
                session_id=session.id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": kickoff}]}],
            )
            for event in stream:
                if time.monotonic() > deadline:
                    try:
                        client.beta.sessions.events.send(
                            session_id=session.id, events=[{"type": "user.interrupt"}]
                        )
                    except Exception:
                        pass
                    error = "session timeout"
                    break

                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    chunk = "".join(
                        b.text
                        for b in getattr(event, "content", [])
                        if getattr(b, "type", "") == "text"
                    )
                    # Best-effort persona attribution: in the coordinator/sub-agent setup a message
                    # may carry the authoring agent. The field name isn't contractually fixed, so we
                    # probe a few and skip silently if none is exposed (per the task: don't invent it).
                    persona = _message_author(event)
                    if persona:
                        # Emit a dict (not a bare string) so the worker flattens `persona` onto the
                        # event and the live feed renders the reviewer name instead of "(no detail)".
                        _emit("persona", {"persona": persona})
                    if chunk:
                        transcript_parts.append(chunk)
                        _emit("message", chunk)
                elif etype == "agent.custom_tool_use":
                    pending.append(event)  # buffer; flush when the session idles on requires_action
                    # Dict payload → the worker flattens `tool` onto the event so the feed shows the
                    # verifier/tool name (e.g. run_verifier) rather than a blank "(no detail)" row.
                    _emit("tool_use", {"tool": getattr(event, "name", "") or "tool"})
                elif etype == "session.status_idle":
                    stop = getattr(event, "stop_reason", None)
                    stop_type = getattr(stop, "type", None)
                    _emit("status", stop_type)
                    if stop_type == "requires_action":
                        # The agent is waiting on our custom-tool results. Flush them on the SAME
                        # stream and keep iterating — the continuation arrives here, no gap.
                        _flush("requires_action")
                        continue
                    # end_turn / retries_exhausted — terminal. Stop driving.
                    break
                elif etype == "session.status_terminated":
                    break
                elif etype == "session.error":
                    error = f"session.error: {getattr(event, 'message', '')!r}"
                    break
    except Exception as exc:  # network / API / beta-not-enabled surfaced mid-stream
        return _SessionRun(
            session.id, None, "".join(transcript_parts), host, f"{type(exc).__name__}: {exc}"
        )

    full = "".join(transcript_parts)
    final = _parse_final(full)
    if final is not None:
        _emit("classification", _classification_counts(final))
    if error is None and final is None:
        error = "no parseable LITMUS_AUDIT result in transcript"
    return _SessionRun(session.id, final, full, host, error)


def _tool_result_payload(name: str, result_text: str) -> dict[str, Any]:
    """Shape one ``tool_result`` event for the live feed: the tool name + the verdict ``status``
    (parsed from the host's JSON result for ``run_verifier``; ``reproduced`` for
    ``confirm_recompute``). Best-effort — a non-JSON / error result still yields the name."""
    payload: dict[str, Any] = {"tool": name}
    try:
        parsed = json.loads(result_text)
    except Exception:
        return payload
    if not isinstance(parsed, dict):
        return payload
    for key in ("status", "verifier_id", "claim_id", "is_flag", "reproduced", "error"):
        if key in parsed:
            payload[key] = parsed[key]
    return payload


def _message_author(event: Any) -> Optional[str]:
    """Best-effort persona/sub-agent attribution off an ``agent.message`` event. The managed-agents
    coordinator delegates to sub-agents in isolated threads; if the event exposes which agent
    authored the message we surface it as a ``persona`` event, else return ``None`` and skip (the
    field name is not contractually fixed — we do not invent an attribute that isn't there)."""
    for attr in ("author", "agent", "agent_name", "name", "source", "from_agent", "persona"):
        val = getattr(event, attr, None)
        if val is None:
            continue
        # The attribute may be a nested object (e.g. an agent ref) with a name/id.
        for sub in ("name", "id"):
            nested = getattr(val, sub, None)
            if isinstance(nested, str) and nested:
                return nested
        if isinstance(val, str) and val:
            return val
    return None


def _classification_counts(final: dict[str, Any]) -> dict[str, int]:
    """Counts of the coordinator's per-claim classifications (correct / flaggable / subjective) +
    how many it routed to a human — a compact, structured summary for the live feed."""
    counts = {"correct": 0, "flaggable": 0, "subjective": 0}
    for c in (final.get("claims") or []):
        if not isinstance(c, dict):
            continue
        cls = str(c.get("classification") or "")
        if cls in counts:
            counts[cls] += 1
    counts["routed_to_human"] = len(final.get("routed_to_human") or [])
    return counts


def _parse_final(text: str) -> Optional[dict[str, Any]]:
    """Extract the coordinator's ``LITMUS_AUDIT {...}`` JSON object from the transcript.

    Tolerant of the model wrapping it in prose/fences: scan from the last marker, then fall back to
    the last balanced ``{...}`` containing a ``"claims"`` key.
    """
    if not text:
        return None
    if FINAL_MARKER in text:
        tail = text[text.rindex(FINAL_MARKER) + len(FINAL_MARKER):].strip()
        obj = _first_balanced_object(tail)
        if obj is not None:
            return obj
    # Fallback: last balanced object with the expected key.
    best: Optional[dict[str, Any]] = None
    for start in range(len(text)):
        if text[start] != "{":
            continue
        obj = _first_balanced_object(text[start:])
        if isinstance(obj, dict) and "claims" in obj:
            best = obj
    return best


def _first_balanced_object(s: str) -> Optional[dict[str, Any]]:
    """Parse the first balanced ``{...}`` (string-aware) at the start of ``s`` as JSON."""
    if not s or s[0] != "{":
        # allow leading whitespace
        s = s.lstrip()
        if not s or s[0] != "{":
            return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[: i + 1])
                except Exception:
                    return None
    return None


# --------------------------------------------------------------------------------------------
# Assemble the AuditReport from the DETERMINISTIC tool records (source of truth), enriched by
# the coordinator's classification for subjective / routed dimensions (DESIGN §3.1, §3.6, §14).
# --------------------------------------------------------------------------------------------
def _assemble_report(
    graph: ClaimGraph,
    host: VerifierToolHost,
    final: Optional[dict[str, Any]],
    *,
    confirm: bool,
) -> AuditReport:
    """Build the report. CHECKABLE findings come from the host's ``run_verifier`` Findings; a FAIL
    is kept only if a ``confirm_recompute`` reproduced it (else it is a DroppedFlag, §13.4). The
    coordinator's ``routed_to_human`` adds the subjective/method/integrity dimensions (§3.5)."""
    confirmations = host.confirmations()
    host_findings = host.findings()

    findings: list[Finding] = []
    dropped: list[DroppedFlag] = []
    for f in host_findings:
        if f.status is Status.FAIL:
            script = f.evidence.recompute_script or ""
            problems = f.validate()
            if problems:
                dropped.append(DroppedFlag(finding=f, reason="; ".join(problems)))
                continue
            if not confirm:
                findings.append(f)
                continue
            reproduced = confirmations.get(script)
            if reproduced is None:
                # The coordinator didn't confirm via the tool — confirm host-side so the §13.4
                # invariant always holds (no flag ships unconfirmed).
                ok, _ = _host_confirm(f)
                reproduced = ok
            if reproduced:
                findings.append(f)
            else:
                dropped.append(
                    DroppedFlag(
                        finding=f,
                        reason="recompute_script did not reproduce expected_output in a fresh, "
                        "network-less sandbox (DESIGN §13.4)",
                    )
                )
        elif f.status is Status.PASS:
            findings.append(f)
        # INCONCLUSIVE/ERROR Findings from run_verifier are coverage signals, not report findings.

    # De-dup PASS/FAIL findings by (verifier_id, claim_id) — the model may retry a tool call.
    findings = _dedup_findings(findings)

    routed: list[RoutedItem] = []
    abstained: list[Finding] = []
    seen_routed: set[tuple[Optional[str], str]] = set()
    if final:
        for r in final.get("routed_to_human", []) or []:
            if not isinstance(r, dict):
                continue
            dim = str(r.get("dimension") or "unspecified")
            cid = r.get("claim_id")
            key = (cid, dim)
            if key in seen_routed:
                continue
            seen_routed.add(key)
            routed.append(
                RoutedItem(
                    claim_id=cid,
                    dimension=dim,
                    note=str(r.get("note") or ""),
                    quote=r.get("quote"),
                )
            )
        # Claims the coordinator marked "subjective" with no deterministic finding → abstained.
        covered = {f.claim_id for f in findings} | {d.finding.claim_id for d in dropped}
        for c in final.get("claims", []) or []:
            if not isinstance(c, dict):
                continue
            if c.get("classification") == "subjective":
                cid = c.get("claim_id")
                if cid and cid not in covered:
                    abstained.append(_subjective_abstain(graph, cid, str(c.get("reason") or "")))

    panel_summary = (final or {}).get("panel_summary", "")
    return AuditReport(
        paper_id=graph.paper_id,
        findings=findings,
        dropped_flags=dropped,
        routed_to_human=routed,
        abstained=abstained,
        meta={"panel_summary": panel_summary},
    )


def _host_confirm(f: Finding) -> tuple[bool, str]:
    from litmus.core import sandbox

    ok, res = sandbox.reproduces(
        f.evidence.recompute_script or "", f.evidence.expected_output or ""
    )
    return ok, ("" if ok else (res.stderr or "").strip()[:160])


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, Optional[str], str]] = set()
    for f in findings:
        key = (f.verifier_id, f.claim_id, f.status.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _subjective_abstain(graph: ClaimGraph, claim_id: str, reason: str) -> Finding:
    claim = graph.claim_by_id(claim_id)
    loc = claim.location if claim is not None else None
    return Finding(
        verifier_id="litmus.coordinator",
        claim_id=claim_id,
        status=Status.INCONCLUSIVE,
        trust_tier=TrustTier.ROUTED_TO_HUMAN,
        verifier_kind=VerifierKind.ASSISTED,
        message=reason or "not deterministically scorable; routed for human review (DESIGN §3.5)",
        evidence=EvidencePacket(
            quote=loc.quote if loc else None, location=loc if loc else EvidencePacket().location
        ),
    )


# --------------------------------------------------------------------------------------------
# Public entry point — what ManagedAgentExecutor calls (DESIGN §13, §15).
# --------------------------------------------------------------------------------------------
def run_managed_audit(
    graph: ClaimGraph,
    *,
    registry: Optional[Registry] = None,
    resources: Optional[ManagedResources] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    confirm: bool = True,
    timeout_s: float = DEFAULT_SESSION_TIMEOUT_S,
    allow_fallback: bool = True,
    on_event: Optional[Callable[[str, Any], None]] = None,
) -> AuditReport:
    """Audit ``graph`` inside a Claude managed-agents coordinator session and return a schema-valid
    report (DESIGN §13, §15).

    The coordinator runs deterministic verifier TOOLS (host-side ``registry.get(id).judge()`` —
    DESIGN §3.1) and convenes persona sub-agents for the non-deterministic dimensions. CHECKABLE
    findings + the dropped-flag log come from the host's tool records; the coordinator's
    classification adds the routed-to-human / abstained items. ``meta["executor"]`` is
    ``"managed"`` on success, or ``"managed:fallback-local"`` when the beta was unavailable and the
    deterministic pipeline ran in-process (so the seam is always real). Set
    ``allow_fallback=False`` to surface managed-agents failures instead of degrading.
    """
    registry = registry or build_default_registry()
    host = VerifierToolHost(registry)

    managed_meta: dict[str, Any] = {"beta": MANAGED_AGENTS_BETA, "model": model}
    run: Optional[_SessionRun] = None
    fell_back_reason: Optional[str] = None

    try:
        client = _client(api_key)
        res = ensure_resources(client, resources=resources, model=model)
        managed_meta["agent_id"] = res.agent_id
        managed_meta["environment_id"] = res.environment_id
        if res.persona_agent_ids:
            managed_meta["persona_agent_ids"] = res.persona_agent_ids
        run = _run_audit_session(
            client, res, graph, host, timeout_s=timeout_s, on_event=on_event
        )
        if run.session_id:
            managed_meta["session_id"] = run.session_id
        if run.error:
            fell_back_reason = run.error
    except Exception as exc:
        fell_back_reason = f"{type(exc).__name__}: {exc}"

    # Managed session ran and produced a final classification → assemble from the tool records.
    if run is not None and run.final is not None and not fell_back_reason:
        report = _assemble_report(graph, host, run.final, confirm=confirm)
        report.meta.update(
            {
                "executor": "managed",
                "architecture": "coordinator+personas+verifier-tools",
                "n_claims": len(graph.claims),
                "n_verifiers": len(registry),
                "confirmation": "managed-tool-sandbox" if confirm else "disabled",
                "personas": [p.key for p in PERSONAS],
                "tool_calls": _tool_call_stats(host),
                "managed": managed_meta,
                "registry_load_errors": registry.load_errors,
            }
        )
        return report

    # Fallback: run the deterministic pipeline in-process so the seam always yields a valid report.
    if not allow_fallback:
        raise RuntimeError(
            f"managed-agents audit unavailable and allow_fallback=False: {fell_back_reason}"
        )
    report = LocalExecutor(confirm=confirm).audit_graph(graph, registry)
    report.meta.update(
        {
            "executor": "managed:fallback-local",
            "architecture": "coordinator+personas+verifier-tools (fallback to local)",
            "confirmation": "fresh-sandbox (local fallback)" if confirm else "disabled",
            "managed": {
                **managed_meta,
                "fallback_reason": fell_back_reason,
                "transcript_tail": (run.transcript[-800:] if run else ""),
                "tool_calls": _tool_call_stats(host),
            },
        }
    )
    return report


def _tool_call_stats(host: VerifierToolHost) -> dict[str, int]:
    stats: dict[str, int] = {}
    for c in host.calls:
        stats[c.tool] = stats.get(c.tool, 0) + 1
    return stats
