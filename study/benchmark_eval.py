"""Benchmark evaluation — the convergence step (DESIGN §17 caveat, §19 convergence).

Cross-references the discovery study's CANDIDATE benchmark (reasoner judgments, not gold) with
the deterministic LITMUS audit verdicts over the same corpus. A candidate "suspect" label
becomes GOLD only once a deterministic verifier (or a human) confirms it (DESIGN §17). This
script reports, honestly:

  * the corpus-wide deterministic audit result (confirmed, reproducible flags = gold positives;
    passes = gold negatives; abstains = out of current verifier reach / synthesis gaps);
  * deterministic PRECISION = fraction of emitted flags that reproduced in a fresh sandbox
    (1 - dropped/emitted) — this is measured, not asserted;
  * the calibrated FPR ceiling each scoring verifier was admitted under (the measured operating
    bound from the kernel, §7);
  * CONVERGENCE: where the reasoner said "suspect" and LITMUS deterministically confirmed a flag
    (candidate -> gold), vs. where LITMUS abstained (the WS-D/E synthesis queue).

It deliberately does NOT fabricate a single "recall" number against the external corpus: that
requires human confirmation of the reasoner's suspect claims (DESIGN §17 — using LLM judgments
to validate an LLM-extraction system would be circular). What it reports is reproducible.

Run:  .venv/bin/python study/benchmark_eval.py
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    try:
        return json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main() -> None:
    audits = {}
    for af in glob.glob(os.path.join(ROOT, "study/corpus/audits/*.json")):
        pid = os.path.basename(af)[:-5]
        a = _load(af)
        if a:
            audits[pid] = a

    # --- corpus-wide deterministic audit result -----------------------------
    emitted = reproduced = passes = abstains = routed = 0
    flags_by_verifier: Counter = Counter()
    flags_by_field: Counter = Counter()
    papers_with_flags = {}
    manifest = {p["id"]: p for p in (_load(os.path.join(ROOT, "study/corpus/manifest.json")) or {}).get("papers", [])}

    for pid, a in audits.items():
        field = manifest.get(pid, {}).get("field", "?")
        fails = [f for f in a.get("findings", []) if f.get("status") == "fail"]
        emitted += len(fails) + len(a.get("dropped_flags", []))
        reproduced += len(fails)  # findings that survived fresh-context confirmation
        passes += len([f for f in a.get("findings", []) if f.get("status") == "pass"])
        abstains += len(a.get("abstained", []))
        routed += len(a.get("routed_to_human", []))
        if fails:
            papers_with_flags[pid] = len(fails)
        for f in fails:
            flags_by_verifier[f.get("verifier_id", "?")] += 1
            flags_by_field[field] += 1

    det_precision = (reproduced / emitted) if emitted else 1.0

    # --- candidate benchmark (reasoner) -------------------------------------
    bench = _load(os.path.join(ROOT, "study/discovery/candidate-benchmark.json")) or {}
    items = bench.get("items", [])
    suspect = [i for i in items if i.get("candidate_label") == "suspect"]
    supported = [i for i in items if i.get("candidate_label") == "supported"]
    suspect_papers = {i.get("paper_id") for i in suspect}

    # --- convergence: reasoner-suspect ∩ LITMUS-confirmed -------------------
    converged = sorted(suspect_papers & set(papers_with_flags))
    suspect_but_abstained = sorted(suspect_papers - set(papers_with_flags))

    # --- calibrated FPR ceilings (the measured operating bound, §7) ---------
    fpr_ceilings = {}
    try:
        import sys

        sys.path.insert(0, ROOT)
        from litmus.commons.registry import build_default_registry

        for v in build_default_registry().all():
            fpr_ceilings[v.manifest.id] = v.manifest.fpr_ceiling
    except Exception:
        pass

    report = {
        "corpus": {
            "papers_audited": len(audits),
            "confirmed_flags": reproduced,
            "emitted_flags": emitted,
            "deterministic_precision": round(det_precision, 4),
            "passes": passes,
            "abstained": abstains,
            "routed_to_human": routed,
            "flags_by_verifier": dict(flags_by_verifier),
            "flags_by_field": dict(flags_by_field),
            "papers_with_confirmed_flags": papers_with_flags,
        },
        "candidate_benchmark": {
            "total_items": len(items),
            "suspect": len(suspect),
            "supported": len(supported),
            "note": "reasoner judgments — candidate, not gold (DESIGN §17)",
        },
        "convergence": {
            "papers_reasoner_suspect_AND_litmus_confirmed": converged,
            "papers_reasoner_suspect_but_litmus_abstained": suspect_but_abstained,
            "interpretation": (
                "candidate->gold: a deterministic verifier confirmed a reproducible flag on these "
                "papers, promoting the reasoner's suspicion to gold (DESIGN §19 convergence). "
                "Papers where LITMUS abstained are the synthesis/coverage queue (WS-D/E/F)."
            ),
        },
        "calibrated_fpr_ceilings": fpr_ceilings,
        "honesty_note": (
            "No single external 'recall' is reported: confirming the reasoner's suspect claims "
            "needs a human or a new deterministic verifier (DESIGN §17 — avoid circularity). The "
            "numbers above are all reproducible: every confirmed flag reran in a fresh, "
            "network-less sandbox; deterministic_precision is measured, not asserted."
        ),
    }

    out = os.path.join(ROOT, "study/discovery/benchmark_eval.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(report, open(out, "w"), indent=2)

    c = report["corpus"]
    print("LITMUS benchmark evaluation (DESIGN §17, §19 convergence)")
    print("=" * 70)
    print(f"corpus: {c['papers_audited']} papers audited")
    print(f"  confirmed flags: {c['confirmed_flags']}  (deterministic precision "
          f"{c['deterministic_precision']:.0%} = reproduced/emitted; emitted={c['emitted_flags']})")
    print(f"  passes: {c['passes']}  abstained: {c['abstained']}  routed_to_human: {c['routed_to_human']}")
    print(f"  flags by verifier: {c['flags_by_verifier']}")
    print(f"  flags by field: {c['flags_by_field']}")
    b = report["candidate_benchmark"]
    print(f"candidate benchmark: {b['total_items']} items ({b['suspect']} suspect / {b['supported']} supported) — candidate, not gold")
    cv = report["convergence"]
    print(f"convergence (candidate->gold): {cv['papers_reasoner_suspect_AND_litmus_confirmed']}")
    print(f"synthesis queue (suspect but abstained): {len(cv['papers_reasoner_suspect_but_litmus_abstained'])} papers")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
