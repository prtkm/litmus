"""Schema loading + validation (DESIGN §10: JSON is canonical, schema-validated).

Every artifact that crosses a boundary — ClaimGraph, AuditReport, Finding, VerifierManifest —
validates against a published JSON schema before it is trusted.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

import jsonschema

SCHEMA_FILES = {
    "claim": "claim.schema.json",
    "audit": "audit.schema.json",
    "finding": "finding.schema.json",
    "verifier_manifest": "verifier_manifest.schema.json",
}


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    """Load a schema by short name ('claim') or filename ('claim.schema.json')."""
    fname = SCHEMA_FILES.get(name, name)
    text = resources.files("litmus.schemas").joinpath(fname).read_text(encoding="utf-8")
    return json.loads(text)


@lru_cache(maxsize=None)
def _validator(name: str) -> jsonschema.protocols.Validator:
    schema = load_schema(name)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def validate(instance: Any, name: str) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    validator = _validator(name)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]


def is_valid(instance: Any, name: str) -> bool:
    return not validate(instance, name)


def assert_valid(instance: Any, name: str) -> None:
    errs = validate(instance, name)
    if errs:
        raise ValueError(f"{name} schema validation failed:\n  " + "\n  ".join(errs))
