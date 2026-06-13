---
name: claude-managed-agents
description: How to build and run Claude Managed Agents — Anthropic's hosted API for stateful, long-running agents (create a versioned Agent, an Environment, then stream Sessions). Use when setting up a managed/hosted Claude agent, working with client.beta.agents / sessions / environments, the managed-agents-2026-04-01 beta, the agent_toolset_20260401 toolset, custom tools, MCP via vaults, or session event streaming. NOT for Claude Code in-session subagents (.claude/agents/).
---

# Claude Managed Agents

Managed Agents is a **hosted Claude API** for stateful, long-running agents. Anthropic runs the agent loop on its orchestration layer and provisions a **sandboxed container per session** where the agent's *tools* execute (bash, file ops, code). You supply two things — an **Agent** config and an **Environment** config — and Anthropic handles the loop, the sandbox, the SSE event stream, prompt caching, context compaction, and extended thinking.

> **Not to be confused with Claude Code subagents** (`.claude/agents/*.md`, invoked via the Agent/Task tool). Those are context-local to a single Claude Code session. Managed Agents are infrastructure-level, persisted, hosted resources reached through the Claude API (`client.beta.agents` / `sessions` / `environments`). This skill is about the latter.

## When to use it

| Use Managed Agents for | Use something else for |
|---|---|
| Long-running, multi-turn agents with a persistent workspace | One-shot classify / summarize / extract → **Messages API** (`client.messages.create`) |
| Server-managed loop + Anthropic-hosted tool sandbox | You want to host the tool runtime yourself → **Claude API + tool use** (the tool runner) |
| Persisted, **versioned** agent configs reused across many runs | An in-editor subagent for one Claude Code session → **Claude Code subagents** |
| File mounts, GitHub repos, SSE event stream, Skills + MCP | |

**Availability:** first-party Claude API and Claude Platform on AWS. **Not** on Amazon Bedrock, Google Vertex AI, or Microsoft Foundry — use Claude API + tool use there.

---

## ⚠️ The one rule that governs everything: Agent (once) → Session (every run)

```
┌─ setup (ONCE) ─────────────┐      ┌─ runtime (EVERY run) ──────────┐
│ environments.create()      │      │ sessions.create(               │
│ agents.create()            │ ───▶ │   agent=AGENT_ID,              │
│   → persist env_id, agent_id│      │   environment_id=ENV_ID)       │
└────────────────────────────┘      │ events.stream() / events.send()│
                                     └────────────────────────────────┘
```

- `model`, `system`, `tools`, `mcp_servers`, `skills` are **top-level fields on the Agent** — **never** on the session. The session's `agent` field accepts only a string ID (`"agent_abc123"`, latest version) or `{type: "agent", id, version}` (pinned).
- **Create the agent once** and reuse the ID. Calling `agents.create()` on every run accumulates orphaned agents, pays create latency for nothing, and defeats versioning. Hoist it to a setup script or guard it (`if not AGENT_ID:`).
- To change an agent's behavior, **update it** (`agents.update`) — each update creates a new immutable version; running sessions keep their pinned version.

If you're about to write `sessions.create(model=..., system=..., tools=...)` — **stop.** Those go on `agents.create()`.

---

## Prerequisites

1. Anthropic API key: `export ANTHROPIC_API_KEY="sk-ant-..."`
2. SDK: `pip install anthropic` (Python) or `npm install @anthropic-ai/sdk` (TypeScript). Managed Agents support is also in the Go, Ruby, PHP, Java, and C# SDKs, and via raw HTTP / the `ant` CLI.
3. Beta header `managed-agents-2026-04-01` — **the SDK sets this automatically** on all `client.beta.{agents,environments,sessions,vaults,memory_stores}.*` calls. Only raw curl needs it explicitly.
4. Model: default to `claude-opus-4-8`.

---

## Complete example (Python)

The example splits **setup** (run once) from **runtime** (run every invocation), per the rule above.

### Setup — run once, persist the IDs

```python
import anthropic

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

# 1. Environment — the container template the agent's tools run in
environment = client.beta.environments.create(
    name="coding-env",
    config={
        "type": "cloud",                       # or "self_hosted"
        "networking": {"type": "unrestricted"},  # or {"type": "limited", ...}
    },
)

# 2. Agent — persisted, versioned config (model/system/tools live HERE)
agent = client.beta.agents.create(
    name="Coding Assistant",
    model="claude-opus-4-8",
    system="You are a careful coding assistant. Write clean, tested code.",
    tools=[
        {"type": "agent_toolset_20260401"},    # bash/read/write/edit/glob/grep/web_fetch/web_search
        {
            "type": "custom",                  # YOUR app executes this one
            "name": "run_tests",
            "description": "Run the project test suite. Call this after editing code.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Test path"}},
                "required": ["path"],
            },
        },
    ],
)

print("SAVE THESE:", environment.id, agent.id)  # e.g. env_..., agent_...
```

### Runtime — create a session per run and stream it

```python
import anthropic

client = anthropic.Anthropic()
ENV_ID, AGENT_ID = "env_...", "agent_..."  # loaded from config/env/db


def run_custom_tool(name: str, tool_input: dict) -> str:
    if name == "run_tests":
        return "All 42 tests passed."        # your real implementation
    return f"Unknown tool: {name}"


# 1. Create a session pointing at the pre-created agent + environment.
#    Blocks until any resources (files/repos) are mounted.
session = client.beta.sessions.create(
    agent=AGENT_ID,                          # string = latest version
    environment_id=ENV_ID,
    title="Add a retry helper",
)
# Handy while developing — watch it live in the Console:
print(f"https://platform.claude.com/workspaces/default/sessions/{session.id}")

# 2. STREAM FIRST, then send. The stream only delivers events that occur
#    AFTER it opens — open it before sending the kickoff or you miss events.
while True:
    pending_tool_calls = []
    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": "Add a retry() helper and test it."}],
            }],
        )
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        print(block.text, end="", flush=True)
            elif event.type == "agent.custom_tool_use":
                pending_tool_calls.append(event)     # session goes idle awaiting your result
            elif event.type == "session.status_idle":
                # DON'T break on bare idle — it fires transiently. Check stop_reason.
                if event.stop_reason.type == "requires_action":
                    break          # waiting on you (custom tool / confirmation) — handle it
                else:
                    pending_tool_calls = None         # end_turn / retries_exhausted = terminal
                    break
            elif event.type == "session.status_terminated":
                pending_tool_calls = None
                break

    if not pending_tool_calls:                        # None or [] → done
        break

    # 3. Answer every custom tool call, or the session hangs forever.
    client.beta.sessions.events.send(
        session_id=session.id,
        events=[{
            "type": "user.custom_tool_result",
            "custom_tool_use_id": call.id,
            "content": [{"type": "text", "text": run_custom_tool(call.name, call.input)}],
        } for call in pending_tool_calls],
    )
```

### Same core calls in TypeScript

```typescript
import Anthropic from "@anthropic-ai/sdk";
const client = new Anthropic();

const environment = await client.beta.environments.create({
  name: "coding-env",
  config: { type: "cloud", networking: { type: "unrestricted" } },
});
const agent = await client.beta.agents.create({
  name: "Coding Assistant",
  model: "claude-opus-4-8",
  system: "You are a careful coding assistant.",
  tools: [{ type: "agent_toolset_20260401" }],
});
const session = await client.beta.sessions.create({
  agent: agent.id,
  environment_id: environment.id,
});

const stream = await client.beta.sessions.events.stream(session.id);
await client.beta.sessions.events.send(session.id, {
  events: [{ type: "user.message", content: [{ type: "text", text: "Hello" }] }],
});
for await (const event of stream) {
  if (event.type === "agent.message")
    for (const b of event.content) if (b.type === "text") process.stdout.write(b.text);
  if (event.type === "session.status_terminated") break;
  if (event.type === "session.status_idle" && event.stop_reason.type !== "requires_action") break;
}
```

### Raw HTTP (curl)

```bash
HDR=(-H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
     -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json")

# Agent first
curl -X POST https://api.anthropic.com/v1/agents "${HDR[@]}" -d '{
  "name": "Coding Assistant", "model": "claude-opus-4-8",
  "tools": [{"type": "agent_toolset_20260401"}]
}'   # → {"id": "agent_abc123", "version": "...", ...}

# Then a session referencing it
curl -X POST https://api.anthropic.com/v1/sessions "${HDR[@]}" -d '{
  "agent": {"type": "agent", "id": "agent_abc123"}, "environment_id": "env_abc123"
}'

# Send a message, then stream
curl -X POST https://api.anthropic.com/v1/sessions/$SID/events "${HDR[@]}" -d '{
  "events": [{"type": "user.message", "content": [{"type": "text", "text": "Hello"}]}]
}'
curl -N https://api.anthropic.com/v1/sessions/$SID/events/stream "${HDR[@]}"
```

---

## Reference

### Agent config fields (`agents.create` / `agents.update`)

| Field | Required | Notes |
|---|---|---|
| `name` | ✅ | 1–256 chars |
| `model` | ✅ | Bare string (`"claude-opus-4-8"`) or `{id, speed}`. Claude 4.5+ |
| `system` | | System prompt, ≤ 100K chars |
| `tools` | | Agent toolset / `mcp_toolset` / custom tools. Max 128 |
| `mcp_servers` | | `{type:"url", name, url}` only — **no auth here** (auth → vaults). Max 20 |
| `skills` | | `{type:"anthropic", skill_id:"xlsx"}` or `{type:"custom", skill_id, version}`. Max 20 |
| `multiagent` | | `{type:"coordinator", agents:[...]}` — delegate to sub-agents |
| `description`, `metadata` | | |

### Environment config

- `config.type`: `"cloud"` (Anthropic runs the container) or `"self_hosted"` (your worker runs the tools).
- `config.networking`: `{"type":"unrestricted"}` or `{"type":"limited", "allow_package_managers":bool, "allow_mcp_servers":bool, "allowed_hosts":[...]}`. Under `limited`, MCP server domains must be in `allowed_hosts` or set `allow_mcp_servers: true`, else tools silently fail.

### The built-in toolset (`agent_toolset_20260401`)

`bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch`, `web_search`. Enable all at once, or configure per-tool:

```python
{"type": "agent_toolset_20260401",
 "default_config": {"enabled": True},
 "configs": [{"name": "bash", "enabled": False}]}        # everything except bash
```

**Permission policies** (server-executed tools only): `always_allow` (default) or `always_ask`. With `always_ask`, the session idles and you reply with `user.tool_confirmation` (`tool_use_id` = the event id, `result` = `"allow"`/`"deny"`).

### Three kinds of tools

| Kind | Who runs it | How you handle it |
|---|---|---|
| Built-in toolset | Anthropic (cloud container) | Nothing — automatic |
| `mcp_toolset` | Anthropic orchestration | Declare server on agent; credentials in a **vault** attached via `vault_ids` |
| `custom` | **Your app** | Agent emits `agent.custom_tool_use` → session idles → you send `user.custom_tool_result` |

### Key event types (received on the stream)

`agent.message`, `agent.thinking`, `agent.tool_use` / `agent.tool_result`, `agent.mcp_tool_use`, `agent.custom_tool_use`, `session.status_running` / `session.status_idle` / `session.status_terminated`, `session.error`, `span.model_request_end` (carries `model_usage` for cost tracking).

### Send-able events

`user.message`, `user.interrupt`, `user.tool_confirmation`, `user.custom_tool_result`, `user.define_outcome` (rubric-graded iterate loop).

### Endpoints / SDK methods

| Resource | Python / TS (`client.beta.*`) | REST |
|---|---|---|
| Agents | `agents.create / retrieve / update / list / archive` | `/v1/agents` |
| Environments | `environments.create / … / delete / archive` | `/v1/environments` |
| Sessions | `sessions.create / retrieve / update / list / delete / archive` | `/v1/sessions` |
| Events | `sessions.events.stream / send / list` | `/v1/sessions/{id}/events[/stream]` |

---

## Critical gotchas (the ones that actually bite)

1. **Agent first, then session.** `model`/`system`/`tools`/`mcp_servers`/`skills` go on the agent. The session takes only an agent pointer. Putting them on the session is the #1 mistake.
2. **Agent once, not per run.** Persist `agent_id`; don't recreate on the hot path.
3. **Stream-first ordering.** Open `events.stream()` *before* `events.send()` the kickoff — the stream only delivers events emitted after it opens.
4. **Don't break on bare `session.status_idle`.** It fires transiently (between parallel tools, waiting on a confirmation/custom-tool result). Break on `session.status_terminated`, or on idle when `stop_reason.type != "requires_action"`.
5. **Answer custom tool calls.** Every `agent.custom_tool_use` must be answered with a `user.custom_tool_result` (`custom_tool_use_id` + `content`) or the session deadlocks. (Tool *confirmations* use `user.tool_confirmation` with `tool_use_id` instead.)
6. **SSE has no replay.** If the stream drops, reconnect by also fetching `events.list()` and de-duping on `event.id` — otherwise you lose events from the gap.
7. **MCP auth lives in vaults, not the agent.** The agent declares `{type, name, url}`; credentials go in `client.beta.vaults.credentials.create(...)` and attach to the session via `vault_ids`. Anthropic auto-refreshes OAuth tokens. (Note: hosted MCP servers want OAuth bearer tokens, not the service's native API key.)
8. **Secrets never enter the container.** No way to set container env vars; vaults are MCP-only. For a non-MCP API/CLI that needs a secret, declare a **custom tool** and run the authenticated call host-side in your orchestrator.
9. **Archive is permanent.** Archiving an agent/environment/session/vault makes it read-only with no unarchive; archived agents/environments can't back new sessions. Don't archive production resources as cleanup.
10. **Session outputs.** The agent writes deliverables to `/mnt/session/outputs/`. Retrieve them with `files.list(scope_id=session.id, betas=["managed-agents-2026-04-01"])` then `files.download(...)`. (~1–3s indexing lag after idle.)

---

## Beyond the basics

- **Version-controlled config** — define agents/environments as YAML and apply with the `ant` CLI (`ant beta:agents create < agent.yaml`); drive sessions from the SDK. Good split: CLI for the control plane, SDK for the data plane.
- **Outcomes** — instead of a `user.message`, send `user.define_outcome` with a gradeable rubric; a grader runs an iterate → grade → revise loop until satisfied / `max_iterations`.
- **Multiagent** — `multiagent: {type:"coordinator", agents:[...]}` on the coordinator agent; sub-agents run in isolated threads sharing the container.
- **Self-hosted sandboxes** — `config: {type:"self_hosted"}` keeps the loop on Anthropic but runs tools in your infra via an outbound-polling `EnvironmentWorker` / `ant beta:worker poll`.
- **Memory stores, GitHub repos, file mounts, webhooks, permission policies** — all session/agent resources.

## Sources (official docs)

- Overview — https://platform.claude.com/docs/en/managed-agents/overview.md
- Quickstart — https://platform.claude.com/docs/en/managed-agents/quickstart.md
- Agent setup — https://platform.claude.com/docs/en/managed-agents/agent-setup.md
- Environments — https://platform.claude.com/docs/en/managed-agents/environments.md
- Tools — https://platform.claude.com/docs/en/managed-agents/tools.md
- Events & streaming — https://platform.claude.com/docs/en/managed-agents/events-and-streaming.md
- Outcomes — https://platform.claude.com/docs/en/managed-agents/define-outcomes.md
- Multi-agent — https://platform.claude.com/docs/en/managed-agents/multi-agent.md
- Self-hosted sandboxes — https://platform.claude.com/docs/en/managed-agents/self-hosted-sandboxes.md
- Webhooks — https://platform.claude.com/docs/en/managed-agents/webhooks.md

> Tip: in Claude Code, the bundled `/claude-api` skill carries a deep, always-current Managed Agents reference (per-language SDK bindings, client patterns, full endpoint table). Reach for it when you need a binding or wire-level detail not covered here.
