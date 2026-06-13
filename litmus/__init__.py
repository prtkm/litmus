"""LITMUS — a self-extending, multi-domain auditor for published scientific literature.

A reasoning loop over a self-extending verifier library. The model extracts, locates,
and reasons about *what to check*; deterministic code decides *whether it holds*
(DESIGN §1, §3.1). This package is the framework: plain local Python, no external
services. The web app (app/) and managed-agents executor are separate.
"""

__version__ = "0.1.0"
