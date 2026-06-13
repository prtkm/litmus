"""The published JSON schemas are themselves valid, and they enforce the FAIL contract.

DESIGN §3.2 ("no script, no flag") is encoded in finding.schema.json: a ``fail`` finding must
carry a non-empty ``recompute_script`` + ``expected_output`` (and a severity). A ``pass`` is
allowed to have neither. These tests pin that the schema actually enforces it.
"""

from __future__ import annotations

import jsonschema
import pytest

from litmus.core import schema

SCHEMA_NAMES = ["claim", "finding", "audit", "verifier_manifest"]


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_itself_valid_json_schema(name):
    """Each schema document is a valid JSON Schema (its metaschema check_schema passes)."""
    s = schema.load_schema(name)
    validator_cls = jsonschema.validators.validator_for(s)
    validator_cls.check_schema(s)  # raises SchemaError if the schema is malformed


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_loads_by_short_name_and_filename(name):
    by_short = schema.load_schema(name)
    by_file = schema.load_schema(f"{name}.schema.json")
    assert by_short == by_file
    assert "$schema" in by_short


def _base_fail_finding() -> dict:
    return {
        "verifier_id": "sum_check.v1",
        "claim_id": "c1",
        "status": "fail",
        "trust_tier": "deterministic_confirmed",
        "verifier_kind": "prebuilt",
        "severity": "B",
        "message": "mismatch",
        "evidence": {
            "recompute_script": "print('MISMATCH reported=100 computed=97')\n",
            "expected_output": "MISMATCH reported=100 computed=97",
        },
    }


def test_fail_finding_without_recompute_script_is_invalid():
    bad = _base_fail_finding()
    del bad["evidence"]["recompute_script"]
    errors = schema.validate(bad, "finding")
    assert errors, "a FAIL finding missing recompute_script must fail finding-schema validation"
    assert not schema.is_valid(bad, "finding")


def test_fail_finding_with_empty_recompute_script_is_invalid():
    bad = _base_fail_finding()
    bad["evidence"]["recompute_script"] = ""  # minLength 1 in the FAIL branch
    assert schema.validate(bad, "finding")


def test_fail_finding_without_severity_is_invalid():
    bad = _base_fail_finding()
    del bad["severity"]
    assert schema.validate(bad, "finding")


def test_complete_fail_finding_is_valid():
    assert schema.validate(_base_fail_finding(), "finding") == []


def test_pass_finding_without_script_is_valid():
    good = {
        "verifier_id": "sum_check.v1",
        "claim_id": "c1",
        "status": "pass",
        "trust_tier": "deterministic_confirmed",
        "verifier_kind": "prebuilt",
        "message": "holds",
        "evidence": {},  # no script needed for a PASS
    }
    assert schema.validate(good, "finding") == []


def test_fail_finding_inside_audit_enforces_script():
    """The audit schema embeds the same FAIL contract for nested findings."""
    audit = {
        "paper_id": "p",
        "findings": [
            {
                "verifier_id": "sum_check.v1",
                "claim_id": "c1",
                "status": "fail",
                "trust_tier": "deterministic_confirmed",
                "verifier_kind": "prebuilt",
                # missing severity + script -> must be rejected
                "evidence": {},
            }
        ],
    }
    assert schema.validate(audit, "audit")
