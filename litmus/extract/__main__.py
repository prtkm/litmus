"""CLI: ``python -m litmus.extract <pdf> [--out path] [--no-cache]``.

Extracts a ClaimGraph from a PDF (Opus 4.8 native PDF ingestion, DESIGN §11), writes it as
JSON to the canonical corpus location (``study/corpus/claims/<paper_id>.json`` by default,
DESIGN §10), and prints a short summary: n claims / evidence / bindings and a tier histogram.
By default the API call is skipped if the output already exists (use ``--no-cache`` to force).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from litmus.core.claim import ClaimGraph

from .extractor import (
    DEFAULT_MODEL,
    ExtractionError,
    default_out_path,
    default_paper_id,
    extract_to_file,
)


def _tier_histogram(graph: ClaimGraph) -> dict[str, int]:
    """Count claims per proposed epistemic tier (untiered claims -> 'none')."""
    counts: Counter[str] = Counter()
    for c in graph.claims:
        counts[c.epistemic_tier.value if c.epistemic_tier else "none"] += 1
    # Deterministic order: T0..T8 then 'none', only for tiers that occur.
    order = [f"T{i}" for i in range(9)] + ["none"]
    return {t: counts[t] for t in order if counts[t]}


def summarize(graph: ClaimGraph, out: Path, *, from_cache: bool) -> str:
    """Render the one-screen summary printed after extraction."""
    n_bound = sum(1 for c in graph.claims if graph.evidence_for(c))
    hist = _tier_histogram(graph)
    hist_str = "  ".join(f"{t}:{n}" for t, n in hist.items()) or "(none)"
    src = "cache" if from_cache else "extracted"
    lines = [
        f"paper_id : {graph.paper_id}   [{src}]",
        f"output   : {out}",
        f"claims   : {len(graph.claims)}  ({n_bound} with bound evidence)",
        f"evidence : {len(graph.evidence)}",
        f"bindings : {len(graph.bindings)}",
        f"tiers    : {hist_str}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m litmus.extract",
        description="Extract a schema-valid ClaimGraph from a paper PDF (Opus 4.8, DESIGN §11).",
    )
    parser.add_argument("pdf", help="path to the source PDF")
    parser.add_argument(
        "--out",
        default=None,
        help="output JSON path (default: study/corpus/claims/<paper_id>.json)",
    )
    parser.add_argument(
        "--paper-id",
        default=None,
        help="paper_id stamped on the graph (default: the PDF filename stem)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model id (default {DEFAULT_MODEL})")
    parser.add_argument(
        "--max-tokens", type=int, default=16000, help="output token cap (default 16000)"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="re-extract even if the output file already exists",
    )
    args = parser.parse_args(argv)

    pid = args.paper_id or default_paper_id(args.pdf)
    out = Path(args.out) if args.out else default_out_path(pid)
    will_use_cache = (not args.no_cache) and out.is_file()

    try:
        graph, out_path = extract_to_file(
            args.pdf,
            out_path=args.out,
            model=args.model,
            paper_id=args.paper_id,
            max_tokens=args.max_tokens,
            use_cache=not args.no_cache,
        )
    except (ExtractionError, FileNotFoundError) as e:
        print(f"extraction failed: {e}", file=sys.stderr)
        return 1

    print(summarize(graph, out_path, from_cache=will_use_cache))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
