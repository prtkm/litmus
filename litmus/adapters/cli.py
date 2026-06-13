"""The ``litmus`` command-line entry point (DESIGN §15, §19).

A thin argparse front end over the framework. It will grow (audit a paper, list capabilities,
run the discovery study); for WS-A it exposes the two things the gate needs:

  * ``litmus verify [--json] [--strict]``  — delegate to the system calibration scorecard.
  * ``litmus verifier list``               — list every registered verifier (id, tier, kind).

Wired as the ``litmus`` console script in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from litmus import verify as verify_module
from litmus.commons.registry import build_default_registry


def _cmd_verifier_list(_args: argparse.Namespace) -> int:
    """Print id / tier / kind for every registered verifier (first-party + discovered)."""
    registry = build_default_registry()
    verifiers = registry.all()
    if not verifiers:
        print("(no verifiers registered)")
        return 0
    print(f"{'ID':<28} {'TIER':<5} {'KIND'}")
    for v in verifiers:
        m = v.manifest
        print(f"{m.id:<28} {m.epistemic_tier.value:<5} {m.kind.value}")
    if registry.load_errors:
        print("\nplugin load errors:", file=sys.stderr)
        for name, err in registry.load_errors:
            print(f"  {name}: {err}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litmus",
        description="LITMUS — a self-extending auditor for the scientific literature (DESIGN §1).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # litmus verify [--json] [--strict]
    p_verify = sub.add_parser(
        "verify",
        help="run the calibration gate over every registered verifier (the system scorecard)",
    )
    p_verify.add_argument("--json", action="store_true", help="emit scorecards as a JSON list")
    p_verify.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless every registered verifier is admitted SCORING",
    )
    p_verify.set_defaults(func=_cmd_verify)

    # litmus verifier <subcommand>
    p_verifier = sub.add_parser("verifier", help="inspect the verifier library")
    verifier_sub = p_verifier.add_subparsers(dest="verifier_command", required=True)
    p_list = verifier_sub.add_parser("list", help="list registered verifiers (id, tier, kind)")
    p_list.set_defaults(func=_cmd_verifier_list)

    return parser


def _cmd_verify(args: argparse.Namespace) -> int:
    """Delegate to litmus.verify.main, forwarding the parsed flags."""
    forwarded: list[str] = []
    if getattr(args, "json", False):
        forwarded.append("--json")
    if getattr(args, "strict", False):
        forwarded.append("--strict")
    return verify_module.main(forwarded)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
