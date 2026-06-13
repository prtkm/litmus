"""PDF -> schema-valid ClaimGraph via Opus 4.8 native PDF ingestion (DESIGN §11, §19).

The only model-in-the-loop step for *finding* things. Opus reads the PDF natively
(text + figures + tables, via a base64 ``document`` content block) and is forced to emit
a ClaimGraph through a single ``emit_claim_graph`` tool. The tool result is parsed into a
``ClaimGraph`` and asserted schema-valid before it is returned or cached.

Caching (DESIGN §10): the ClaimGraph JSON is the canonical artifact. If the output file
already exists, ``extract_claim_graph`` returns it without an API call unless ``use_cache``
is False.
"""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
from typing import Any, Optional

from litmus.core import schema
from litmus.core.claim import ClaimGraph

from .prompts import EXTRACTION_SYSTEM_PROMPT

DEFAULT_MODEL = "claude-opus-4-8"
TOOL_NAME = "emit_claim_graph"

# Repo root -> default corpus location for extracted ClaimGraphs (DESIGN §10).
# litmus/extract/extractor.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLAIMS_DIR = _REPO_ROOT / "study" / "corpus" / "claims"


def default_paper_id(pdf_path: str | os.PathLike[str]) -> str:
    """Derive a stable paper_id from the PDF filename (its stem)."""
    return Path(pdf_path).stem


def default_out_path(paper_id: str) -> Path:
    """Where a ClaimGraph for ``paper_id`` is cached by default."""
    return DEFAULT_CLAIMS_DIR / f"{paper_id}.json"


def _relax_additional_properties(node: Any) -> Any:
    """Return a deep copy of a JSON-schema node with every ``additionalProperties: false``
    flipped to ``true``.

    The published claim schema is strict (``additionalProperties: false`` everywhere). The
    Anthropic tool ``input_schema`` validator can be over-strict about such schemas (and the
    model may legitimately add keys we then drop); we relax it ONLY for the tool boundary so a
    near-miss isn't rejected before we can validate it ourselves. The RESULT is still asserted
    against the strict ``schema.validate(..., "claim")`` — relaxing here never weakens the
    real contract.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "additionalProperties" and v is False:
                out[k] = True
            else:
                out[k] = _relax_additional_properties(v)
        return out
    if isinstance(node, list):
        return [_relax_additional_properties(v) for v in node]
    return node


def _tool_input_schema() -> dict[str, Any]:
    """The claim-graph JSON schema, relaxed for use as a tool ``input_schema``.

    We strip the JSON-Schema meta keys ($schema/$id) the tool validator doesn't want, keep
    $defs/$ref (Anthropic supports them), and relax additionalProperties (see above).
    """
    base = copy.deepcopy(schema.load_schema("claim"))
    base.pop("$schema", None)
    base.pop("$id", None)
    return _relax_additional_properties(base)


def _pdf_document_block(pdf_path: str | os.PathLike[str]) -> dict[str, Any]:
    """A base64 PDF ``document`` content block so Opus reads text+figures+tables natively."""
    data = Path(pdf_path).read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        },
    }


def _extract_tool_input(message: Any) -> dict[str, Any]:
    """Pull the single ``emit_claim_graph`` tool-use input out of a Messages response.

    ``message`` is an Anthropic ``Message`` (has ``.content`` blocks) or any object/dict that
    looks like one — kept permissive so unit tests can feed a hand-built fake through the
    same parse path without importing SDK types.
    """
    content = _get(message, "content")
    if content is None:
        raise ExtractionError("model response had no content blocks")
    for block in content:
        if _get(block, "type") == "tool_use" and _get(block, "name") == TOOL_NAME:
            tool_input = _get(block, "input")
            if not isinstance(tool_input, dict):
                raise ExtractionError(
                    f"{TOOL_NAME} tool input was not an object: {type(tool_input)!r}"
                )
            return tool_input
    stop = _get(message, "stop_reason")
    raise ExtractionError(
        f"model did not call {TOOL_NAME} (stop_reason={stop!r}); no ClaimGraph produced"
    )


def _get(obj: Any, key: str) -> Any:
    """Attribute-or-key accessor (SDK objects expose attributes; fakes/dicts use keys)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def build_claim_graph(
    tool_input: dict[str, Any],
    *,
    paper_id: str,
    model: str = DEFAULT_MODEL,
    meta: Optional[dict[str, Any]] = None,
) -> ClaimGraph:
    """Parse a raw ``emit_claim_graph`` tool input into a schema-valid ``ClaimGraph``.

    Sets ``paper_id`` and stamps provenance into ``meta``. Asserts the result passes
    ``schema.validate(graph.to_dict(), "claim")`` (raises ``ExtractionError`` with the errors
    if not). This is the shared parse path for both the live API call and the unit tests.

    The model is not asked to emit ``paper_id`` (we own it), so inject it before
    ``ClaimGraph.from_dict`` — which requires the key — rather than relying on a later set.
    """
    raw = dict(tool_input)
    raw["paper_id"] = paper_id
    graph = ClaimGraph.from_dict(raw)
    graph.paper_id = paper_id

    provenance: dict[str, Any] = {
        "extractor": "litmus.extract",
        "model": model,
        "source": "opus-native-pdf",
    }
    provenance.update(meta or {})
    # Preserve any meta the model emitted, but our provenance wins on conflict.
    merged = dict(graph.meta or {})
    merged.update(provenance)
    graph.meta = merged

    errors = schema.validate(graph.to_dict(), "claim")
    if errors:
        raise ExtractionError(
            "extracted ClaimGraph failed schema validation:\n  " + "\n  ".join(errors)
        )
    return graph


def extract_claim_graph(
    pdf_path: str,
    *,
    model: str = DEFAULT_MODEL,
    paper_id: Optional[str] = None,
    max_tokens: int = 16000,
    client: Any = None,
) -> ClaimGraph:
    """Extract a schema-valid ``ClaimGraph`` from a PDF via Opus 4.8 native PDF ingestion.

    Reads the PDF, sends it as a base64 ``document`` block, and forces a single
    ``emit_claim_graph`` tool call (``tool_choice`` pinned to the tool) whose ``input_schema``
    is the claim-graph schema. Parses the tool input -> ``ClaimGraph``, sets ``paper_id``, and
    asserts schema validity before returning.

    This always calls the API. For the cached / write-to-disk path, use ``extract_to_file``
    or ``python -m litmus.extract`` (DESIGN §10 cache).

    Args:
        pdf_path: path to the source PDF.
        model: Claude model id (default ``claude-opus-4-8``).
        paper_id: id stamped on the graph; defaults to the PDF filename stem.
        max_tokens: output cap for the extraction call.
        client: an Anthropic client (injectable for tests); created on demand otherwise.
    """
    pid = paper_id or default_paper_id(pdf_path)
    pdf = Path(pdf_path)
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if client is None:
        client = _make_client()

    tool = {
        "name": TOOL_NAME,
        "description": (
            "Emit the structured ClaimGraph for this paper: every checkable claim "
            "(with proposed epistemic_tier, operationalized predicate, strength, scope, "
            "evidence_refs, confidence), every piece of evidence it rests on (with exact "
            "extracted_values and a location), and the claim->evidence bindings. Transcribe "
            "and locate only; never judge; record null for any missing value."
        ),
        "input_schema": _tool_input_schema(),
    }

    user_content = [
        _pdf_document_block(pdf_path),
        {
            "type": "text",
            "text": (
                f"Extract the ClaimGraph for this paper (paper_id: {pid}). Call "
                f"{TOOL_NAME} exactly once with the complete graph. Remember: transcribe and "
                "locate only — never judge correctness, never invent a missing value (use "
                "null), and every quote must be a verbatim substring of the paper."
            ),
        },
    ]

    request_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=EXTRACTION_SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": user_content}],
    )
    message = _stream_with_retry(client, request_kwargs)
    tool_input = _extract_tool_input(message)
    return build_claim_graph(tool_input, paper_id=pid, model=model)


def _stream_with_retry(client: Any, request_kwargs: dict[str, Any], *, max_attempts: int = 4) -> Any:
    """Stream the extraction request, retrying transient connection drops.

    Large input (a whole PDF) + a sizeable max_tokens -> stream to avoid the SDK's
    non-streaming long-request guard / HTTP timeouts (claude-api skill guidance). The SDK
    auto-retries the *initial* request but NOT a drop that occurs mid-stream — long PDF
    extractions occasionally hit ``APIConnectionError`` (httpx ``RemoteProtocolError``:
    "peer closed connection ... incomplete chunked read") or a transient 5xx/overload partway
    through. We catch those, back off, and re-send (the call is idempotent — a fresh extraction).
    Non-retryable errors (400/401/404, e.g. the forced-tool/thinking conflict) propagate
    immediately.

    NOTE: ``thinking`` is intentionally absent from ``request_kwargs`` — Opus 4.8 rejects adaptive
    thinking when ``tool_choice`` forces a specific tool, and forcing ``emit_claim_graph`` is how
    we guarantee structured output.
    """
    import time

    import anthropic

    retryable = (anthropic.APIConnectionError, anthropic.InternalServerError, anthropic.RateLimitError)
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with client.messages.stream(**request_kwargs) as stream:
                return stream.get_final_message()
        except anthropic.APIStatusError as e:
            # Retry only on overloaded/5xx; surface client errors (4xx) right away.
            if e.status_code in (429, 529) or e.status_code >= 500:
                last_exc = e
            else:
                raise
        except retryable as e:
            last_exc = e
        if attempt < max_attempts - 1:
            time.sleep(min(2.0 * (2**attempt), 30.0))
    raise ExtractionError(
        f"extraction stream failed after {max_attempts} attempts: {last_exc}"
    ) from last_exc


def extract_to_file(
    pdf_path: str,
    *,
    out_path: Optional[str | os.PathLike[str]] = None,
    model: str = DEFAULT_MODEL,
    paper_id: Optional[str] = None,
    max_tokens: int = 16000,
    use_cache: bool = True,
    client: Any = None,
) -> tuple[ClaimGraph, Path]:
    """Extract a ClaimGraph and persist it as JSON (the canonical store, DESIGN §10).

    If ``out_path`` exists and ``use_cache`` is True, the cached graph is loaded and returned
    WITHOUT an API call. Otherwise the PDF is extracted, written to ``out_path``, and returned.

    Returns ``(graph, out_path)``.
    """
    pid = paper_id or default_paper_id(pdf_path)
    out = Path(out_path) if out_path is not None else default_out_path(pid)

    if use_cache and out.is_file():
        cached = json.loads(out.read_text(encoding="utf-8"))
        graph = ClaimGraph.from_dict(cached)
        errors = schema.validate(graph.to_dict(), "claim")
        if errors:
            raise ExtractionError(
                f"cached ClaimGraph at {out} failed schema validation:\n  "
                + "\n  ".join(errors)
            )
        return graph, out

    graph = extract_claim_graph(
        pdf_path, model=model, paper_id=pid, max_tokens=max_tokens, client=client
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return graph, out


def _make_client() -> Any:
    """Construct an Anthropic client, with a clear error if the key/SDK is missing."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - dependency declared in pyproject
        raise ExtractionError(
            "the 'anthropic' package is required for extraction (pip install anthropic)"
        ) from e
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ExtractionError(
            "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is not set; cannot call the API"
        )
    return anthropic.Anthropic()


class ExtractionError(RuntimeError):
    """Raised when extraction fails to produce a schema-valid ClaimGraph."""
