"""Host-side custom-tool handlers for the managed-agents auditor (DESIGN §3.1, §13, §15).

The managed-agents coordinator (``litmus/pipeline/managed.py``) does the non-deterministic
work — extract the ClaimGraph, convene persona sub-agents — but it is FORBIDDEN from rendering
a verdict on a checkable claim (DESIGN §3.1: "extract/locate/reason with the LLM; judge with
code"). The way that invariant is enforced over the wire is: the coordinator can only reach a
deterministic verdict by *calling a custom tool*, and **the host runs the real verifier code**
and hands back the resulting :class:`~litmus.core.finding.Finding`. The model never gets to
decide whether a checkable claim holds — it only decides *what to check*; the tool's code decides
*whether it holds*.

Three custom tools, all backed by the genuine first-party machinery:

  * ``list_verifiers``    -> the registry's verifiers + their ``consumes`` / tier / determinism,
        so the coordinator can route a claim to the right check (DESIGN §12).
  * ``run_verifier``      -> ``registry.get(id).judge(claim, evidence)`` on the REAL verifier,
        returning the serialized Finding (status/severity/discrepancy/recompute_script). This is
        the only path to a CHECKABLE verdict; the host executes it (DESIGN §3.1).
  * ``confirm_recompute`` -> re-runs a flag's ``recompute_script`` in the network-less recompute
        sandbox (DESIGN §7 G3, §13.4) and reports whether it reproduces ``expected_output`` — the
        fresh-context confirmation that drops self-caught false positives.

Everything here is pure host-side Python over the existing registry + sandbox; it takes the
claim/evidence the coordinator extracted (as JSON), rebuilds the domain objects, and runs the
deterministic code. No model call happens in this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from litmus.commons.registry import Registry, build_default_registry
from litmus.core import sandbox
from litmus.core.claim import Claim, Evidence
from litmus.core.finding import Finding, Status

# The custom-tool names the agent is told it has (must match the tool defs on the Agent and the
# dispatch table below). Kept as constants so the system prompt, the tool schema, and the handler
# never drift.
TOOL_LIST_VERIFIERS = "list_verifiers"
TOOL_RUN_VERIFIER = "run_verifier"
TOOL_CONFIRM_RECOMPUTE = "confirm_recompute"

CUSTOM_TOOL_NAMES = (TOOL_LIST_VERIFIERS, TOOL_RUN_VERIFIER, TOOL_CONFIRM_RECOMPUTE)


# --------------------------------------------------------------------------------------------
# Tool schema (declared on the Agent at create-time) — DESIGN §3.1 enforced over the wire.
# --------------------------------------------------------------------------------------------
def custom_tool_defs() -> list[dict[str, Any]]:
    """The ``{type:"custom", name, description, input_schema}`` blocks for the Agent's ``tools``.

    These are the ONLY way the coordinator can obtain a deterministic verdict — the descriptions
    make the contract explicit so the model routes checkable claims here instead of judging them.
    """
    claim_schema = {
        "type": "object",
        "description": "The claim being checked, as extracted (DESIGN §11).",
        "properties": {
            "id": {"type": "string"},
            "text": {"type": "string"},
            "epistemic_tier": {"type": "string"},
            "predicate": {"type": ["string", "null"]},
            "strength": {"type": ["string", "null"]},
            "scope": {"type": ["string", "null"]},
            "location": {"type": "object"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["id", "text"],
    }
    evidence_item = {
        "type": "object",
        "description": "One Evidence record the claim rests on. extracted_values carries the "
        "transcribed numbers the verifier recomputes against (use the canonical keys, "
        "e.g. {'test':'t','statistic':3.6,'df':41,'reported_p':0.013}).",
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string"},
            "location": {"type": "object"},
            "extracted_values": {"type": "object"},
            "confidence": {"type": "number"},
        },
        "required": ["id", "kind", "extracted_values"],
    }
    return [
        {
            "type": "custom",
            "name": TOOL_LIST_VERIFIERS,
            "description": (
                "List the deterministic verifiers in the LITMUS registry with what each consumes, "
                "its epistemic tier, and its determinism. Call this first to see which verifier to "
                "route a checkable claim to. Takes no arguments."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "type": "custom",
            "name": TOOL_RUN_VERIFIER,
            "description": (
                "Run a deterministic verifier on a checkable claim and its evidence. THE HOST RUNS "
                "THE REAL VERIFIER CODE and returns its verdict (Finding): status (pass|fail|"
                "inconclusive), severity, discrepancy, and — on a fail — a runnable recompute_script "
                "+ expected_output. This is the ONLY way to render a verdict on a checkable claim "
                "(T0-T2/T6): you must NEVER judge such a claim yourself (DESIGN §3.1). Pass the "
                "claim and the list of evidence records it rests on, with the numbers transcribed "
                "into extracted_values under the verifier's canonical keys."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "verifier_id": {
                        "type": "string",
                        "description": "The id from list_verifiers, e.g. 'statcheck.v1'.",
                    },
                    "claim": claim_schema,
                    "evidence": {"type": "array", "items": evidence_item},
                },
                "required": ["verifier_id", "claim", "evidence"],
            },
        },
        {
            "type": "custom",
            "name": TOOL_CONFIRM_RECOMPUTE,
            "description": (
                "Re-run a flag's recompute_script in a fresh, network-less sandbox and report "
                "whether its stdout reproduces expected_output (DESIGN §13.4). Use this to "
                "confirm a fail before reporting it; a flag whose script does not reproduce is a "
                "self-caught false positive and must be dropped. Pass the exact recompute_script "
                "and expected_output from the Finding that run_verifier returned."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "recompute_script": {"type": "string"},
                    "expected_output": {"type": "string"},
                },
                "required": ["recompute_script", "expected_output"],
            },
        },
    ]


# --------------------------------------------------------------------------------------------
# The handlers — pure host-side deterministic code over the real registry + sandbox.
# --------------------------------------------------------------------------------------------
@dataclass
class ToolCallRecord:
    """One custom-tool invocation + its result, captured so the host can assemble the report
    from the deterministic verdicts (not from the model's say-so) and audit what the agent did."""

    tool: str
    verifier_id: Optional[str]
    claim_id: Optional[str]
    status: Optional[str]
    finding: Optional[Finding]
    reproduced: Optional[bool]
    raw_input: dict[str, Any]


class VerifierToolHost:
    """Executes the coordinator's custom-tool calls against the real verifier library.

    Stateful: it records every ``run_verifier`` Finding and every ``confirm_recompute`` outcome,
    so :class:`~litmus.pipeline.managed.ManagedAgentExecutor` can build the AuditReport from the
    deterministic results the HOST computed — the model's classification is corroboration, the
    tool record is the source of truth (DESIGN §3.1, §3.6).
    """

    def __init__(self, registry: Optional[Registry] = None) -> None:
        self.registry = registry or build_default_registry()
        self.calls: list[ToolCallRecord] = []

    # --- dispatch ------------------------------------------------------------
    def handle(self, name: str, tool_input: dict[str, Any]) -> str:
        """Run one custom tool and return its result as a JSON string (the tool-result content).

        Never raises: a bad tool call returns a JSON ``{"error": ...}`` so the session keeps
        moving (an unanswered custom_tool_use deadlocks the session — SKILL.md gotcha #5).
        """
        try:
            if name == TOOL_LIST_VERIFIERS:
                return self._list_verifiers()
            if name == TOOL_RUN_VERIFIER:
                return self._run_verifier(tool_input or {})
            if name == TOOL_CONFIRM_RECOMPUTE:
                return self._confirm_recompute(tool_input or {})
            return json.dumps({"error": f"unknown tool {name!r}"})
        except Exception as exc:  # never deadlock the session on a handler bug
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    # --- list_verifiers ------------------------------------------------------
    def _list_verifiers(self) -> str:
        out = []
        for v in self.registry.all():
            m = v.manifest
            out.append(
                {
                    "id": m.id,
                    "consumes": list(m.consumes),
                    "epistemic_tier": m.epistemic_tier.value,
                    "determinism": m.determinism.value,
                    "capability_tags": list(m.capability_tags),
                    "description": m.description,
                }
            )
        self.calls.append(
            ToolCallRecord(TOOL_LIST_VERIFIERS, None, None, None, None, None, {})
        )
        return json.dumps({"verifiers": out})

    # --- run_verifier (the deterministic verdict path) -----------------------
    def _run_verifier(self, tool_input: dict[str, Any]) -> str:
        vid = tool_input.get("verifier_id")
        if not vid or vid not in self.registry:
            rec = ToolCallRecord(TOOL_RUN_VERIFIER, vid, None, None, None, None, tool_input)
            self.calls.append(rec)
            return json.dumps(
                {"error": f"no such verifier {vid!r}", "known": self.registry.ids()}
            )

        claim = _claim_from_input(tool_input.get("claim") or {})
        evidence = _evidence_from_input(tool_input.get("evidence") or [])

        # THE HOST RUNS THE REAL VERIFIER CODE (DESIGN §3.1). The model never decides the verdict.
        finding = self.registry.get(vid).judge(claim, evidence)

        self.calls.append(
            ToolCallRecord(
                TOOL_RUN_VERIFIER,
                vid,
                claim.id,
                finding.status.value,
                finding,
                None,
                tool_input,
            )
        )
        # Hand the agent the verdict + its executable evidence (so it can call confirm_recompute).
        result = {
            "verifier_id": vid,
            "claim_id": claim.id,
            "status": finding.status.value,
            "severity": finding.severity.value if finding.severity else None,
            "trust_tier": finding.trust_tier.value,
            "verifier_kind": finding.verifier_kind.value,
            "message": finding.message,
            "discrepancy": finding.discrepancy,
            "reported": finding.reported,
            "computed": finding.computed,
            "recompute_script": finding.evidence.recompute_script,
            "expected_output": finding.evidence.expected_output,
            "is_flag": finding.status is Status.FAIL,
        }
        return json.dumps(result)

    # --- confirm_recompute (fresh-context confirmation) ----------------------
    def _confirm_recompute(self, tool_input: dict[str, Any]) -> str:
        script = tool_input.get("recompute_script") or ""
        expected = tool_input.get("expected_output")
        if not script:
            self.calls.append(
                ToolCallRecord(TOOL_CONFIRM_RECOMPUTE, None, None, None, None, False, tool_input)
            )
            return json.dumps({"reproduced": False, "reason": "no recompute_script supplied"})

        ok, res = sandbox.reproduces(script, expected or "")
        reason = "" if ok else (
            "recompute_script did not reproduce expected_output in a fresh, network-less "
            f"sandbox{'' if res.ok else ': ' + (res.stderr or '').strip()[:200]}"
        )
        self.calls.append(
            ToolCallRecord(
                TOOL_CONFIRM_RECOMPUTE, None, None, None, None, ok, tool_input
            )
        )
        return json.dumps(
            {
                "reproduced": ok,
                "got": (res.stdout or "").strip()[:400],
                "expected": (expected or "").strip()[:400],
                "reason": reason,
            }
        )

    # --- accessors used by the assembler -------------------------------------
    def findings(self) -> list[Finding]:
        """Every Finding the host computed via ``run_verifier`` (deterministic verdicts)."""
        return [c.finding for c in self.calls if c.finding is not None]

    def confirmations(self) -> dict[str, bool]:
        """Map a recompute_script -> whether the sandbox reproduced it (most recent wins)."""
        out: dict[str, bool] = {}
        for c in self.calls:
            if c.tool == TOOL_CONFIRM_RECOMPUTE and c.reproduced is not None:
                script = c.raw_input.get("recompute_script") or ""
                out[script] = bool(c.reproduced)
        return out


# --------------------------------------------------------------------------------------------
# JSON -> domain object rebuilders (tolerant: the coordinator's transcription may be partial).
# --------------------------------------------------------------------------------------------
def _claim_from_input(d: dict[str, Any]) -> Claim:
    """Rebuild a Claim from the coordinator's JSON, tolerating missing optional fields."""
    payload = {
        "id": d.get("id") or "c?",
        "text": d.get("text") or "",
        "location": d.get("location") or {},
        "epistemic_tier": d.get("epistemic_tier"),
        "predicate": d.get("predicate"),
        "strength": d.get("strength"),
        "scope": d.get("scope"),
        "evidence_refs": list(d.get("evidence_refs") or []),
        "confidence": float(d.get("confidence", 1.0)) if d.get("confidence") is not None else 1.0,
    }
    try:
        return Claim.from_dict(payload)
    except Exception:
        # Last-resort: an unrecognized tier string shouldn't sink the verdict path.
        payload["epistemic_tier"] = None
        return Claim.from_dict(payload)


def _evidence_from_input(items: list[dict[str, Any]]) -> list[Evidence]:
    out: list[Evidence] = []
    for d in items or []:
        payload = {
            "id": d.get("id") or f"ev{len(out)}",
            "kind": d.get("kind") or "number",
            "location": d.get("location") or {},
            "extracted_values": d.get("extracted_values") or {},
            "confidence": float(d.get("confidence", 1.0)) if d.get("confidence") is not None else 1.0,
        }
        try:
            out.append(Evidence.from_dict(payload))
        except Exception:
            payload["kind"] = "number"
            out.append(Evidence.from_dict(payload))
    return out
