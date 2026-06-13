"""WS-H · the managed-agents executor (DESIGN §13, §15, §19 Track 2).

This is the *hosted* half of the ``ExecutorAdapter`` seam (DESIGN §15). The audit pipeline
is written once in :mod:`litmus.pipeline.executor`; :class:`~litmus.pipeline.executor.LocalExecutor`
runs it with local subprocess/thread workers (CLI / gate / study, no external services), and
:class:`~litmus.pipeline.executor.ManagedAgentExecutor` runs it inside a **Claude managed-agents
session** for the live app. Only this module depends on managed agents.

``run_managed_audit`` is the entry point ``ManagedAgentExecutor`` calls. It:

  1. creates (or reuses) an **Agent** whose system prompt is the LITMUS auditor, an
     **Environment** (a cloud sandbox — DESIGN §15: "the session's sandbox is also where
     recompute scripts + synthesized verifiers execute"), and a **Session** (managed-agents
     beta ``managed-agents-2026-04-01``: ``client.beta.agents`` / ``environments`` / ``sessions``);
  2. drives that session to audit the given :class:`~litmus.core.claim.ClaimGraph`; and
  3. returns a schema-valid :class:`~litmus.core.provenance.AuditReport`.

**Verdicts stay deterministic (DESIGN §3.1).** The agent loop never *renders* a verdict.
What runs in the hosted sandbox is the part the design assigns to it: the **fresh-context
confirmation** of DESIGN §13 step 4 — every emitted flag's ``recompute_script`` is re-run in
the managed sandbox and dropped if it does not reproduce. Judging/planning are the same
deterministic code as ``LocalExecutor``; we hand the sandbox a self-contained runner, the
agent executes it with the built-in ``bash`` tool, and we read the confirmation result back.
This is the v1 contract requested for WS-H: the point is the hosted, sandboxed execution
surface, not re-deriving verdicts with an LLM.

If the managed-agents beta is not enabled on the key (or the API is unreachable), the function
**falls back to running the deterministic pipeline in-process** (``LocalExecutor`` logic) so the
seam is always real and callers always get a valid report; ``meta["executor"]`` records which
path actually ran (``"managed"`` vs ``"managed:fallback-local"``).
"""

from __future__ import annotations

import json
import os
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Optional

from litmus.commons.registry import Registry, build_default_registry
from litmus.core.claim import ClaimGraph
from litmus.core.finding import Finding, Status
from litmus.core.provenance import AuditReport, DroppedFlag
from litmus.pipeline.executor import LocalExecutor

# Beta surface for managed agents (SKILL.md: the SDK sets the header automatically on
# client.beta.{agents,environments,sessions,...} calls; only raw curl needs it explicitly).
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
DEFAULT_MODEL = "claude-opus-4-8"

# How long to let one managed session run before giving up and falling back (seconds).
# A fresh session's first turn includes sandbox cold-start (~45-60s observed) before the agent
# acts, so keep the default generous; the confirmation itself is sub-second.
DEFAULT_SESSION_TIMEOUT_S = 300.0

LITMUS_AUDITOR_SYSTEM = textwrap.dedent(
    """\
    You are the LITMUS audit executor running inside a sandboxed managed-agents session.

    LITMUS audits scientific papers with DETERMINISTIC verifiers — code renders every verdict,
    never the model (DESIGN §3.1). Your job is NOT to judge claims. Your job is to drive the
    hosted sandbox: run the self-contained Python program you are given (it performs the
    fresh-context confirmation of DESIGN §13 step 4 — re-running each flagged claim's
    recompute_script in this clean, network-less sandbox and dropping any flag that does not
    reproduce its expected output), then return its stdout verbatim.

    Rules:
      - Use the bash tool to execute exactly the command you are handed, VERBATIM and in a single
        call. Do NOT inspect, decode, reformat, split, or "check it first" — just run it. Do not
        edit it, do not substitute your own judgement for its output, do not invent or remove flags.
      - The program prints a single JSON object on its last line (prefixed `LITMUS_CONFIRM`).
        Return that line unchanged as your final message, with no commentary, fences, or extra text.
      - If the program errors, report the stderr verbatim so the orchestrator can fall back.
    """
)


# --------------------------------------------------------------------------------------------
# Resource handles + setup (Agent once, Session every run — SKILL.md "the one rule").
# --------------------------------------------------------------------------------------------
@dataclass
class ManagedResources:
    """The persisted control-plane handles. Create the agent/environment ONCE and reuse the
    ids (SKILL.md gotcha #2: don't recreate the agent on the hot path)."""

    agent_id: str
    environment_id: str


def _client(api_key: Optional[str] = None):
    """Construct the Anthropic SDK client (reads ANTHROPIC_API_KEY if not passed)."""
    import anthropic  # imported lazily so the framework never hard-depends on it

    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def ensure_resources(
    client: Any,
    *,
    resources: Optional[ManagedResources] = None,
    model: str = DEFAULT_MODEL,
    networking: str = "limited",
) -> ManagedResources:
    """Create (or reuse) the LITMUS Agent + Environment and return their ids.

    ``resources`` short-circuits to reuse already-created handles (the app persists these).
    Otherwise reads ``LITMUS_AGENT_ID`` / ``LITMUS_ENVIRONMENT_ID`` from the env, and only
    creates fresh ones when neither is available.

    The environment is a **cloud** sandbox (DESIGN §15: Anthropic runs the container). The
    recompute confirmation we run there is network-less by construction (it only re-executes
    stdlib recompute scripts), so we default to ``limited`` networking with package managers
    off — matching the recompute sandbox's safety profile (DESIGN §15, "do not weaken it").
    """
    if resources is not None:
        return resources

    agent_id = os.environ.get("LITMUS_AGENT_ID")
    environment_id = os.environ.get("LITMUS_ENVIRONMENT_ID")

    if environment_id is None:
        net_cfg: dict[str, Any]
        if networking == "unrestricted":
            net_cfg = {"type": "unrestricted"}
        else:
            net_cfg = {"type": "limited", "allow_package_managers": False, "allow_mcp_servers": False}
        environment = client.beta.environments.create(
            name="litmus-recompute-env",
            config={"type": "cloud", "networking": net_cfg},
        )
        environment_id = environment.id

    if agent_id is None:
        agent = client.beta.agents.create(
            name="LITMUS Auditor",
            model=model,
            system=LITMUS_AUDITOR_SYSTEM,
            # The built-in cloud toolset; we only need bash/write to run the confirmation
            # program, but enabling the set keeps the agent capable of the fuller §13 pipeline.
            tools=[{"type": "agent_toolset_20260401"}],
        )
        agent_id = agent.id

    return ManagedResources(agent_id=agent_id, environment_id=environment_id)


# --------------------------------------------------------------------------------------------
# The deterministic core (shared with LocalExecutor) + the sandbox confirmation runner.
# --------------------------------------------------------------------------------------------
def _judge_outcomes(graph: ClaimGraph, registry: Registry):
    """Run plan+judge for every claim using a confirm-disabled LocalExecutor, so judging is
    byte-for-byte the same deterministic code path as the local pipeline. Returns the report
    with flags NOT yet confirmed (confirmation happens in the managed sandbox)."""
    pre = LocalExecutor(confirm=False).audit_graph(graph, registry)
    return pre


# A self-contained program shipped into the managed sandbox. It reads a JSON array of flags
# (each: {idx, recompute_script, expected_output}) from FLAGS_JSON, re-runs each script in a
# fresh, network-less subprocess (the same confirmation contract as litmus.core.sandbox), and
# prints {"confirmed": [idx...], "dropped": [{"idx", "reason"}...]} as its last line.
# Kept dependency-free (stdlib only) so it runs in any cloud sandbox with no install.
_CONFIRM_RUNNER = textwrap.dedent(
    '''\
    import base64, json, os, subprocess, sys, tempfile, signal

    SITECUSTOMIZE = (
        "import socket as _s\\n"
        "def _deny(*a, **k):\\n"
        "    raise OSError('network disabled in LITMUS recompute sandbox (DESIGN \\u00a715)')\\n"
        "_s.socket=_deny; _s.create_connection=_deny; _s.getaddrinfo=_deny; _s.gethostbyname=_deny\\n"
    )

    def reproduces(script, expected, timeout_s=15.0):
        with tempfile.TemporaryDirectory(prefix="litmus-sbx-") as tmp:
            site = os.path.join(tmp, "_site"); os.mkdir(site)
            with open(os.path.join(site, "sitecustomize.py"), "w") as f: f.write(SITECUSTOMIZE)
            sp = os.path.join(tmp, "recompute.py")
            with open(sp, "w") as f: f.write(script or "")
            env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": site, "PYTHONHASHSEED": "0",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "LANG": "C",
                   "LC_ALL": "C", "TMPDIR": tmp, "HOME": tmp, "no_proxy": "*", "NO_PROXY": "*"}
            try:
                p = subprocess.Popen([sys.executable, sp], cwd=tmp, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True)
            except Exception as exc:
                return False, "spawn failed: %s" % exc
            try:
                out, err = p.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception: p.kill()
                return False, "wall-clock timeout"
            if p.returncode != 0:
                return False, "exit %d: %s" % (p.returncode, (err or "").strip()[:160])
            if (out or "").strip() != (expected or "").strip():
                return False, "stdout did not match expected_output"
            return True, ""

    # Flags are embedded as a base64'd JSON blob (no env plumbing): the orchestrator substitutes
    # __LITMUS_FLAGS_B64__ before shipping the program. base64 is pure ASCII, so it is safe inside
    # this Python literal AND inside a single-quoted shell heredoc — unlike a raw JSON literal,
    # whose escaped newlines/control chars and non-ASCII (e.g. \\u00a7) corrupt a """...""" literal.
    flags = json.loads(base64.b64decode("__LITMUS_FLAGS_B64__").decode("utf-8"))
    confirmed, dropped = [], []
    for fl in flags:
        ok, why = reproduces(fl.get("recompute_script"), fl.get("expected_output"))
        if ok:
            confirmed.append(fl["idx"])
        else:
            dropped.append({"idx": fl["idx"],
                            "reason": "recompute_script did not reproduce expected_output in a "
                                      "fresh, network-less sandbox (DESIGN \\u00a713.4): " + why})
    print("LITMUS_CONFIRM " + json.dumps({"confirmed": confirmed, "dropped": dropped}))
    '''
)


def _build_confirm_program(flags: list[Finding]) -> str:
    """The complete self-contained program shipped to the sandbox: the runner with the flag array
    embedded as a base64'd JSON blob. stdlib-only, single file, no env/argv dependency. base64
    keeps the payload pure-ASCII so it survives both the Python literal and the shell heredoc
    (a raw JSON literal does not — escaped newlines/control chars/non-ASCII corrupt it)."""
    import base64

    flags_json = json.dumps(_confirm_payload(flags))
    flags_b64 = base64.b64encode(flags_json.encode("utf-8")).decode("ascii")
    return _CONFIRM_RUNNER.replace("__LITMUS_FLAGS_B64__", flags_b64)


def _confirm_payload(flags: list[Finding]) -> list[dict[str, Any]]:
    """Project flags to the minimal JSON the sandbox runner consumes."""
    out: list[dict[str, Any]] = []
    for i, f in enumerate(flags):
        out.append(
            {
                "idx": i,
                "recompute_script": f.evidence.recompute_script or "",
                "expected_output": f.evidence.expected_output or "",
            }
        )
    return out


def _parse_confirm_result(text: str) -> Optional[dict[str, Any]]:
    """Pull the ``{"confirmed":[...], "dropped":[...]}`` object out of the agent's reply.

    Tolerant of the model wrapping it in prose or fences: we scan for our ``LITMUS_CONFIRM``
    sentinel first, then fall back to the last balanced JSON object containing "confirmed".
    """
    if not text:
        return None
    marker = "LITMUS_CONFIRM"
    if marker in text:
        tail = text[text.rindex(marker) + len(marker):].strip()
        try:
            return json.loads(tail[: tail.index("}") + 1]) if "}" in tail else json.loads(tail)
        except Exception:
            pass
    # Fallback: find the last {...} that parses and has the expected keys.
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        for end in range(start, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : end + 1])
                    except Exception:
                        break
                    if isinstance(obj, dict) and "confirmed" in obj:
                        return obj
                    break
    return None


# --------------------------------------------------------------------------------------------
# Drive one managed session: ship the runner, run it in the sandbox, collect the result.
# --------------------------------------------------------------------------------------------
@dataclass
class _SessionRun:
    session_id: Optional[str]
    confirm_result: Optional[dict[str, Any]]
    transcript: str
    error: Optional[str] = None


def _run_confirmation_session(
    client: Any,
    resources: ManagedResources,
    flags: list[Finding],
    *,
    paper_id: str,
    timeout_s: float = DEFAULT_SESSION_TIMEOUT_S,
) -> _SessionRun:
    """Create a session, ask the agent to run the confirmation program over ``flags`` in the
    sandbox, and stream until it returns the result JSON (SKILL.md: stream-first, then send;
    don't break on bare idle — check stop_reason; break on terminated)."""
    # Ship the COMPLETE program (runner + embedded flags) in a SINGLE bash command via a
    # single-quoted heredoc — content is passed byte-for-byte (no shell interpolation), the runner
    # is pure ASCII Python, and it runs in one tool call so the recompute scripts execute in the
    # managed sandbox, not here. (Earlier hex+separate-FLAGS_JSON and base64-pipe attempts proved
    # brittle: the model stalled between two commands, or "inspected"/mangled the blob and
    # `base64 -d` rejected it. A literal heredoc removes both failure modes.)
    program = _build_confirm_program(flags)
    heredoc = f"cat > /tmp/litmus_confirm.py <<'LITMUS_PYEOF'\n{program}\nLITMUS_PYEOF\npython3 /tmp/litmus_confirm.py"
    kickoff = textwrap.dedent(
        """\
        Audit confirmation for paper {pid!r}: re-run {n} flagged claim(s)' recompute_script in this
        fresh, network-less sandbox and report which reproduce. The verdicts are deterministic — do
        NOT judge the claims yourself; just run the program and relay its output.

        Make EXACTLY ONE bash tool call, passing this command UNCHANGED (it writes a stdlib-only
        Python program via a heredoc and runs it; the program prints one `LITMUS_CONFIRM {{...}}`
        JSON line). Do not inspect, decode, reformat, or split it — run it verbatim:

        ```bash
        {cmd}
        ```

        Then return that `LITMUS_CONFIRM ...` line verbatim as your final message — nothing else,
        no commentary, no code fences.
        """
    ).format(pid=paper_id, n=len(flags), cmd=heredoc)

    session = client.beta.sessions.create(
        agent=resources.agent_id,
        environment_id=resources.environment_id,
        title=f"LITMUS confirm · {paper_id}",
    )
    transcript_parts: list[str] = []
    deadline = time.monotonic() + timeout_s
    last_text = ""

    try:
        with client.beta.sessions.events.stream(session_id=session.id) as stream:
            client.beta.sessions.events.send(
                session_id=session.id,
                events=[{"type": "user.message", "content": [{"type": "text", "text": kickoff}]}],
            )
            for event in stream:
                if time.monotonic() > deadline:
                    try:
                        client.beta.sessions.events.send(
                            session_id=session.id,
                            events=[{"type": "user.interrupt"}],
                        )
                    except Exception:
                        pass
                    return _SessionRun(session.id, None, "".join(transcript_parts), "session timeout")
                etype = getattr(event, "type", "")
                if etype == "agent.message":
                    chunk = "".join(
                        b.text for b in getattr(event, "content", []) if getattr(b, "type", "") == "text"
                    )
                    if chunk:
                        transcript_parts.append(chunk)
                        last_text = chunk
                elif etype == "session.status_idle":
                    stop = getattr(event, "stop_reason", None)
                    if getattr(stop, "type", None) != "requires_action":
                        break  # end_turn / retries_exhausted — terminal
                    # requires_action with the agent_toolset is a server-run tool confirmation
                    # under always_allow → shouldn't occur; nothing to answer, keep streaming.
                elif etype == "session.status_terminated":
                    break
                elif etype == "session.error":
                    return _SessionRun(
                        session.id, None, "".join(transcript_parts),
                        f"session.error: {getattr(event, 'message', '')!r}",
                    )
    except Exception as exc:  # network / API / beta-not-enabled surfaced mid-stream
        return _SessionRun(session.id, None, "".join(transcript_parts), f"{type(exc).__name__}: {exc}")

    full = "".join(transcript_parts) or last_text
    result = _parse_confirm_result(full)
    if result is None:
        return _SessionRun(session.id, None, full, "no parseable confirmation result in transcript")
    return _SessionRun(session.id, result, full)


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
    on_event: Optional[Any] = None,
) -> AuditReport:
    """Audit ``graph`` inside a Claude managed-agents session and return a schema-valid report.

    Judging/planning are the same deterministic code as :class:`LocalExecutor` (DESIGN §3.1);
    the managed **session's sandbox** performs the fresh-context confirmation of DESIGN §13
    step 4 — every flag's ``recompute_script`` is re-run there and dropped if it doesn't
    reproduce. ``meta["executor"]`` is ``"managed"`` when the hosted sandbox confirmed the
    flags, or ``"managed:fallback-local"`` when the beta was unavailable and confirmation ran
    in-process (so the seam is always real and the caller always gets a valid report).

    Set ``allow_fallback=False`` to surface managed-agents failures instead of degrading.
    ``resources`` reuses an already-created Agent+Environment (the app persists these ids).
    """
    registry = registry or build_default_registry()

    # 1) Deterministic plan + judge (flags NOT yet confirmed). Identical to LocalExecutor.
    pre = _judge_outcomes(graph, registry)
    flags = [f for f in pre.findings if f.status is Status.FAIL]

    if not confirm:
        pre.meta.update({"executor": "managed", "confirmation": "disabled", "n_claims": len(graph.claims)})
        return pre

    # 2) Confirm the flags in the managed sandbox (DESIGN §15 hosted surface).
    managed_meta: dict[str, Any] = {"beta": MANAGED_AGENTS_BETA, "model": model}
    run: Optional[_SessionRun] = None
    fell_back_reason: Optional[str] = None

    if flags:  # only spin up a session when there is something to confirm
        try:
            client = _client(api_key)
            res = ensure_resources(client, resources=resources, model=model)
            managed_meta["agent_id"] = res.agent_id
            managed_meta["environment_id"] = res.environment_id
            run = _run_confirmation_session(
                client, res, flags, paper_id=graph.paper_id, timeout_s=timeout_s
            )
            if run.session_id:
                managed_meta["session_id"] = run.session_id
            if on_event is not None:
                try:
                    on_event(run)
                except Exception:
                    pass
            if run.error or run.confirm_result is None:
                fell_back_reason = run.error or "no confirmation result"
        except Exception as exc:
            # Beta not enabled on the key, SDK too old, auth/network failure — degrade cleanly.
            fell_back_reason = f"{type(exc).__name__}: {exc}"
    else:
        managed_meta["note"] = "no flags to confirm; no session created"

    # 3a) Managed confirmation succeeded → keep/drop per the sandbox's verdict.
    if flags and run is not None and run.confirm_result is not None and not fell_back_reason:
        report = _assemble_from_confirmation(pre, flags, run.confirm_result)
        report.meta.update(
            {
                "executor": "managed",
                "n_claims": len(graph.claims),
                "n_verifiers": len(registry),
                "confirmation": "managed-sandbox",
                "managed": managed_meta,
                "synthesis_candidates": pre.meta.get("synthesis_candidates", []),
                "registry_load_errors": pre.meta.get("registry_load_errors", []),
            }
        )
        return report

    if not flags:
        # Nothing to confirm; the pre-report already has zero flags. Mark it managed.
        pre.meta.update(
            {
                "executor": "managed",
                "confirmation": "managed-sandbox",
                "managed": managed_meta,
                "n_claims": len(graph.claims),
            }
        )
        return pre

    # 3b) Fallback: confirm in-process with the local sandbox so the seam still produces a
    # correct, schema-valid report (documented: meta.executor == "managed:fallback-local").
    if not allow_fallback:
        raise RuntimeError(
            f"managed-agents confirmation unavailable and allow_fallback=False: {fell_back_reason}"
        )
    report = LocalExecutor(confirm=True).audit_graph(graph, registry)
    report.meta.update(
        {
            "executor": "managed:fallback-local",
            "confirmation": "fresh-sandbox (local fallback)",
            "managed": {**managed_meta, "fallback_reason": fell_back_reason,
                        "transcript_tail": (run.transcript[-500:] if run else "")},
        }
    )
    return report


def _assemble_from_confirmation(
    pre: AuditReport, flags: list[Finding], confirm_result: dict[str, Any]
) -> AuditReport:
    """Rebuild the report keeping only flags the managed sandbox reproduced; the rest become
    DroppedFlags (DESIGN §13 step 4 — the dropped-flag log is the autonomy evidence, §14)."""
    confirmed_idx = set(confirm_result.get("confirmed", []))
    drop_reasons = {d["idx"]: d.get("reason", "did not reproduce") for d in confirm_result.get("dropped", [])}

    # Non-flag findings (PASS, etc.) carry over untouched.
    kept_findings: list[Finding] = [f for f in pre.findings if f.status is not Status.FAIL]
    dropped: list[DroppedFlag] = []
    for i, f in enumerate(flags):
        if i in confirmed_idx:
            kept_findings.append(f)
        else:
            reason = drop_reasons.get(
                i,
                "recompute_script did not reproduce expected_output in the managed sandbox "
                "(DESIGN §13.4)",
            )
            dropped.append(DroppedFlag(finding=f, reason=reason))

    return AuditReport(
        paper_id=pre.paper_id,
        findings=kept_findings,
        dropped_flags=dropped,
        routed_to_human=list(pre.routed_to_human),
        abstained=list(pre.abstained),
        meta=dict(pre.meta),
    )
