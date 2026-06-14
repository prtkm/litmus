"""Paper-level finding gates applied at report assembly, shared by every executor.

These run AFTER per-claim verification + fresh-context confirmation, with the whole findings list in
view, so they can make decisions a single verifier can't (cross-finding patterns). They mutate the
findings in place and never change a verifier's PASS/FAIL verdict (calibration tests the verdict, not
the trust tier — so these gates can't regress the kernel).
"""

from __future__ import annotations

from litmus.core.finding import Finding, Status, TrustTier


def gate_fragile_grim(findings: list[Finding]) -> None:
    """Soften a LONE, fragile GRIM inconsistency from a hard deterministic flag to advisory/review.

    Owner feedback (DESIGN §3.6): a single GRIM-impossible mean is *fragile* when dropping one
    response makes it achievable (``grim`` marks ``details['fragile']`` — e.g. M=6.62 is impossible
    at n=62 but achievable at n=61, consistent with one missing item). In isolation that is weak
    evidence and should be reviewed, not asserted as a confirmed error. But a PATTERN — two or more
    GRIM flags, or any *robust* one — is exactly what catches real data-integrity problems
    (Wansink/Festinger), so those stay ``deterministic_confirmed``.

    Rule: if there is exactly one GRIM FAIL and it is fragile, downgrade it to ``advisory_assisted``
    and append the honest "achievable at n-1" context to its discrepancy. Otherwise leave every GRIM
    flag untouched. The FAIL, severity, and executable evidence are unchanged either way.
    """
    grim_fails = [
        f
        for f in findings
        if f.status is Status.FAIL and (f.verifier_id or "").split(".")[0] == "grim"
    ]
    if not grim_fails:
        return

    def _is_fragile(f: Finding) -> bool:
        return bool((f.details or {}).get("fragile"))

    # A pattern (>=2 GRIM flags) or any robust (non-fragile) flag is real evidence — keep it hard.
    has_pattern = len(grim_fails) >= 2 or any(not _is_fragile(f) for f in grim_fails)
    if has_pattern:
        return

    # Exactly one GRIM flag, and it's fragile → soften to advisory + add honest N-sensitivity context.
    for f in grim_fails:
        if not _is_fragile(f) or f.trust_tier is not TrustTier.DETERMINISTIC_CONFIRMED:
            continue
        f.trust_tier = TrustTier.ADVISORY_ASSISTED
        rescued = (f.details or {}).get("rescued_at_n")
        rescued_note = f"achievable if N were {rescued}" if rescued else "achievable at a nearby N"
        ctx = (
            f" — {rescued_note} (consistent with one missing or excluded response). A single GRIM "
            f"inconsistency is sensitive to the exact N, so this is routed for review rather than "
            f"asserted as a hard error."
        )
        f.discrepancy = (f.discrepancy or "") + ctx
