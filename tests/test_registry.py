"""The verifier registry: registration, routing, and resilient entry-point discovery (DESIGN §9, §12).

The registry is how the planner finds verifiers for a claim type, and how out-of-tree plugins
join the library. A broken plugin must never crash discovery (DESIGN §9, §13).
"""

from __future__ import annotations

import pytest

from litmus.commons import build_default_registry
from litmus.commons.registry import Registry
from litmus.core.claim import EpistemicTier
from litmus.core.finding import Status, VerifierKind
from litmus.core.verifier import Determinism, Verifier, VerifierManifest
from litmus.verifiers.sum_check import SumCheck


def _verifier(vid: str, consumes: list[str]) -> Verifier:
    class _V(Verifier):
        manifest = VerifierManifest(
            id=vid,
            version="1.0",
            kind=VerifierKind.PREBUILT,
            epistemic_tier=EpistemicTier.T0,
            determinism=Determinism.DETERMINISTIC,
            consumes=consumes,
        )

        def judge(self, claim, evidence):
            return self.make_finding(claim=claim, status=Status.PASS)

        def self_test(self):
            return []

    return _V()


# --- registration + lookup ---------------------------------------------------
def test_register_and_get():
    reg = Registry()
    v = _verifier("a.v1", ["x"])
    reg.register(v)
    assert reg.get("a.v1") is v
    assert "a.v1" in reg
    assert len(reg) == 1
    assert reg.ids() == ["a.v1"]
    assert reg.all() == [v]


def test_duplicate_id_raises_unless_replace():
    reg = Registry()
    reg.register(_verifier("a.v1", ["x"]))
    with pytest.raises(ValueError):
        reg.register(_verifier("a.v1", ["y"]))
    # replace=True overwrites
    v2 = _verifier("a.v1", ["z"])
    reg.register(v2, replace=True)
    assert reg.get("a.v1") is v2


def test_register_rejects_missing_manifest_id():
    reg = Registry()

    class Bare:
        manifest = None

    with pytest.raises(ValueError):
        reg.register(Bare())  # type: ignore[arg-type]


def test_get_missing_raises_keyerror():
    reg = Registry()
    with pytest.raises(KeyError):
        reg.get("nope")


# --- routing (DESIGN §12) ----------------------------------------------------
def test_for_claim_type_matches_consumes():
    reg = Registry()
    a = _verifier("a.v1", ["table_total", "sum_claim"])
    b = _verifier("b.v1", ["p_value"])
    reg.register_all([a, b])
    assert reg.for_claim_type("table_total") == [a]
    assert reg.for_claim_type("p_value") == [b]
    assert reg.for_claim_type("unknown") == []


def test_for_claim_type_returns_multiple():
    reg = Registry()
    a = _verifier("a.v1", ["table_total"])
    b = _verifier("b.v1", ["table_total"])
    reg.register_all([a, b])
    assert set(v.manifest.id for v in reg.for_claim_type("table_total")) == {"a.v1", "b.v1"}


# --- default registry --------------------------------------------------------
def test_build_default_registry_has_sum_check():
    reg = build_default_registry()
    assert "sum_check.v1" in reg
    assert isinstance(reg.get("sum_check.v1"), SumCheck)
    # sum_check routes for both of its declared claim types.
    assert reg.get("sum_check.v1") in reg.for_claim_type("table_total")
    assert reg.get("sum_check.v1") in reg.for_claim_type("sum_claim")


def test_build_default_registry_no_discover_still_has_first_party():
    reg = build_default_registry(discover=False)
    assert "sum_check.v1" in reg


# --- entry-point discovery resilience (DESIGN §9) ----------------------------
def test_discover_tolerates_no_entry_points():
    """With no plugins installed, discovery returns [] and records no errors."""
    reg = Registry()
    discovered = reg.discover_entry_points()
    assert discovered == []
    assert reg.load_errors == []


def test_discover_handles_broken_plugin(monkeypatch):
    """A plugin that raises on load is recorded, not propagated (DESIGN §9, §13)."""
    import litmus.commons.registry as registry_mod

    class _BadEP:
        name = "broken"

        def load(self):
            raise RuntimeError("plugin import blew up")

    class _GoodEP:
        name = "good"

        def load(self):
            return _verifier("plugin.v1", ["x"])

    monkeypatch.setattr(registry_mod, "_iter_entry_points", lambda group: [_BadEP(), _GoodEP()])
    reg = Registry()
    discovered = reg.discover_entry_points()
    # The good plugin loaded; the bad one was caught and logged.
    assert [v.manifest.id for v in discovered] == ["plugin.v1"]
    assert "plugin.v1" in reg
    assert any(name == "broken" for name, _ in reg.load_errors)


def test_discover_accepts_list_and_factory(monkeypatch):
    """An entry point may resolve to a Verifier, a list of them, or a zero-arg factory."""
    import litmus.commons.registry as registry_mod

    class _ListEP:
        name = "many"

        def load(self):
            return [_verifier("p1.v1", ["x"]), _verifier("p2.v1", ["y"])]

    class _FactoryEP:
        name = "factory"

        def load(self):
            return lambda: _verifier("p3.v1", ["z"])

    monkeypatch.setattr(registry_mod, "_iter_entry_points", lambda group: [_ListEP(), _FactoryEP()])
    reg = Registry()
    discovered = reg.discover_entry_points()
    assert {v.manifest.id for v in discovered} == {"p1.v1", "p2.v1", "p3.v1"}


def test_discover_records_wrong_type(monkeypatch):
    """An entry point that yields a non-Verifier produces a recorded error, not a crash."""
    import litmus.commons.registry as registry_mod

    class _JunkEP:
        name = "junk"

        def load(self):
            return "not a verifier"

    monkeypatch.setattr(registry_mod, "_iter_entry_points", lambda group: [_JunkEP()])
    reg = Registry()
    discovered = reg.discover_entry_points()
    assert discovered == []
    assert any(name == "junk" for name, _ in reg.load_errors)
