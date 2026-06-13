"""The system scorecard harness + CLI (DESIGN §7, §19): the WS-A gate's exit behavior.

``litmus.verify`` aggregates per-verifier calibration into one PASS/FAIL with an exit code,
and the ``litmus`` CLI delegates to it. These tests pin the contract the gate depends on.
"""

from __future__ import annotations

import json

from litmus import verify as verify_module
from litmus.adapters import cli
from litmus.commons.registry import Registry
from litmus.core.calibration import AdmissionStatus
from litmus.verifiers.sum_check import SumCheck


def test_run_returns_scorecard_per_verifier():
    cards = verify_module.run()
    ids = [c.verifier_id for c in cards]
    assert "sum_check.v1" in ids


def test_default_gate_passes_with_scoring_verifier(capsys):
    rc = verify_module.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sum_check.v1" in out
    assert "SCORING" in out
    assert "GATE" in out and "PASS" in out


def test_strict_gate_passes_when_all_scoring(capsys):
    rc = verify_module.main(["--strict"])
    capsys.readouterr()
    assert rc == 0


def test_json_output_is_machine_readable(capsys):
    rc = verify_module.main(["--json"])
    out = capsys.readouterr().out
    assert rc == 0
    cards = json.loads(out)
    assert isinstance(cards, list)
    sc = next(c for c in cards if c["verifier_id"] == "sum_check.v1")
    assert sc["admission"] == AdmissionStatus.SCORING.value
    assert sc["recall"] >= 0.9
    assert sc["deterministic"] is True
    assert sc["reproducibility"] == 1.0


def test_strict_gate_fails_when_a_verifier_is_not_scoring(capsys, monkeypatch):
    """Inject a registry whose only verifier is advisory/rejected -> strict gate exits 1."""

    class _NoFuel(SumCheck):
        # Same judge, but no self_test -> kernel REJECTS it (not SCORING).
        def self_test(self):
            return []

    reg = Registry()
    reg.register(_NoFuel())
    monkeypatch.setattr(verify_module, "build_default_registry", lambda: reg)

    rc_strict = verify_module.main(["--strict"])
    assert rc_strict == 1
    # Default policy also treats an intended-scoring verifier that fell out of SCORING as a regression.
    rc_default = verify_module.main([])
    assert rc_default == 1


# --- CLI delegation ----------------------------------------------------------
def test_cli_verify_delegates(capsys):
    rc = cli.main(["verify"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sum_check.v1" in out


def test_cli_verify_json(capsys):
    rc = cli.main(["verify", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    cards = json.loads(out)
    assert any(c["verifier_id"] == "sum_check.v1" for c in cards)


def test_cli_verifier_list(capsys):
    rc = cli.main(["verifier", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sum_check.v1" in out
    assert "T0" in out
    assert "prebuilt" in out


def test_cli_requires_subcommand(capsys):
    """Bare `litmus` with no subcommand errors out (argparse exits non-zero)."""
    import pytest

    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code != 0
