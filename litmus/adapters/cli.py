"""The ``litmus`` command-line entry point (DESIGN §15, §19).

A thin argparse front end over the framework. It will grow (audit a paper, list capabilities,
run the discovery study); today it exposes:

  * ``litmus verify [--json] [--strict]``  — delegate to the system calibration scorecard.
  * ``litmus verifier list``               — list every registered verifier (id, tier, kind).
  * ``litmus verifier new <id> [--dir]``   — scaffold a new verifier (the commons SDK, DESIGN §9).
  * ``litmus verifier test <id|path>``     — calibrate one verifier locally; exit 0 iff SCORING.

Wired as the ``litmus`` console script in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from litmus import verify as verify_module
from litmus.commons.registry import build_default_registry
from litmus.commons.sdk import scaffold_verifier, test_verifier
from litmus.core.calibration import AdmissionStatus


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


def _cmd_verifier_new(args: argparse.Namespace) -> int:
    """Scaffold a new verifier module + docs stub (DESIGN §9: ``litmus verifier new``)."""
    try:
        path = scaffold_verifier(
            args.id,
            args.dir,
            tier=args.tier,
            kind=args.kind,
            overwrite=args.force,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    docs = path.with_suffix(".md")
    print(f"scaffolded {args.id}:")
    print(f"  module: {path}")
    print(f"  docs:   {docs}")
    print(f"\nNext: implement judge() + self_test(), then `litmus verifier test {path}`.")
    return 0


def _cmd_verifier_test(args: argparse.Namespace) -> int:
    """Calibrate one verifier by id or path; exit 0 iff admitted SCORING (DESIGN §9, §7)."""
    try:
        card = test_verifier(args.target)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a contributor's module that fails to import is a usage error, not a crash
        print(f"error: failed to load verifier {args.target!r}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0 if card.admission is AdmissionStatus.SCORING else 1


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
    p_verifier = sub.add_parser("verifier", help="inspect, scaffold, and calibrate verifiers")
    verifier_sub = p_verifier.add_subparsers(dest="verifier_command", required=True)

    p_list = verifier_sub.add_parser("list", help="list registered verifiers (id, tier, kind)")
    p_list.set_defaults(func=_cmd_verifier_list)

    # litmus verifier new <id> [--dir] [--tier] [--kind] [--force]
    p_new = verifier_sub.add_parser(
        "new",
        help="scaffold a new verifier (manifest + judge/self_test stubs + docs) — DESIGN §9",
    )
    p_new.add_argument("id", help="verifier id, e.g. ph_bounds.v1")
    p_new.add_argument(
        "--dir",
        default="examples/contrib",
        help="directory to write the new verifier into (default: examples/contrib)",
    )
    p_new.add_argument("--tier", default="T0", help="epistemic tier T0..T8 (default: T0)")
    p_new.add_argument(
        "--kind",
        default="prebuilt",
        choices=["prebuilt", "templated", "synthesized", "assisted"],
        help="verifier kind (default: prebuilt)",
    )
    p_new.add_argument(
        "--force", action="store_true", help="overwrite an existing module of the same name"
    )
    p_new.set_defaults(func=_cmd_verifier_new)

    # litmus verifier test <id|path>
    p_test = verifier_sub.add_parser(
        "test",
        help="run the calibration kernel on one verifier (by id or .py path); exit 0 iff SCORING",
    )
    p_test.add_argument("target", help="a registered verifier id, or a path to a verifier .py file")
    p_test.set_defaults(func=_cmd_verifier_test)

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
