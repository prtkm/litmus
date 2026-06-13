"""On-the-fly verifier synthesis (DESIGN §8, §19 WS-F).

When the planner meets a checkable claim with no matching A/B verifier, this package
asks the reasoner (Opus 4.8) to *write* a bespoke deterministic verifier, then puts it
through the SAME calibration gate (DESIGN §7) as every first-party one. The trust comes
from the gate, not the model: a synthesized verifier that can't catch a planted version
of its own error, whose output varies, or whose flag won't reproduce never scores.

  propose_verifier  -> ask Opus for {strategy, manifest, judge_src, self_test_src}
  materialize       -> sandbox-vet the source, then import it into a real Verifier
  synthesize        -> propose -> materialize -> calibrate -> admit | reject
"""

from litmus.synth.synthesizer import (
    SynthesisError,
    materialize,
    propose_verifier,
    synthesize,
)

__all__ = [
    "propose_verifier",
    "materialize",
    "synthesize",
    "SynthesisError",
]
