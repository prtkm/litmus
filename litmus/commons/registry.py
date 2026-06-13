"""The verifier registry (DESIGN §9, §12).

A process-local catalog of every available :class:`~litmus.core.verifier.Verifier`. The
planner queries it to route a claim to the verifiers that ``consume`` its type; the system
scorecard (``verify.py``) iterates it to calibrate the whole library.

Two population paths, both designed in from day one (DESIGN §9):

  * **first-party** — in-tree verifier modules, registered by :func:`build_default_registry`.
  * **out-of-tree** — independent packages discovered via ``importlib.metadata`` entry points
        in group ``litmus.verifiers`` (like a pytest plugin). We ship in-tree first; the
        entry-point seam means out-of-tree works later with no redesign.

A broken or missing plugin must never crash discovery — third-party code is untrusted
(DESIGN §9, §13). Each entry point is loaded under ``try/except`` and a load failure is
recorded, not raised.
"""

from __future__ import annotations

import importlib
import pkgutil
from importlib import metadata
from typing import Any, Iterable, Optional

from litmus.core.verifier import Verifier

ENTRY_POINT_GROUP = "litmus.verifiers"


class Registry:
    """An ordered, id-keyed catalog of verifiers (DESIGN §9, §12)."""

    def __init__(self) -> None:
        self._by_id: dict[str, Verifier] = {}
        # Diagnostics: entry-point loads that failed, so a broken plugin is visible
        # without taking the process down (DESIGN §9).
        self.load_errors: list[tuple[str, str]] = []

    # --- registration --------------------------------------------------------
    def register(self, verifier: Verifier, *, replace: bool = False) -> Verifier:
        """Register a verifier instance, keyed by its manifest id.

        Raises ``ValueError`` on a duplicate id unless ``replace=True``. Validates that the
        object actually carries a manifest with an id (a verifier is self-describing,
        DESIGN §6.3).
        """
        manifest = getattr(verifier, "manifest", None)
        vid = getattr(manifest, "id", None)
        if not vid:
            raise ValueError(
                f"cannot register {verifier!r}: missing manifest.id (DESIGN §6.3: "
                "every verifier is a self-describing package)"
            )
        if vid in self._by_id and not replace:
            raise ValueError(f"duplicate verifier id {vid!r} already registered")
        self._by_id[vid] = verifier
        return verifier

    def register_all(self, verifiers: Iterable[Verifier], *, replace: bool = False) -> None:
        for v in verifiers:
            self.register(v, replace=replace)

    # --- lookup --------------------------------------------------------------
    def get(self, verifier_id: str) -> Verifier:
        """Return the verifier with this id, or raise ``KeyError``."""
        return self._by_id[verifier_id]

    def __contains__(self, verifier_id: object) -> bool:
        return verifier_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def all(self) -> list[Verifier]:
        """All registered verifiers, in registration order."""
        return list(self._by_id.values())

    def ids(self) -> list[str]:
        """All registered verifier ids, in registration order."""
        return list(self._by_id.keys())

    def for_claim_type(self, claim_type: str) -> list[Verifier]:
        """Verifiers whose manifest ``consumes`` this claim_type / record_type (DESIGN §12).

        This is the routing primitive: the planner asks the registry which verifiers can
        even look at a claim of a given type before it spends any work on it.
        """
        return [v for v in self._by_id.values() if claim_type in v.manifest.consumes]

    # --- out-of-tree discovery (DESIGN §9) -----------------------------------
    def discover_entry_points(self, *, replace: bool = False) -> list[Verifier]:
        """Load external verifiers advertised under the ``litmus.verifiers`` entry-point group.

        Each entry point is expected to resolve to either a single :class:`Verifier` instance
        or an iterable of them (or a zero-arg callable returning one of those). Anything that
        raises while loading, returns the wrong type, or duplicates an id is skipped and
        recorded in :attr:`load_errors` — a broken third-party plugin must not crash the host
        (DESIGN §9, §13).

        Returns the list of verifiers it actually registered.
        """
        registered: list[Verifier] = []
        for ep in _iter_entry_points(ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
            except Exception as exc:  # untrusted import; never propagate
                self.load_errors.append((ep.name, f"load failed: {type(exc).__name__}: {exc}"))
                continue
            try:
                produced = _coerce_to_verifiers(obj)
            except Exception as exc:
                self.load_errors.append((ep.name, f"factory raised: {type(exc).__name__}: {exc}"))
                continue
            if not produced:
                self.load_errors.append((ep.name, "produced no Verifier instances"))
                continue
            for v in produced:
                try:
                    self.register(v, replace=replace)
                    registered.append(v)
                except Exception as exc:
                    vid = getattr(getattr(v, "manifest", None), "id", "?")
                    self.load_errors.append((ep.name, f"register {vid!r} failed: {exc}"))
        return registered


def _iter_entry_points(group: str) -> Iterable[Any]:
    """Yield entry points for ``group`` across importlib.metadata API variants.

    Python 3.12's ``entry_points`` returns a selectable ``EntryPoints``; older shapes returned
    a dict. We tolerate both so the seam is portable. Never raises — a metadata read failure
    just yields nothing.
    """
    try:
        eps = metadata.entry_points()
    except Exception:
        return []
    # 3.10+ selectable API
    select = getattr(eps, "select", None)
    if callable(select):
        try:
            return list(select(group=group))
        except Exception:
            return []
    # legacy dict API
    if isinstance(eps, dict):
        return list(eps.get(group, []))
    return []


def _coerce_to_verifiers(obj: Any) -> list[Verifier]:
    """Normalize an entry-point payload into a list of Verifier instances.

    Accepts: a Verifier, an iterable of Verifiers, or a zero-arg callable returning either.
    Non-Verifier items are dropped (with the caller recording the miss).
    """
    # A class or factory: call it to get an instance / list.
    if isinstance(obj, type) or (callable(obj) and not isinstance(obj, Verifier)):
        obj = obj()
    if isinstance(obj, Verifier):
        return [obj]
    out: list[Verifier] = []
    if isinstance(obj, (list, tuple, set)):
        for item in obj:
            if isinstance(item, Verifier):
                out.append(item)
    return out


def _first_party_verifiers(
    load_errors: Optional[list[tuple[str, str]]] = None,
) -> list[Verifier]:
    """Auto-discover in-tree verifiers by scanning the ``litmus.verifiers`` package (DESIGN §9, §19 WS-D).

    Each verifier module exposes a module-level ``VERIFIERS`` list of Verifier instances. Modules
    are imported under ``try/except`` so one broken or half-written module never takes down the
    whole library (the same resilience as the entry-point seam). A new first-party verifier is
    added simply by dropping a file that defines ``VERIFIERS`` — no edit here. This is what lets
    the verifier library be authored in parallel without contention on shared code.
    """
    import litmus.verifiers as pkg

    found: list[Verifier] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        name = mod_info.name
        if name.startswith("_"):
            continue
        full = f"{pkg.__name__}.{name}"
        try:
            module = importlib.import_module(full)
        except Exception as exc:  # a half-written sibling module must not break discovery
            if load_errors is not None:
                load_errors.append((full, f"import failed: {type(exc).__name__}: {exc}"))
            continue
        vlist = getattr(module, "VERIFIERS", None)
        if not vlist:
            continue
        for v in vlist:
            if isinstance(v, Verifier):
                found.append(v)
            elif load_errors is not None:
                load_errors.append((full, f"VERIFIERS held a non-Verifier {type(v).__name__}"))
    return found


def build_default_registry(*, discover: bool = True) -> Registry:
    """Build the registry the rest of LITMUS uses (DESIGN §9).

    Auto-discovers the first-party verifiers (any ``litmus.verifiers`` module exposing
    ``VERIFIERS``), then (unless ``discover=False``) loads out-of-tree verifiers advertised via
    entry points. A duplicate id or a broken module is recorded in ``registry.load_errors``,
    never raised — the library stays up.
    """
    registry = Registry()
    for v in _first_party_verifiers(registry.load_errors):
        try:
            registry.register(v)
        except Exception as exc:
            vid = getattr(getattr(v, "manifest", None), "id", "?")
            registry.load_errors.append((vid, f"register failed: {exc}"))
    if discover:
        registry.discover_entry_points()
    return registry
