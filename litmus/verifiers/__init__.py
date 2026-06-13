"""First-party verifiers (DESIGN §6.1 class A, §19 WS-D).

In-tree, hardened verifiers that ship with LITMUS. Each is a self-describing package
(manifest + ``judge`` + ``self_test``) admitted through the same calibration gate (DESIGN §7)
as any contributed or synthesized verifier. The registry (``litmus.commons.registry``)
discovers and registers them.
"""
