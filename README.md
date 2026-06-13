# LITMUS

A self-extending, multi-domain instrument for auditing the published scientific literature
with executable evidence.

The model extracts, locates, and reasons about *what to check*; deterministic code decides
*whether it holds*. Every flag ships a recompute script a skeptical reader can rerun. See
[`DESIGN.md`](DESIGN.md) for the controlling design document.

## Development

```sh
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q          # tests
.venv/bin/python -m litmus.verify      # the system calibration scorecard (the gate)
litmus verifier list                   # registered verifiers
```

The calibration kernel (`litmus/core/calibration.py`) is the project's reward function: every
verifier — first-party, contributed, or synthesized — is admitted through the same gate, with
zero human labels (`G1` recall, `G2`/`G6` false-positive rate, `G3` reproducibility, `G4`
determinism).
