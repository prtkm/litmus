"""`python -m litmus.pipeline <pdf|claimgraph.json>` — run the local audit pipeline end to end.

A ``.json`` input is loaded as a ClaimGraph (no API call); a ``.pdf`` is extracted with Opus
first (DESIGN §11) then audited. Writes a schema-valid audit report to
``study/corpus/audits/<paper_id>.json`` and prints a summary (DESIGN §13, §14).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from litmus.commons.registry import build_default_registry
from litmus.core import schema
from litmus.core.claim import ClaimGraph
from litmus.pipeline.executor import LocalExecutor


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="litmus.pipeline", description="Run the local audit pipeline.")
    ap.add_argument("input", help="a .pdf (extract+audit) or a claim-graph .json (audit only)")
    ap.add_argument("--out", help="where to write the audit report JSON")
    ap.add_argument("--no-confirm", action="store_true", help="skip fresh-context confirmation")
    ap.add_argument("--paper-id", help="override paper_id (PDF mode)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.exists():
        print(f"no such file: {src}", file=sys.stderr)
        return 2

    registry = build_default_registry()
    executor = LocalExecutor(confirm=not args.no_confirm)

    if src.suffix.lower() == ".json":
        graph = ClaimGraph.from_dict(json.loads(src.read_text()))
        report = executor.audit_graph(graph, registry)
    else:
        report = executor.audit_pdf(str(src), registry, paper_id=args.paper_id)

    report_dict = report.to_dict()
    errors = schema.validate(report_dict, "audit")
    if errors:
        print("WARNING: audit report failed schema validation:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)

    out = Path(args.out) if args.out else Path("study/corpus/audits") / f"{report.paper_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report_dict, indent=2))

    s = report.summary()
    print(f"audit: {report.paper_id}")
    print(f"  claims={report.meta.get('n_claims')}  verifiers={report.meta.get('n_verifiers')}")
    print(f"  CHECKABLE flags={s['n_flags']}  (by tier: {s['flags_by_trust_tier']}, severity: {s['flags_by_severity']})")
    print(f"  dropped (self-caught FPs)={s['n_dropped']}  routed_to_human={s['n_routed_to_human']}  abstained={s['n_abstained']}")
    print(f"  synthesis candidates={len(report.meta.get('synthesis_candidates', []))}")
    print(f"  -> {out}  (schema-valid: {not errors})")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
