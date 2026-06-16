"""Paper-level finding gates applied at report assembly, shared by every executor.

These run AFTER per-claim verification + fresh-context confirmation, with the whole findings list in
view, so they can make decisions a single verifier can't (cross-finding patterns). They mutate the
findings in place and never change a verifier's PASS/FAIL verdict (calibration tests the verdict, not
the trust tier — so these gates can't regress the kernel).
"""

from __future__ import annotations

from litmus.core.finding import Finding, Status, TrustTier


def gate_grim_relevance(findings: list[Finding]) -> None:
    """GRIM is always a SCREENING signal, never a confirmed error on its own.

    A single reported mean being GRIM-impossible is, by itself, within rounding/transcription noise: a
    0.01-0.03 gap is exactly what a typo or a half-up-vs-truncate choice produces (owner: "the errors
    are in the second decimal and fine"). That the mean is impossible is a FACT — we never touch
    ``status`` / ``severity`` / the recompute evidence, so the calibration kernel and G3 are untouched
    — but it is never asserted as a CONFIRMED quantitative error. So every GRIM cluster is re-tiered to
    ONE ``routed_to_human`` screening note (still FAIL, still reproducible, scripts attached, fully
    promotable). The genuinely-hard quantitative flags stay reserved for errors that CANNOT be a
    rounding artifact — a subgroup total that doesn't add up (sum_check), a p-value that flips
    significance (statcheck), an impossible yield.

    CORROBORATION (an independent non-GRIM deterministic error on the same paper) does not change the
    GRIM tier — it sets the note's PROMINENCE. Wansink: its subgroup totals are 89 vs a stated 95, so
    its hard flag is that sum error, and the GRIM cluster is the corroborating "and the means are
    impossible too" note. Festinger / kniffin / just2014 have GRIM only -> a low-key "likely rounding"
    note. Mutates ``trust_tier`` + ``details`` only.
    """
    grim_fails = [
        f
        for f in findings
        if f.status is Status.FAIL and (f.verifier_id or "").split(".")[0] == "grim"
    ]
    if not grim_fails:
        return

    n = len(grim_fails)
    plural = n != 1

    # An independent, un-roundable error on the same paper raises the note's prominence (it does NOT
    # promote the GRIM means to hard flags — only the independent error itself is the hard flag).
    corroborated = any(
        f.status is Status.FAIL
        and f.trust_tier is TrustTier.DETERMINISTIC_CONFIRMED
        and (f.verifier_id or "").split(".")[0] != "grim"
        for f in findings
    )

    if corroborated:
        note = (
            f"{n} reported mean{'s' if plural else ''} on this paper "
            f"{'are' if plural else 'is'} GRIM-impossible (cannot arise from whole-number responses at "
            "the stated N) — and an INDEPENDENT arithmetic error (one that can't be a rounding artifact, "
            "flagged separately) was found on the same paper. Each mean is individually within rounding "
            "distance, but together with the corroborating error this is worth a close look at the raw "
            "data. Each recompute script is attached."
        )
    else:
        note = (
            f"{n} reported mean{'s' if plural else ''} on this paper "
            f"{'are' if plural else 'is'} GRIM-impossible (cannot arise from whole-number responses at "
            "the stated N), but each sits within ~1-3 hundredths of an achievable value and there is no "
            "other arithmetic error on the paper — individually consistent with rounding or typesetting. "
            "Surfaced for review, not a confirmed error. Each recompute script is attached."
        )
    for f in grim_fails:
        f.trust_tier = TrustTier.ROUTED_TO_HUMAN
        f.details = {
            **(f.details or {}),
            "grim_cluster_size": n,
            "grim_cluster": n >= 2,
            "grim_corroborated": corroborated,
            "grim_screening_note": True,
            "grim_cluster_note": note,
        }


# Back-compat: the relevance gate subsumes the old lone-fragile behavior. Kept so existing imports/
# call sites keep working.
def gate_fragile_grim(findings: list[Finding]) -> None:
    gate_grim_relevance(findings)
