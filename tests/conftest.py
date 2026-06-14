"""Shared pytest configuration for the LITMUS suite.

The suite contains a handful of LIVE integration tests that make real Anthropic API /
managed-agents calls with long (600-900s) timeouts — `test_managed_live` alone runs ~9 minutes.
Their gating is `skipif(no ANTHROPIC_API_KEY)`, which does NOT help when a key is present in the
environment (the common dev case): they then run on every `pytest tests/` and dominate wall time
(~12 min vs ~35s for everything else).

So we auto-tag any test whose name contains "live" with the `live` marker, and `pyproject.toml`'s
`addopts = -m "not live"` deselects them by default. Run them explicitly with `pytest -m live`
(needs `ANTHROPIC_API_KEY` + the managed-agents beta). This is name-based on purpose: the fast,
network-less deterministic tests that happen to live in a `*_live.py` module (e.g.
`test_custom_tools_run_real_verifier_code`) are NOT named "live" and stay in the default run.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "live" in item.name:
            item.add_marker(pytest.mark.live)
