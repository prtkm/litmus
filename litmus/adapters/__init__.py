"""Adapters — the thin edges that drive the LITMUS framework (DESIGN §15, §19).

The CLI (``litmus ...``) lives here. Adapters translate an external surface (a terminal, later
an HTTP request or a managed-agents session) into calls on the core. They stay deliberately
thin: all real logic is in ``litmus.core`` / ``litmus.commons`` / ``litmus.verify``.
"""
