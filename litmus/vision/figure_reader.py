"""Read numeric values off a figure with Opus 4.8 vision (DESIGN §5 frontier, §19 WS-E).

The *reading* half of the figure frontier. A plot — a bar chart, a curve, a panel with error bars —
is sent to Opus as a base64 image block, and the model is forced through a single
``emit_figure_values`` tool to return the numbers it reads off the axes. Those numbers then feed a
deterministic T0/T2 check (``figure_vs_table.v1`` / the recompute core), collapsing "read numbers
off a figure" into a checkable comparison (DESIGN §5). The model only *reads*; it never renders a
verdict (DESIGN §3.1) — that stays in deterministic verifier code.

This mirrors ``litmus.extract.extractor``'s Anthropic conventions: a forced single tool call for
structured output, streaming with ``get_final_message()`` for timeout robustness, an injectable
client for tests, and a clear error if the key/SDK is missing. It is intentionally *not* a verifier
(no manifest, no calibration) — it is an upstream evidence-extraction step, like the PDF extractor.

Usage::

    from litmus.vision import read_figure_values
    vals = read_figure_values("fig2.png", "Read the height of each bar in Figure 2.")
    # -> {"values": {"control": 50.0, "treatment": 68.0}, "notes": "..."}
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = "claude-opus-4-8"
TOOL_NAME = "emit_figure_values"

# Image media types Opus vision accepts, keyed by file suffix (DESIGN §5: high-DPI figure reading).
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# The structured-output contract: the model returns a flat map of label -> number, plus optional
# free-text notes (e.g. "y-axis is log-scaled", "left bar partially occluded"). Numbers only — the
# model transcribes what it reads, it does not judge (DESIGN §3.1).
_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "values": {
            "type": "object",
            "description": (
                "Map of a short label for each value read off the figure (e.g. a series/condition "
                "name, or 'bar_1') to its numeric value on the plotted axis. Read as precisely as "
                "the axis allows. Include only values you can actually read; omit anything unclear."
            ),
            "additionalProperties": {"type": "number"},
        },
        "notes": {
            "type": "string",
            "description": (
                "Optional caveats about the reading: axis scale (log/linear), truncated or dual "
                "axes, occlusion, units, or any value you could not read. Empty if none."
            ),
        },
    },
    "required": ["values"],
}


def _media_type_for(image_path: str | os.PathLike[str]) -> str:
    suffix = Path(image_path).suffix.lower()
    media = _MEDIA_TYPES.get(suffix)
    if media is None:
        raise FigureReadError(
            f"unsupported image type {suffix!r} for {image_path}; "
            f"expected one of {sorted(_MEDIA_TYPES)}"
        )
    return media


def _image_block(image_path: str | os.PathLike[str]) -> dict[str, Any]:
    """A base64 image content block so Opus reads the figure natively (DESIGN §5)."""
    data = Path(image_path).read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _media_type_for(image_path),
            "data": b64,
        },
    }


def _get(obj: Any, key: str) -> Any:
    """Attribute-or-key accessor (SDK objects expose attributes; fakes/dicts use keys).

    Kept permissive — like the extractor — so unit tests can feed a hand-built fake response
    through the same parse path without importing SDK types.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_tool_input(message: Any) -> dict[str, Any]:
    """Pull the single ``emit_figure_values`` tool-use input out of a Messages response."""
    content = _get(message, "content")
    if content is None:
        raise FigureReadError("model response had no content blocks")
    for block in content:
        if _get(block, "type") == "tool_use" and _get(block, "name") == TOOL_NAME:
            tool_input = _get(block, "input")
            if not isinstance(tool_input, dict):
                raise FigureReadError(
                    f"{TOOL_NAME} tool input was not an object: {type(tool_input)!r}"
                )
            return tool_input
    stop = _get(message, "stop_reason")
    raise FigureReadError(
        f"model did not call {TOOL_NAME} (stop_reason={stop!r}); no figure values produced"
    )


def _coerce_values(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize the raw tool input into ``{'values': {label: number}, 'notes': str}``.

    Drops any non-numeric (or boolean) entries the model might have slipped into ``values`` — the
    downstream verifiers (``figure_vs_table.v1``) only consume real numbers, and a verifier never
    invents a value (DESIGN §3.1). Always returns a ``values`` dict (possibly empty) and a string
    ``notes`` so callers have a stable shape.
    """
    raw_values = tool_input.get("values")
    values: dict[str, float] = {}
    if isinstance(raw_values, dict):
        for label, val in raw_values.items():
            if isinstance(val, bool):  # bool is an int subclass; not a real reading
                continue
            if isinstance(val, (int, float)):
                values[str(label)] = float(val)
    notes = tool_input.get("notes")
    return {"values": values, "notes": str(notes) if notes is not None else ""}


def read_figure_values(
    image_path: str,
    instruction: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
    client: Any = None,
) -> dict[str, Any]:
    """Read numeric values off a figure image with Opus 4.8 vision (DESIGN §5, §19 WS-E).

    Sends the image as a base64 block plus ``instruction`` (what to read — e.g. "read each bar
    height in Figure 2"), and forces a single ``emit_figure_values`` tool call (``tool_choice``
    pinned to the tool) whose ``input_schema`` is a flat ``{label: number}`` map. Returns a dict::

        {"values": {<label>: <float>, ...}, "notes": "<caveats or ''>"}

    The returned numbers are the raw material a deterministic verifier (``figure_vs_table.v1``)
    compares against the table — this step only reads, it never judges (DESIGN §3.1).

    Args:
        image_path: path to the figure image (PNG/JPEG/GIF/WebP).
        instruction: what to read off the figure, in plain language.
        model: Claude model id (default ``claude-opus-4-8``).
        max_tokens: output cap for the read.
        client: an Anthropic client (injectable for tests); created on demand otherwise.

    Raises:
        FileNotFoundError: if ``image_path`` does not exist.
        FigureReadError: if the SDK/key is missing or the model returns no figure values.
    """
    img = Path(image_path)
    if not img.is_file():
        raise FileNotFoundError(f"figure image not found: {image_path}")

    if client is None:
        client = _make_client()

    tool = {
        "name": TOOL_NAME,
        "description": (
            "Emit the numeric values you read off this figure. Transcribe what is plotted as "
            "precisely as the axes allow; never guess a value you cannot read, and never judge "
            "whether the figure is correct — only report the numbers."
        ),
        "input_schema": _TOOL_SCHEMA,
    }

    user_content = [
        _image_block(image_path),
        {
            "type": "text",
            "text": (
                f"{instruction}\n\nCall {TOOL_NAME} exactly once with the values you can read off "
                "the figure. Read each value as precisely as the axis allows. Record any caveat "
                "(axis scale, truncation, occlusion, units) in 'notes'. Do not invent values."
            ),
        },
    ]

    request_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        tools=[tool],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=[{"role": "user", "content": user_content}],
    )
    message = _stream_with_retry(client, request_kwargs)
    tool_input = _extract_tool_input(message)
    return _coerce_values(tool_input)


def _stream_with_retry(client: Any, request_kwargs: dict[str, Any], *, max_attempts: int = 4) -> Any:
    """Stream the read request, retrying transient connection drops.

    Streaming (with ``get_final_message()``) avoids the SDK's long-request timeout guard for the
    image payload (claude-api skill guidance). The SDK auto-retries the *initial* request but not a
    mid-stream drop; we catch ``APIConnectionError`` / 5xx / overload, back off, and re-send (the
    read is idempotent). Non-retryable 4xx propagate immediately.

    NOTE: ``thinking`` is intentionally absent — Opus 4.8 rejects adaptive thinking when
    ``tool_choice`` forces a specific tool, and forcing ``emit_figure_values`` is how we guarantee
    structured output.
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
    raise FigureReadError(
        f"figure read stream failed after {max_attempts} attempts: {last_exc}"
    ) from last_exc


def _make_client() -> Any:
    """Construct an Anthropic client, with a clear error if the key/SDK is missing."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - dependency declared in pyproject
        raise FigureReadError(
            "the 'anthropic' package is required for figure reading (pip install anthropic)"
        ) from e
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise FigureReadError(
            "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is not set; cannot call the API"
        )
    return anthropic.Anthropic()


class FigureReadError(RuntimeError):
    """Raised when reading values off a figure fails to produce a usable result."""
