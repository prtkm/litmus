"""The synthesis loop (DESIGN §8): propose -> sandbox+determinism -> calibrate -> admit.

``propose_verifier`` forces Opus 4.8 to emit the Python source of a ``Verifier`` subclass
that matches the litmus contract exactly (a synthesized-kind manifest, a pure deterministic
``judge`` that ships a stdlib byte-exact ``recompute_script`` on every FAIL, and a
``self_test`` with >=5 clean + >=5 planted). ``materialize`` first runs that source through
the network-less recompute sandbox (DESIGN §15) with a tiny harness — construct + run one
self_test case — to catch a crash / non-termination / network attempt in isolation BEFORE we
import attacker-influenced code into this process, then imports it and returns the Verifier.
``synthesize`` then calibrates it through the SAME kernel as everything else (DESIGN §7) and
admits it as ``CALIBRATED_SYNTHESIZED`` only if the kernel says SCORING or ADVISORY; a
non-deterministic / self-test-less / non-reproducing proposal is REJECTED — it never scores.

The model proposes; the GATE disposes (DESIGN §8: "The trust comes from the gate, not the LLM.").
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from litmus.core.calibration import AdmissionStatus, Scorecard, calibrate
from litmus.core.verifier import Verifier

DEFAULT_MODEL = "claude-opus-4-8"
TOOL_NAME = "emit_verifier"

# The sandbox vetting harness gets this long to construct the verifier + run one self_test
# case in an isolated subprocess. Short, because a healthy proposal does this in well under a
# second; a proposal that hangs (non-terminating judge / blocking network call) is exactly
# what we want this ceiling to kill (DESIGN §8 sandbox + determinism check, §15).
SANDBOX_VET_TIMEOUT_S = 20.0


class SynthesisError(RuntimeError):
    """Raised when synthesis cannot even produce a loadable verifier (before the gate)."""


# =============================================================================
# 1. propose  — ask Opus to WRITE a bespoke verifier (forced tool / structured output)
# =============================================================================
def _worked_example() -> str:
    """The real ``sum_check.v1`` source, handed to the model verbatim as the contract by example."""
    path = Path(__file__).resolve().parents[1] / "verifiers" / "sum_check.py"
    return path.read_text(encoding="utf-8")


_SYSTEM_PROMPT = """\
You are LITMUS's verifier synthesizer (DESIGN §8). When a paper makes a bespoke, checkable \
quantitative claim that no existing verifier covers, you WRITE a brand-new deterministic \
verifier for it — as Python source — and nothing else.

The trust in your verifier comes from the calibration GATE it must pass (DESIGN §7), not from \
you. So your only job is to write a verifier that is honest, pure, deterministic, and ships \
executable evidence. If it isn't, the gate rejects it and your work is wasted.

NON-NEGOTIABLE CONTRACT (the gate checks every one of these empirically):

1. The module defines exactly one concrete subclass of `litmus.core.verifier.Verifier`, sets \
its `manifest`, and implements `judge(self, claim, evidence) -> Finding` and \
`self_test(self) -> list[SelfTestCase]`. It ends with `VERIFIERS = [TheClass()]`.

2. The manifest is a `VerifierManifest` with `kind=VerifierKind.SYNTHESIZED` and \
`determinism=Determinism.SYNTHESIZED`. Pick a sensible `epistemic_tier` (usually T0 for pure \
arithmetic on the paper's own numbers), a stable `id` ending in `.v1`, a `consumes` routing \
key, and an honest `fpr_ceiling` (0.05 is normal).

3. `judge` MUST be PURE and DETERMINISTIC — no `random`, no `time`/`datetime`, no `os`/env, no \
network, no file I/O, no global mutable state. Given the same claim+evidence it must return a \
byte-identical Finding every call. The gate runs it N times and rejects any variation (G4).

4. `judge` reads its numbers off `evidence[i].extracted_values` (a dict). If the evidence it \
needs isn't present, it returns `self.abstain(claim, "...")` — never guesses (abstain > guess).

5. On a real violation, `judge` returns a FAIL via `self.make_finding(..., status=Status.FAIL, \
severity=Severity.A|B|C, evidence=packet)`. The packet's `recompute_script` MUST be a \
self-contained, STDLIB-ONLY, network-less, DETERMINISTIC Python program that hardcodes the \
relevant numbers, recomputes the metric, and prints EXACTLY the `expected_output` string \
(byte-for-byte) — and nothing else. The gate runs the script twice in a sandbox and rejects \
the verifier unless its stdout reproduces `expected_output` and is identical across runs (G3). \
A clean (PASS) instance returns status=PASS with no script needed.

   CRITICAL byte-exactness: the live verdict's `expected_output` and the script's printed line \
must be produced by the SAME formatting logic, so they agree byte-for-byte. The simplest \
robust pattern: compute the canonical output line as a string in `judge`, embed that exact \
string as a literal inside the generated script via `print(<repr>)`, and set \
`expected_output` to the same string. Avoid float formatting drift — round/format identically \
on both sides.

6. `self_test` returns AT LEAST 5 clean (correct, must PASS) and AT LEAST 5 planted (a known \
error injected, must FAIL) `SelfTestCase`s, built from FIXED hand-written numbers (no RNG). \
Span >=2 `claim_type` values across the cases so per-claim-type FPR (G6) is exercised. Each \
case carries a `Claim` and a list with one `Evidence` whose `extracted_values` holds exactly \
the keys `judge` reads. Make the planted cases violate the metric by a clear margin and the \
clean cases satisfy it exactly, so recall is 1.0 and FPR is 0.0.

Imports you will need (use these real symbols only):
    from litmus.core.claim import Claim, Evidence, EvidenceKind, EpistemicTier, Location
    from litmus.core.finding import EvidencePacket, Finding, Severity, Status, VerifierKind
    from litmus.core.verifier import Determinism, SelfTestCase, Verifier, VerifierManifest

Below is the COMPLETE source of `sum_check.v1`, a real first-party verifier that passes the \
gate. Mirror its structure exactly — the `_fmt`/byte-exact-script discipline, the abstain \
path, the manifest shape, the fixed self_test specs with asserts — adapting the *metric* to \
the claim you are given. Output ONLY via the `emit_verifier` tool.

================ WORKED EXAMPLE: litmus/verifiers/sum_check.py ================
{worked_example}
================ END WORKED EXAMPLE ================
"""


def _emit_tool() -> dict[str, Any]:
    """The forced tool: a structured envelope around the verifier source (DESIGN §11 pattern)."""
    return {
        "name": TOOL_NAME,
        "description": (
            "Emit the synthesized verifier: a short strategy note, the manifest fields you "
            "chose, and the COMPLETE Python source of the Verifier subclass module "
            "(ending in VERIFIERS = [TheClass()]). The source must satisfy the litmus "
            "contract exactly so it passes the calibration gate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {
                    "type": "string",
                    "description": "One or two sentences: the checkable metric and how judge recomputes it.",
                },
                "manifest_fields": {
                    "type": "object",
                    "description": "The key manifest choices, for provenance.",
                    "properties": {
                        "id": {"type": "string"},
                        "version": {"type": "string"},
                        "epistemic_tier": {"type": "string"},
                        "consumes": {"type": "array", "items": {"type": "string"}},
                        "fpr_ceiling": {"type": "number"},
                        "description": {"type": "string"},
                    },
                    "required": ["id", "consumes"],
                    "additionalProperties": True,
                },
                "judge_src": {
                    "type": "string",
                    "description": (
                        "The COMPLETE module source: imports + the Verifier subclass with "
                        "manifest, judge, self_test + the module-level VERIFIERS export. This "
                        "is the whole .py file; self_test_src may repeat or be empty."
                    ),
                },
                "self_test_src": {
                    "type": "string",
                    "description": (
                        "Optional. If you put the whole module in judge_src (recommended), "
                        "leave this empty. Provided only as a redundant copy of the self_test."
                    ),
                },
            },
            "required": ["strategy", "manifest_fields", "judge_src"],
            "additionalProperties": False,
        },
    }


def _get(obj: Any, key: str) -> Any:
    """Attribute-or-key accessor (SDK objects expose attributes; fakes/dicts use keys)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_tool_input(message: Any) -> dict[str, Any]:
    """Pull the single ``emit_verifier`` tool-use input out of a Messages response."""
    content = _get(message, "content")
    if content is None:
        raise SynthesisError("model response had no content blocks")
    for block in content:
        if _get(block, "type") == "tool_use" and _get(block, "name") == TOOL_NAME:
            tool_input = _get(block, "input")
            if not isinstance(tool_input, dict):
                raise SynthesisError(f"{TOOL_NAME} input was not an object: {type(tool_input)!r}")
            return tool_input
    stop = _get(message, "stop_reason")
    raise SynthesisError(f"model did not call {TOOL_NAME} (stop_reason={stop!r})")


def propose_verifier(
    claim_description: str,
    evidence_example: Any,
    *,
    model: str = DEFAULT_MODEL,
    client: Any = None,
    max_tokens: int = 8000,
) -> dict[str, Any]:
    """Ask Opus to WRITE a bespoke verifier for ``claim_description`` (DESIGN §8 propose step).

    Forces a single ``emit_verifier`` tool call (so the output is structured) and hands the
    model ``sum_check.py`` as the worked example. Returns a dict with keys ``strategy``,
    ``manifest_fields``, ``judge_src``, ``self_test_src`` — the proposed verifier SOURCE, not
    yet vetted or trusted. ``materialize`` + ``synthesize`` apply the gate.

    Args:
        claim_description: the bespoke checkable claim (metric + the discrepancy to flag).
        evidence_example: an example of the evidence shape ``judge`` will bind to (dict or
            ``extracted_values``-like) — included verbatim so the model targets the real keys.
        model: Claude model id (default ``claude-opus-4-8``).
        client: an Anthropic client (injectable for tests); created on demand otherwise.
    """
    if client is None:
        client = _make_client()

    system = _SYSTEM_PROMPT.format(worked_example=_worked_example())
    user_text = (
        "Synthesize a verifier for this bespoke, checkable claim.\n\n"
        f"CLAIM / METRIC TO CHECK:\n{claim_description}\n\n"
        f"EXAMPLE EVIDENCE (the extracted_values shape judge will bind to):\n{evidence_example!r}\n\n"
        f"Call {TOOL_NAME} exactly once with the complete verifier module source. Remember: "
        "pure deterministic judge, byte-exact stdlib recompute_script on every FAIL, and a "
        "self_test with >=5 clean + >=5 planted across >=2 claim_types so it passes the gate."
    )

    request_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=[_emit_tool()],
        # Force the tool so we always get structured source. NOTE: no `thinking` — Opus 4.8
        # rejects adaptive thinking when tool_choice forces a specific tool (claude-api skill).
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": user_text}],
    )
    message = _stream_with_retry(client, request_kwargs)
    tool_input = _extract_tool_input(message)

    src = tool_input.get("judge_src") or ""
    if not src.strip():
        raise SynthesisError(f"{TOOL_NAME} returned empty judge_src")
    return {
        "strategy": tool_input.get("strategy", ""),
        "manifest_fields": tool_input.get("manifest_fields") or {},
        "judge_src": src,
        "self_test_src": tool_input.get("self_test_src") or "",
    }


def _stream_with_retry(client: Any, request_kwargs: dict[str, Any], *, max_attempts: int = 4) -> Any:
    """Stream the proposal request, retrying transient connection drops (mirrors extract)."""
    import anthropic

    retryable = (anthropic.APIConnectionError, anthropic.InternalServerError, anthropic.RateLimitError)
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with client.messages.stream(**request_kwargs) as stream:
                return stream.get_final_message()
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 529) or e.status_code >= 500:
                last_exc = e
            else:
                raise
        except retryable as e:
            last_exc = e
        if attempt < max_attempts - 1:
            time.sleep(min(2.0 * (2**attempt), 30.0))
    raise SynthesisError(f"synthesis stream failed after {max_attempts} attempts: {last_exc}")


def _make_client() -> Any:
    """Construct an Anthropic client, with a clear error if the key/SDK is missing."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - dependency declared in pyproject
        raise SynthesisError(
            "the 'anthropic' package is required for synthesis (pip install anthropic)"
        ) from e
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise SynthesisError(
            "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is not set; cannot call the API"
        )
    return anthropic.Anthropic()


# =============================================================================
# 2. materialize  — sandbox-vet the source, THEN import it into a Verifier
# =============================================================================
# A tiny self-contained harness, prepended to the proposed source and run in the network-less
# recompute sandbox (DESIGN §8, §15). It imports nothing of ours that isn't on the path of the
# proposal itself: it just constructs the verifier and runs ONE self_test case end-to-end. If
# the proposal crashes on import, loops forever, or reaches for the network, this dies in an
# isolated subprocess and we never import the code into THIS process. It prints LITMUS_SBX_OK
# on success so we can distinguish "ran clean" from "ran but produced no signal".
_VET_HARNESS = '''

# --- LITMUS sandbox vetting harness (DESIGN §8) — appended by the synthesizer ---------------
if __name__ == "__main__":
    import sys as _sys
    _vs = globals().get("VERIFIERS")
    if _vs:
        _v = _vs[0]
    else:
        from litmus.core.verifier import Verifier as _V
        _cands = [o for o in list(globals().values())
                  if isinstance(o, type) and issubclass(o, _V) and o is not _V
                  and o.__module__ == "__main__"]
        if len(_cands) != 1:
            print("LITMUS_SBX_ERR no single Verifier / VERIFIERS export", file=_sys.stderr)
            _sys.exit(3)
        _v = _cands[0]()
    _cases = list(_v.self_test())
    if not _cases:
        print("LITMUS_SBX_ERR self_test produced no cases", file=_sys.stderr)
        _sys.exit(4)
    _f = _v.judge(_cases[0].claim, _cases[0].evidence)
    _ = _f.to_dict()  # exercise the Finding contract
    print("LITMUS_SBX_OK")
'''


def _sandbox_vet(src: str, *, timeout_s: float = SANDBOX_VET_TIMEOUT_S) -> None:
    """Run ``src`` + a construct/one-judge harness in the recompute sandbox (DESIGN §8, §15).

    The whole point is isolation BEFORE import: a malicious or broken proposal that crashes,
    hangs, or touches the network is contained in a separate, resource-limited, network-less
    subprocess. Raises ``SynthesisError`` if it doesn't terminate cleanly with the OK marker.
    """
    from litmus.core import sandbox

    result = sandbox.run_script(src + _VET_HARNESS, timeout_s=timeout_s)
    if result.timed_out:
        raise SynthesisError(
            "proposed verifier did not terminate in the sandbox (possible infinite loop / "
            "blocked network call) — rejected before import (DESIGN §8, §15)"
        )
    if not result.ok or "LITMUS_SBX_OK" not in result.stdout:
        raise SynthesisError(
            "proposed verifier failed sandbox vetting before import "
            f"(rc={result.returncode}): {result.stderr.strip()[:600]}"
        )


def materialize(src: str, *, vet: bool = True, vet_timeout_s: float = SANDBOX_VET_TIMEOUT_S) -> Verifier:
    """Turn proposed source into a live ``Verifier`` — sandbox-vetting it FIRST (DESIGN §8).

    Safety (DESIGN §8 "sandbox + determinism check", §15): before importing attacker-influenced
    code into THIS process, ``materialize`` runs the source through the network-less recompute
    sandbox with a tiny harness (construct the verifier + run one self_test case) in an isolated
    subprocess — so a crash, non-termination, or network attempt is caught in isolation. Only
    then does it write the source to a temp module, import it, and return ``VERIFIERS[0]`` (or
    the single ``Verifier`` subclass defined in the module).

    Pass ``vet=False`` only in trusted unit tests that deliberately exercise the import path
    (e.g. feeding a known-nonterminating-free but non-deterministic source straight to the
    calibrator); production synthesis always vets.
    """
    if vet:
        _sandbox_vet(src, timeout_s=vet_timeout_s)

    with tempfile.TemporaryDirectory(prefix="litmus-synth-") as tmp_name:
        mod_name = f"_litmus_synth_{uuid.uuid4().hex}"
        path = Path(tmp_name) / f"{mod_name}.py"
        path.write_text(src, encoding="utf-8")

        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise SynthesisError(f"could not build an import spec for synthesized module {mod_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(mod_name, None)
            raise SynthesisError(f"synthesized module failed to import: {type(exc).__name__}: {exc}") from exc

    # Preferred: the VERIFIERS export the registry's auto-discovery consumes (DESIGN §9).
    vlist = getattr(module, "VERIFIERS", None)
    if vlist:
        for v in vlist:
            if isinstance(v, Verifier):
                return v

    # Fallback: a single concrete Verifier subclass defined in this module.
    candidates = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Verifier)
        and obj is not Verifier
        and getattr(obj, "__module__", None) == mod_name
    ]
    if len(candidates) == 1:
        return candidates[0]()
    if len(candidates) > 1:
        raise SynthesisError(
            f"synthesized module defines {len(candidates)} Verifier subclasses and no usable "
            "VERIFIERS export; cannot disambiguate"
        )
    raise SynthesisError(
        "synthesized module exposes no Verifier (expected VERIFIERS = [TheClass()] or a single "
        "Verifier subclass)"
    )


# =============================================================================
# 3. synthesize  — propose -> materialize -> calibrate -> admit | reject (DESIGN §8)
# =============================================================================
def synthesize(
    claim_description: str,
    evidence_example: Any,
    *,
    model: str = DEFAULT_MODEL,
    client: Any = None,
    src: Optional[str] = None,
) -> dict[str, Any]:
    """Run the full §8 loop and return ``{verifier, scorecard, admission, strategy, ...}``.

    propose -> materialize (sandbox-vet + import) -> calibrate through the SAME kernel as every
    other verifier (DESIGN §7). The verifier is admitted as ``CALIBRATED_SYNTHESIZED`` iff the
    kernel says SCORING or ADVISORY; it is REJECTED (``admission='rejected'``) if it is
    non-deterministic (G4), ships a flag that doesn't reproduce (G3), or has no self_test —
    exactly the kernel's hard-rejection invariants. The trust is the gate, not the model.

    Args:
        claim_description / evidence_example: passed to :func:`propose_verifier`.
        src: a pre-obtained module source to use instead of calling the API (for tests / replay).

    Returns a dict:
        ``verifier``   -> the materialized Verifier (None if it never loaded),
        ``scorecard``  -> its Scorecard (None if it never loaded),
        ``admission``  -> 'calibrated_synthesized' | 'advisory' | 'rejected',
        ``kernel_admission`` -> the raw kernel AdmissionStatus value,
        ``strategy`` / ``manifest_fields`` / ``proposed_src`` -> provenance,
        ``reason``     -> a one-line human summary.
    """
    proposal: dict[str, Any]
    if src is not None:
        proposal = {"strategy": "", "manifest_fields": {}, "judge_src": src, "self_test_src": ""}
    else:
        proposal = propose_verifier(claim_description, evidence_example, model=model, client=client)

    base: dict[str, Any] = {
        "verifier": None,
        "scorecard": None,
        "admission": "rejected",
        "kernel_admission": None,
        "strategy": proposal.get("strategy", ""),
        "manifest_fields": proposal.get("manifest_fields", {}),
        "proposed_src": proposal["judge_src"],
        "reason": "",
    }

    # propose -> materialize (sandbox-vet + import). A proposal that can't even load — or that
    # fails sandbox vetting (crash / non-termination / network) — is a hard reject (DESIGN §8).
    try:
        verifier = materialize(proposal["judge_src"])
    except SynthesisError as exc:
        base["reason"] = f"rejected: did not materialize ({exc})"
        return base
    base["verifier"] = verifier

    # ... -> the SAME calibration kernel as everything else (DESIGN §7, §8).
    card: Scorecard = calibrate(verifier)
    base["scorecard"] = card
    base["kernel_admission"] = card.admission.value

    if card.admission is AdmissionStatus.REJECTED:
        base["admission"] = "rejected"
        base["reason"] = "rejected by kernel: " + ("; ".join(card.reasons) or "hard invariant failed")
        return base

    # SCORING or ADVISORY -> admitted as a calibrated-synthesized verifier. ADVISORY keeps the
    # 'advisory' status (surfaces flags, never an A/B verdict); SCORING earns the full tier.
    if card.admission is AdmissionStatus.SCORING:
        base["admission"] = "calibrated_synthesized"
        base["reason"] = f"admitted CALIBRATED_SYNTHESIZED (scoring): {card.summary_line()}"
    else:  # ADVISORY
        base["admission"] = "advisory"
        base["reason"] = f"admitted advisory only: {card.summary_line()}"
    return base
