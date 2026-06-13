"""The verifier contribution SDK — ``litmus verifier new`` / ``litmus verifier test`` (DESIGN §9).

This is the developer-facing half of the commons. A contributor in any field gets two moves,
both built on the *same* machinery the rest of LITMUS uses:

  * :func:`scaffold_verifier` (``litmus verifier new <id>``) — generate a ready-to-edit verifier
        module (manifest + ``judge`` stub + ``self_test`` stub + a ``VERIFIERS`` export) plus a
        Markdown docs stub, from a single template. The output is immediately importable and
        auto-discoverable; the contributor fills in the two methods.
  * :func:`test_verifier` (``litmus verifier test <id|path>``) — load a verifier *by registered id
        or by ``.py`` path* and run it through the calibration kernel (DESIGN §7), printing its own
        scorecard so the author sees SCORING-vs-ADVISORY-vs-REJECTED **before** they submit.

The point of the gate (DESIGN §8): a contributed verifier earns trust by passing the *same*
seeded-error kernel as every first-party one — not by who wrote it. ``test_verifier`` is how a
contributor runs that gate locally.

The registry's entry-point seam (``discover_entry_points``) is the *other* half — how a finished
out-of-tree verifier ships like a pytest plugin. This module does not touch it; it operates on a
single verifier the contributor is actively authoring.
"""

from __future__ import annotations

import importlib.util
import keyword
import re
import sys
from pathlib import Path

from litmus.core.calibration import Scorecard, calibrate
from litmus.core.finding import VerifierKind
from litmus.core.verifier import Determinism, Verifier

# The scaffold template lives next to this module under templates/ (DESIGN §9, §15).
_TEMPLATE_PACKAGE = "litmus.commons.templates"
_TEMPLATE_NAME = "verifier_template.py"

# kind string (CLI-facing) -> the VerifierKind member to stamp into the manifest.
_KIND_BY_NAME: dict[str, VerifierKind] = {k.value: k for k in VerifierKind}

# A freshly-scaffolded verifier's determinism follows from its kind: an assisted verifier
# (DESIGN §6.1 class D) is ASSISTED, a synthesized one SYNTHESIZED, everything else
# DETERMINISTIC (the honest default for the T0 recompute core).
_DETERMINISM_BY_KIND: dict[VerifierKind, Determinism] = {
    VerifierKind.PREBUILT: Determinism.DETERMINISTIC,
    VerifierKind.TEMPLATED: Determinism.DETERMINISTIC,
    VerifierKind.SYNTHESIZED: Determinism.SYNTHESIZED,
    VerifierKind.ASSISTED: Determinism.ASSISTED,
}

_VALID_TIERS = {f"T{i}" for i in range(9)}  # T0..T8 (DESIGN §5)


# =============================================================================
# scaffold  (litmus verifier new <id>)
# =============================================================================
def _module_stem(verifier_id: str) -> str:
    """Derive a filesystem-safe, importable module stem from a verifier id.

    ``ph_bounds.v1`` -> ``ph_bounds`` (a module holds one verifier; the version lives in the
    manifest, not the filename). An id without a ``.vN`` suffix is sanitized whole. The result is
    always a valid, non-keyword Python identifier so the file imports.
    """
    head = verifier_id.split(".", 1)[0] if re.search(r"\.v\d+$", verifier_id) else verifier_id
    stem = re.sub(r"[^0-9A-Za-z_]", "_", head).strip("_") or "verifier"
    if stem[0].isdigit():
        stem = f"v_{stem}"
    if keyword.iskeyword(stem):
        stem = f"{stem}_"
    return stem


def _class_name(verifier_id: str) -> str:
    """Derive a CamelCase class name from a verifier id (``ph_bounds.v1`` -> ``PhBounds``)."""
    head = verifier_id.split(".", 1)[0] if re.search(r"\.v\d+$", verifier_id) else verifier_id
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", head) if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts) or "Verifier"
    if name[0].isdigit():
        name = f"V{name}"
    if keyword.iskeyword(name):
        name = f"{name}Verifier"
    return name


def _version_from_id(verifier_id: str) -> str:
    """``ph_bounds.v1`` -> ``1.0``; ``ph_bounds.v3`` -> ``3.0``; no suffix -> ``1.0``."""
    m = re.search(r"\.v(\d+)$", verifier_id)
    return f"{m.group(1)}.0" if m else "1.0"


def _load_template() -> str:
    """Read the scaffold template source, via importlib.resources (works under the wheel)."""
    try:
        from importlib import resources

        return resources.files(_TEMPLATE_PACKAGE).joinpath(_TEMPLATE_NAME).read_text(encoding="utf-8")
    except Exception:
        # Fallback for any environment where the resource API can't see the package data.
        return (Path(__file__).parent / "templates" / _TEMPLATE_NAME).read_text(encoding="utf-8")


def _docs_stub(verifier_id: str, class_name: str, tier: str, kind: str, author: str) -> str:
    """The Markdown docs stub that ships next to a scaffolded verifier (DESIGN §9)."""
    return (
        f"# `{verifier_id}`\n"
        "\n"
        f"- **Class:** `{class_name}`\n"
        f"- **Tier:** {tier}\n"
        f"- **Kind:** {kind}\n"
        f"- **Author(s):** {author}\n"
        "\n"
        "## What it checks\n"
        "\n"
        "TODO: one paragraph — the invariant this verifier enforces and the claim/record types it\n"
        "`consumes` (DESIGN §12).\n"
        "\n"
        "## Evidence contract\n"
        "\n"
        "TODO: the `extracted_values` shape `judge` binds to (the keys it reads off its evidence,\n"
        "DESIGN §11).\n"
        "\n"
        "## Verdict\n"
        "\n"
        "- **PASS** — TODO\n"
        "- **FAIL** (severity A/B/C) — TODO; ships a stdlib-only `recompute_script` that reprints\n"
        "  the discrepancy (DESIGN §3.2: no script, no flag).\n"
        "- **ABSTAIN** — TODO; when the evidence can't be bound (DESIGN §3.4: abstain > guess).\n"
        "\n"
        "## Calibration\n"
        "\n"
        "`self_test` ships clean + planted instances. Run the kernel locally before submitting:\n"
        "\n"
        "```\n"
        f"litmus verifier test {verifier_id}\n"
        "```\n"
        "\n"
        "Admitted **SCORING** only when recall ≥ 0.90, FPR ≤ the declared ceiling (overall and\n"
        "per claim_type), every flag reproduces, and `judge` is deterministic (DESIGN §7).\n"
    )


def scaffold_verifier(
    verifier_id: str,
    dest_dir: str | Path,
    tier: str = "T0",
    kind: str = "prebuilt",
    *,
    author: str = "TODO: Your Name",
    provenance: str = "contributed",
    overwrite: bool = False,
) -> Path:
    """Generate a new verifier module + docs stub from the template (``litmus verifier new``).

    Writes ``<dest_dir>/<stem>.py`` (a manifest + ``judge`` stub + ``self_test`` stub + a
    module-level ``VERIFIERS`` export) and ``<dest_dir>/<stem>.md`` (a docs stub). The generated
    module is immediately importable and exposes ``VERIFIERS``, so the registry's auto-discovery /
    entry-point seam can pick it up once the author fills in the logic.

    Args:
        verifier_id: the manifest id, e.g. ``ph_bounds.v1``.
        dest_dir: directory to write into (created if missing).
        tier: epistemic tier ``T0``..``T8`` (DESIGN §5).
        kind: verifier kind — one of ``prebuilt|templated|synthesized|assisted`` (DESIGN §6.1).

    Returns:
        The :class:`~pathlib.Path` to the written ``.py`` module.
    """
    if not verifier_id or not verifier_id.strip():
        raise ValueError("verifier id must be a non-empty string")
    tier = tier.upper()
    if tier not in _VALID_TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {sorted(_VALID_TIERS)}")
    kind_key = kind.lower()
    if kind_key not in _KIND_BY_NAME:
        raise ValueError(
            f"unknown kind {kind!r}; expected one of {sorted(_KIND_BY_NAME)}"
        )
    kind_enum = _KIND_BY_NAME[kind_key]
    determinism_enum = _DETERMINISM_BY_KIND[kind_enum]

    stem = _module_stem(verifier_id)
    class_name = _class_name(verifier_id)
    version = _version_from_id(verifier_id)

    source = _load_template()
    substitutions = {
        "__VERIFIER_ID__": verifier_id,
        "__VERIFIER_CLASS__": class_name,
        "__VERIFIER_VERSION__": version,
        "__VERIFIER_KIND__": kind_enum.name,  # enum member name, e.g. PREBUILT
        "__VERIFIER_TIER__": tier,            # EpistemicTier member name, e.g. T0
        "__VERIFIER_DETERMINISM__": determinism_enum.name,
        "__VERIFIER_CONSUME__": stem,         # a sensible default routing key to edit
        "__VERIFIER_AUTHOR__": author,
        "__VERIFIER_PROVENANCE__": provenance,
    }
    for token, value in substitutions.items():
        source = source.replace(token, value)

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    py_path = dest / f"{stem}.py"
    md_path = dest / f"{stem}.md"
    if py_path.exists() and not overwrite:
        raise FileExistsError(f"{py_path} already exists (pass overwrite=True to replace)")

    py_path.write_text(source, encoding="utf-8")
    md_path.write_text(
        _docs_stub(verifier_id, class_name, tier, kind_enum.value, author),
        encoding="utf-8",
    )
    return py_path


# =============================================================================
# test  (litmus verifier test <id|path>)
# =============================================================================
def _load_verifier_from_path(path: Path) -> Verifier:
    """Import a ``.py`` file by path and return the Verifier it defines (DESIGN §9).

    Resolution order: a module-level ``VERIFIERS`` list (the export the registry consumes — take
    its first instance), else a single concrete :class:`Verifier` subclass defined in the module
    (instantiated). Raises ``ValueError`` if neither is found.
    """
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"no such verifier file: {path}")

    mod_name = f"_litmus_contrib_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:
        raise ValueError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses / relative lookups behave during import.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise

    # Preferred: the VERIFIERS export the registry's auto-discovery uses.
    vlist = getattr(module, "VERIFIERS", None)
    if vlist:
        for v in vlist:
            if isinstance(v, Verifier):
                return v

    # Fallback: a single concrete Verifier subclass defined *in this module*.
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
        raise ValueError(
            f"{path} defines {len(candidates)} Verifier subclasses and no usable VERIFIERS "
            "export; expose a module-level VERIFIERS = [TheVerifier()] to disambiguate"
        )
    raise ValueError(
        f"{path} exposes no Verifier: define a module-level VERIFIERS = [TheVerifier()] "
        "or a single Verifier subclass"
    )


def _looks_like_path(target: str) -> bool:
    """Heuristic: treat the target as a filesystem path if it ends in .py or points at a file."""
    if target.endswith(".py"):
        return True
    return Path(target).is_file()


def load_verifier(target: str | Path | Verifier) -> Verifier:
    """Resolve ``target`` to a Verifier instance: a registered id, a ``.py`` path, or an instance.

    A registered id is looked up in the default registry (first-party + entry-point discovered).
    A path is imported in isolation (DESIGN §9). Used by :func:`test_verifier` and the CLI.
    """
    if isinstance(target, Verifier):
        return target
    if isinstance(target, Path):
        return _load_verifier_from_path(target)
    if _looks_like_path(target):
        return _load_verifier_from_path(Path(target))

    # Otherwise: a registered verifier id.
    from litmus.commons.registry import build_default_registry

    registry = build_default_registry()
    try:
        return registry.get(target)
    except KeyError:
        known = ", ".join(registry.ids()) or "(none)"
        raise ValueError(
            f"no verifier registered as {target!r}, and it is not a .py path. "
            f"Known ids: {known}"
        )


def test_verifier(
    target: str | Path | Verifier,
    *,
    print_report: bool = True,
    file=None,
) -> Scorecard:
    """Calibrate a verifier (by id or ``.py`` path) and return its Scorecard (``litmus verifier test``).

    Loads ``target``, runs the full calibration gate (DESIGN §7), and — when ``print_report`` —
    prints the verifier's one-line scorecard plus the deciding reason(s), so a contributor sees
    SCORING / ADVISORY / REJECTED locally before submitting. Returns the :class:`Scorecard`.
    """
    out = file if file is not None else sys.stdout
    verifier = load_verifier(target)
    card = calibrate(verifier)
    if print_report:
        print(card.summary_line(), file=out)
        for reason in card.reasons:
            print(f"    {reason}", file=out)
    return card
