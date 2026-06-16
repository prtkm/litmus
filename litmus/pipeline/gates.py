"""Paper-level finding gates applied at report assembly, shared by every executor.

These run AFTER per-claim verification + fresh-context confirmation, with the whole findings list in
view, so they can make decisions a single verifier can't (cross-finding patterns). They mutate the
findings in place and never change a verifier's PASS/FAIL verdict (calibration tests the verdict, not
the trust tier — so these gates can't regress the kernel).
"""

from __future__ import annotations

from litmus.core.finding import Finding, Status, TrustTier


def gate_grim_relevance(findings: list[Finding]) -> None:
    """Decide whether a paper's GRIM inconsistencies are RELEVANT hard flags or a screening note.

    GRIM is a paper-level SCREEN, not a per-mean verdict (Brown & Heathers; owner feedback: "we need
    judgment of when things are actually relevant before flagging"). That a reported mean is
    arithmetically impossible is a FACT — we never touch ``status`` / ``severity`` / the recompute
    evidence, so the calibration kernel and G3 are untouched — but whether it is a CONFIRMED
    data-integrity error or a screening signal is a relevance call.

    The discriminating quantity is the ANCHOR test (computed in ``grim.py``): a member whose gap
    exceeds ~1.5 printed display units AND is not reproducible by a normal rounding convention
    (round-half-up / truncation). A paper's GRIM cluster ships HARD (``deterministic_confirmed``) iff
    it has >=1 anchor — which keeps the documented-fraud catches (Wansink 1.89-2.88, Festinger 2.00).
    An anchor-LESS group (kniffin 1.00; just2014 0.67-0.85, two truncation-reproducible) is re-tiered
    to a single ``routed_to_human`` screening note: still FAIL, still reproducible, still visible and
    promotable — just not asserted as a confirmed error. Mutates ``trust_tier`` + ``details`` only.

    NOTE: a rate/count gate cannot split kniffin (100% cluster of 2) from Festinger (100% cluster of
    3), and an N-perturbation suppressor would wrongly rescue Festinger 2.77/4.88 — both verified
    recall-fatal in the critique. The anchor (magnitude + convention) is the load-bearing signal.
    """
    grim_fails = [
        f
        for f in findings
        if f.status is Status.FAIL and (f.verifier_id or "").split(".")[0] == "grim"
    ]
    if not grim_fails:
        return

    n = len(grim_fails)
    has_anchor = any(bool((f.details or {}).get("grim_anchor")) for f in grim_fails)

    # Cluster bookkeeping the UI uses to consolidate members into one finding.
    for f in grim_fails:
        f.details = {**(f.details or {}), "grim_cluster_size": n, "grim_cluster": n >= 2}

    if has_anchor:
        # >=1 substantive, non-convention impossibility → a relevant hard flag. Keep the cluster hard.
        return

    # No anchor: every member is a marginal or convention-reproducible inconsistency, individually
    # consistent with rounding/transcription. Re-tier the whole group to ONE human-review screening
    # note — NOT suppressed (status + recompute evidence intact, promotable by corroboration).
    plural = n != 1
    note = (
        f"{n} reported mean{'s' if plural else ''} on this paper "
        f"{'are' if plural else 'is'} GRIM-impossible, but each is marginal — within ~1 display unit "
        f"of an achievable value, or reproducible under a normal rounding convention (round-half-up / "
        f"truncation). Individually consistent with rounding or transcription, so this is a screening "
        f"signal routed for review, not a confirmed error. Each recompute script is attached."
    )
    for f in grim_fails:
        f.trust_tier = TrustTier.ROUTED_TO_HUMAN
        f.details = {**(f.details or {}), "grim_screening_note": True, "grim_cluster_note": note}


# Back-compat: the relevance gate subsumes the old lone-fragile behavior. Kept so existing imports/
# call sites keep working.
def gate_fragile_grim(findings: list[Finding]) -> None:
    gate_grim_relevance(findings)
