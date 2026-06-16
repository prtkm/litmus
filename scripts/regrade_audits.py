"""One-off maintenance: bring stored Supabase audit_report records up to the current GRIM logic —
dedupe duplicate findings, re-derive magnitude-graded severity + the formatted recompute fields, and
apply the RELEVANCE gate (an anchor-less GRIM cluster is re-tiered to a routed-to-human screening
note). Matches litmus/verifiers/grim.py + litmus/pipeline/gates.gate_grim_relevance so the data
itself is correct, not just the frontend shim. Idempotent. Reads/writes via the service key.
"""

from __future__ import annotations

from litmus.app_backend.supabase_io import PAPERS_TABLE, SupabaseConfig, SupabaseIO
from litmus.verifiers.grim import _fmt_mean, _reproduces_under_convention

EPS = 1e-9


def _key(f: dict) -> str:
    exp = (f.get("evidence") or {}).get("expected_output")
    if f.get("status") == "fail" and isinstance(exp, str) and exp:
        return f"{f.get('verifier_id')}::fail::{exp}"
    return f"{f.get('verifier_id')}::{f.get('claim_id')}::{f.get('status')}"


def _has_quote(f: dict) -> bool:
    return bool(((f.get("evidence") or {}).get("location") or {}).get("quote"))


def dedup(findings: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for f in findings:
        k = _key(f)
        prev = out.get(k)
        if prev is None or (not _has_quote(prev) and _has_quote(f)):
            out[k] = f
    return list(out.values())


def regrade_grim(findings: list[dict]) -> bool:
    grim = [
        f
        for f in findings
        if f.get("status") == "fail" and (f.get("verifier_id") or "").split(".")[0] == "grim"
    ]
    if not grim:
        return False
    n = len(grim)
    changed = False
    # Corroboration: an independent (non-GRIM) deterministic error on the same paper.
    corroborated = any(
        f.get("status") == "fail"
        and f.get("trust_tier") == "deterministic_confirmed"
        and (f.get("verifier_id") or "").split(".")[0] != "grim"
        for f in findings
    )
    for f in grim:
        d = dict(f.get("details") or {})
        gran = d.get("granularity")
        dec = d.get("decimals", 2)
        nitems = d.get("n_items", 1)
        nearest = d.get("nearest_mean")
        if nearest is None and d.get("nearest_total") is not None and gran:
            nearest = d["nearest_total"] / gran
        rep = f.get("reported")
        if nearest is None or rep is None or not gran:
            continue
        d_disp = abs(rep - nearest) / (10 ** -dec)
        conv_ok, conv_name = (
            _reproduces_under_convention(rep, gran, dec) if nitems == 1 else (False, None)
        )
        anchor = (d_disp > 1.5 + EPS) and not conv_ok
        new_sev = "C" if (d_disp <= 1.0 + EPS or conv_ok) else "B"
        d.update(
            {
                "grid_distance_display_units": d_disp,
                "convention_reproducible": conv_ok,
                "convention": conv_name,
                "grim_anchor": anchor,  # informational only — the gate keys on corroboration
                "grim_cluster_size": n,
                "grim_cluster": n >= 2,
                "grim_corroborated": corroborated,
            }
        )
        d.setdefault("nearest_mean_str", _fmt_mean(nearest, dec))
        if d.get("nearest_total") is not None:
            d.setdefault("nearest_fraction", f"{d['nearest_total']}/{gran}")
        if f.get("severity") != new_sev:
            changed = True
        f["severity"] = new_sev
        f["details"] = d
    # GRIM is always a screening signal — re-tier the whole cluster. Corroboration only sets wording.
    plural = n != 1
    if corroborated:
        note = (
            f"{n} reported mean{'s' if plural else ''} on this paper "
            f"{'are' if plural else 'is'} GRIM-impossible — and an INDEPENDENT arithmetic error (one "
            "that can't be a rounding artifact, flagged separately) was found on the same paper. Each "
            "mean is individually within rounding distance, but together with the corroborating error "
            "this is worth a close look at the raw data."
        )
    else:
        note = (
            f"{n} reported mean{'s' if plural else ''} on this paper "
            f"{'are' if plural else 'is'} GRIM-impossible (cannot arise from whole-number responses at "
            "the stated N), but each sits within ~1-3 hundredths of an achievable value and there is no "
            "other arithmetic error on the paper — individually consistent with rounding or typesetting. "
            "Surfaced for review, not a confirmed error."
        )
    for f in grim:
        if f.get("trust_tier") == "deterministic_confirmed":
            f["trust_tier"] = "routed_to_human"
            changed = True
        d = dict(f.get("details") or {})
        d["grim_screening_note"] = True
        d["grim_cluster_note"] = note
        f["details"] = d
    return changed


def main() -> None:
    io = SupabaseIO(SupabaseConfig.from_env())
    url = (
        f"{io.config.rest_url}/{PAPERS_TABLE}"
        "?select=content_hash,audit_report&audit_report=not.is.null"
    )
    rows = io._request("GET", url, headers=io.config.headers()).json()
    print(f"scanning {len(rows)} papers…")
    n_upd = 0
    for row in rows:
        ch = row.get("content_hash")
        ar = row.get("audit_report") or {}
        findings = ar.get("findings") or []
        if not findings or not ch:
            continue
        deduped = dedup(findings)
        c1 = len(deduped) != len(findings)
        c2 = regrade_grim(deduped)
        if c1 or c2:
            ar["findings"] = deduped
            purl = f"{io.config.rest_url}/{PAPERS_TABLE}?content_hash=eq.{ch}"
            io._request(
                "PATCH",
                purl,
                headers=io.config.headers(extra={"Prefer": "return=minimal"}),
                json={"audit_report": ar},
            )
            n_upd += 1
            tags = (" deduped" if c1 else "") + (" regraded/gated" if c2 else "")
            print(f"  updated {ar.get('paper_id')}: {len(findings)}->{len(deduped)} findings{tags}")
    io.close()
    print(f"DONE — updated {n_upd} records.")


if __name__ == "__main__":
    main()
