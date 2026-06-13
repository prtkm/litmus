"""WS-C — the verifier commons SDK + CLI (DESIGN §9).

The contribution half of the commons, held to the gate the rest of LITMUS uses:

  * ``scaffold_verifier`` (``litmus verifier new``) writes an immediately-importable verifier
    module exposing ``VERIFIERS`` plus a docs stub.
  * ``test_verifier`` (``litmus verifier test``) calibrates a verifier by registered id OR by
    ``.py`` path and returns its Scorecard.
  * THE GATE: a verifier authored as an external contribution (``examples/contrib/ph_bounds.py``,
    ``authors=["A. Contributor"]``) passes the *same* kernel as a first-party one -> SCORING.
  * a genuinely non-deterministic verifier does NOT pass (G4).
  * the entry-point seam: a verifier advertised under the ``litmus.verifiers`` group is discovered
    and registered, and a broken entry point is captured in ``load_errors``, never raised
    (DESIGN §9, §13). Exercised WITHOUT pip-installing into the shared venv.
  * the CLI: ``verifier test <path>`` exits 0 on a SCORING verifier.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

from litmus.adapters import cli
from litmus.commons import sdk
from litmus.commons.registry import Registry
from litmus.core.calibration import AdmissionStatus
from litmus.core.claim import Claim, EpistemicTier
from litmus.core.finding import Severity, Status, VerifierKind
from litmus.core.verifier import (
    Determinism,
    SelfTestCase,
    Verifier,
    VerifierManifest,
)

# Bind the SDK entry points to local names that do NOT start with ``test_`` — otherwise pytest
# would collect ``sdk.test_verifier`` (a plain function, not a test) and error on a missing
# ``target`` fixture. The CLI verb is "test"; the Python API is reached via the ``sdk`` module.
scaffold_verifier = sdk.scaffold_verifier
load_verifier = sdk.load_verifier
calibrate_verifier = sdk.test_verifier  # alias: sdk.test_verifier(...) -> Scorecard

REPO_ROOT = Path(__file__).resolve().parents[1]
PH_BOUNDS_PATH = REPO_ROOT / "examples" / "contrib" / "ph_bounds.py"


def _import_isolated(path: Path, name: str):
    """Import a .py file by path (the same move the SDK + entry-point seam make)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# =============================================================================
# 1. scaffold writes an importable module that exposes VERIFIERS.
# =============================================================================
def test_scaffold_writes_importable_module(tmp_path):
    py_path = scaffold_verifier("charge_balance.v1", tmp_path, tier="T1", kind="prebuilt")

    # Both the module and its docs stub were written; the version is stripped from the filename.
    assert py_path.exists()
    assert py_path.name == "charge_balance.py"
    assert (tmp_path / "charge_balance.md").exists()

    # The generated module imports cleanly and exposes a VERIFIERS list of Verifier instances.
    module = _import_isolated(py_path, "_scaffold_charge_balance")
    assert hasattr(module, "VERIFIERS")
    assert len(module.VERIFIERS) == 1
    v = module.VERIFIERS[0]
    assert isinstance(v, Verifier)

    # The manifest carries the id/tier/kind we asked for, with no leftover template placeholders.
    assert v.manifest.id == "charge_balance.v1"
    assert v.manifest.version == "1.0"
    assert v.manifest.epistemic_tier.value == "T1"
    assert v.manifest.kind is VerifierKind.PREBUILT
    src = py_path.read_text()
    assert "__VERIFIER_" not in src  # every placeholder was substituted

    # The docs stub mentions the id and the local-validation command.
    docs = (tmp_path / "charge_balance.md").read_text()
    assert "charge_balance.v1" in docs
    assert "litmus verifier test charge_balance.v1" in docs


def test_scaffold_kind_sets_determinism(tmp_path):
    """A scaffolded 'synthesized' verifier is stamped SYNTHESIZED; 'assisted' -> ASSISTED."""
    syn = scaffold_verifier("synth_demo.v1", tmp_path, tier="T0", kind="synthesized")
    asd = scaffold_verifier("assist_demo.v1", tmp_path, tier="T4", kind="assisted")
    m_syn = _import_isolated(syn, "_scaffold_synth").VERIFIERS[0].manifest
    m_asd = _import_isolated(asd, "_scaffold_assist").VERIFIERS[0].manifest
    assert m_syn.kind is VerifierKind.SYNTHESIZED
    assert m_syn.determinism is Determinism.SYNTHESIZED
    assert m_asd.kind is VerifierKind.ASSISTED
    assert m_asd.determinism is Determinism.ASSISTED


def test_scaffold_rejects_bad_inputs(tmp_path):
    with pytest.raises(ValueError):
        scaffold_verifier("x.v1", tmp_path, tier="T99")  # unknown tier
    with pytest.raises(ValueError):
        scaffold_verifier("x.v1", tmp_path, kind="bogus")  # unknown kind
    with pytest.raises(ValueError):
        scaffold_verifier("   ", tmp_path)  # empty id


def test_scaffold_refuses_overwrite_without_flag(tmp_path):
    scaffold_verifier("dup_check.v1", tmp_path)
    with pytest.raises(FileExistsError):
        scaffold_verifier("dup_check.v1", tmp_path)
    # ...but --force / overwrite=True replaces it.
    scaffold_verifier("dup_check.v1", tmp_path, overwrite=True)


# =============================================================================
# 2. THE GATE — the contributed verifier passes the same kernel -> SCORING.
# =============================================================================
def test_ph_bounds_contrib_is_scoring():
    """The external contribution (authors=['A. Contributor']) clears the kernel: SCORING."""
    assert PH_BOUNDS_PATH.exists(), PH_BOUNDS_PATH
    card = calibrate_verifier(str(PH_BOUNDS_PATH), print_report=False)

    assert card.verifier_id == "ph_bounds.v1"
    assert card.admission is AdmissionStatus.SCORING, card.reasons
    assert card.is_scoring
    # Every gate the kernel measures actually held.
    assert card.recall == 1.0
    assert card.fpr_overall == 0.0
    assert card.deterministic is True
    assert card.reproducibility == 1.0
    assert card.n_clean >= 6 and card.n_planted >= 6
    assert all(card.gates[g] for g in ("G1", "G2", "G3", "G4", "G6"))
    # G6 is only meaningful with >1 claim_type in the clean set.
    assert len(card.fpr_by_claim_type) >= 2


def test_ph_bounds_authored_as_contribution():
    """Provenance is explicit: this is contributed, not first-party (DESIGN §3.7, §9)."""
    v = load_verifier(str(PH_BOUNDS_PATH))
    assert v.manifest.authors == ["A. Contributor"]
    assert v.manifest.provenance == "contributed"
    assert v.manifest.epistemic_tier.value == "T1"
    assert set(v.manifest.consumes) == {"ph", "reported_ph"}


def test_ph_bounds_flags_out_of_range_with_severity_a():
    """Judge FAILs an impossible pH (severity A) and ships a reproducing recompute script."""
    v = load_verifier(str(PH_BOUNDS_PATH))
    cases = {c.name: c for c in v.self_test()}
    planted = next(c for c in cases.values() if c.kind == "planted")
    finding = v.judge(planted.claim, planted.evidence)
    assert finding.status is Status.FAIL
    assert finding.severity is Severity.A
    assert finding.validate() == []  # FAIL ships script + expected_output + severity (DESIGN §3.2)


# =============================================================================
# 3. test_verifier on a non-deterministic verifier -> NOT scoring (G4).
# =============================================================================
class _NonDeterministicStub(Verifier):
    """Draws its verdict from an RNG + a flip counter, so the kernel always sees G4 fail.

    (Same construction as the calibration suite's fixture: a counter guarantees consecutive
    judge() calls differ, so the rejection is a hard fact, never a flaky coin.)
    """

    manifest = VerifierManifest(
        id="nondet_stub.v1",
        version="1.0",
        kind=VerifierKind.PREBUILT,
        epistemic_tier=EpistemicTier.T0,
        determinism=Determinism.DETERMINISTIC,
        consumes=["x"],
        fpr_ceiling=0.05,
        description="non-deterministic on purpose",
    )

    def __init__(self):
        self._calls = 0

    def judge(self, claim, evidence):
        self._calls += 1
        if self._calls % 2 == 0 or random.random() < 0.5:
            return self.make_finding(claim=claim, status=Status.PASS, message="flip a")
        return self.make_finding(claim=claim, status=Status.PASS, message="flip b")

    def self_test(self):
        cases = []
        for i in range(4):
            c = Claim(id=f"c{i}", text="t", epistemic_tier=EpistemicTier.T0)
            cases.append(SelfTestCase(name=f"clean{i}", kind="clean", claim=c, evidence=[]))
            cases.append(SelfTestCase(name=f"planted{i}", kind="planted", claim=c, evidence=[]))
        return cases


def test_nondeterministic_verifier_not_scoring():
    card = calibrate_verifier(_NonDeterministicStub(), print_report=False)
    assert card.admission is not AdmissionStatus.SCORING
    assert card.admission is AdmissionStatus.REJECTED
    assert card.deterministic is False
    assert card.gates.get("G4") is False


def test_load_verifier_by_registered_id():
    """test_verifier also accepts a registered id, not just a path (DESIGN §9)."""
    card = calibrate_verifier("sum_check.v1", print_report=False)
    assert card.verifier_id == "sum_check.v1"
    assert card.admission is AdmissionStatus.SCORING


def test_load_verifier_unknown_target_raises():
    with pytest.raises(ValueError):
        load_verifier("definitely_not_a_registered_id.v9")


# =============================================================================
# 4. The entry-point seam (DESIGN §9): discovered + registered; broken EP captured.
# =============================================================================
class _FakeEntryPoint:
    """A stand-in for importlib.metadata.EntryPoint — only ``name`` + ``load`` are used."""

    def __init__(self, name, loader):
        self.name = name
        self.group = "litmus.verifiers"
        self._loader = loader

    def load(self):
        return self._loader()


class _FakeEntryPoints:
    """A stand-in for the selectable EntryPoints object 3.12's entry_points() returns."""

    def __init__(self, eps):
        self._eps = list(eps)

    def select(self, *, group):
        return [ep for ep in self._eps if ep.group == group]


def _make_ph_bounds_entry_point():
    """An entry point whose load() yields ph_bounds's VERIFIERS — by importing the contrib file.

    This is exactly what a real out-of-tree distribution's entry point resolves to (its module's
    VERIFIERS), but WITHOUT pip-installing anything into the shared venv (DESIGN §9; constraint).
    """
    module = _import_isolated(PH_BOUNDS_PATH, "_ep_ph_bounds_module")
    return _FakeEntryPoint("ph_bounds", lambda: module.VERIFIERS)


def test_entry_point_discovery_registers_contrib(monkeypatch):
    """A monkeypatched entry_points() makes discover_entry_points() pick up ph_bounds."""
    import importlib.metadata as ilm

    good = _make_ph_bounds_entry_point()

    def _broken_loader():
        raise RuntimeError("third-party plugin import blew up")

    broken = _FakeEntryPoint("broken_plugin", _broken_loader)

    fake = _FakeEntryPoints([good, broken])
    # Patch at the importlib.metadata level — the registry calls metadata.entry_points().
    monkeypatch.setattr(ilm, "entry_points", lambda: fake)

    reg = Registry()
    registered = reg.discover_entry_points()

    # The good entry point's VERIFIERS were loaded and registered...
    assert [v.manifest.id for v in registered] == ["ph_bounds.v1"]
    assert "ph_bounds.v1" in reg
    assert reg.get("ph_bounds.v1").manifest.authors == ["A. Contributor"]

    # ...and the broken one was captured in load_errors, NOT raised (DESIGN §9, §13).
    assert any(name == "broken_plugin" for name, _ in reg.load_errors)
    assert all("ph_bounds" != name for name, _ in reg.load_errors)


def test_entry_point_broken_plugin_is_isolated(monkeypatch):
    """A broken entry point alone is recorded and discovery still returns cleanly (no crash)."""
    import importlib.metadata as ilm

    def _boom():
        raise ValueError("boom")

    fake = _FakeEntryPoints([_FakeEntryPoint("only_broken", _boom)])
    monkeypatch.setattr(ilm, "entry_points", lambda: fake)

    reg = Registry()
    registered = reg.discover_entry_points()  # must not raise
    assert registered == []
    assert any(name == "only_broken" for name, _ in reg.load_errors)


# =============================================================================
# 5. The CLI: `litmus verifier test <path>` exits 0 on a SCORING verifier.
# =============================================================================
def test_cli_verifier_test_path_exits_zero(capsys):
    code = cli.main(["verifier", "test", str(PH_BOUNDS_PATH)])
    assert code == 0
    out = capsys.readouterr().out
    assert "ph_bounds.v1" in out
    assert "SCORING" in out


def test_cli_verifier_test_registered_id_exits_zero():
    assert cli.main(["verifier", "test", "sum_check.v1"]) == 0


def test_cli_verifier_test_unknown_exits_two(capsys):
    code = cli.main(["verifier", "test", "no_such_verifier.v9"])
    assert code == 2
    assert "error" in capsys.readouterr().err.lower()


def test_cli_verifier_new_scaffolds_then_test_rejects_stub(tmp_path, capsys):
    """The contributor loop: `verifier new` writes a stub; `verifier test` on it exits 1 (no fuel)."""
    new_code = cli.main(["verifier", "new", "loop_demo.v1", "--dir", str(tmp_path), "--tier", "T0"])
    assert new_code == 0
    py_path = tmp_path / "loop_demo.py"
    assert py_path.exists()
    capsys.readouterr()  # drain

    # The freshly scaffolded stub has an empty self_test -> REJECTED -> exit 1.
    test_code = cli.main(["verifier", "test", str(py_path)])
    assert test_code == 1


def test_cli_verifier_list_still_works(capsys):
    """WS-C must not break the pre-existing `verifier list` subcommand."""
    code = cli.main(["verifier", "list"])
    assert code == 0
    assert "ID" in capsys.readouterr().out
