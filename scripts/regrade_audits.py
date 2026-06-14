"""One-off maintenance: re-derive GRIM magnitude-graded severity + cluster stamps, dedupe duplicate
findings, and add the formatted recompute fields, INTO the stored Supabase audit_report records — so
the data itself matches the current grim.py / gate_fragile_grim logic instead of relying on the
frontend back-compat shim. Idempotent: safe to re-run. Reads/writes via the service key (SupabaseIO).
"""

from __future__ import annotations

from litmus.app_backend.supabase_io import PAPERS_TABLE, SupabaseConfig, SupabaseIO
from litmus.verifiers.grim import _fmt_mean, _reproduces_under_truncation

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


def regrade(findings: list[dict]) -> tuple[bool, list[dict]]:
    changed = False
    grim = [
        f
        for f in findings
        if f.get("status") == "fail" and (f.get("verifier_id") or "").split(".")[0] == "grim"
    ]
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
        conv = (nitems == 1) and _reproduces_under_truncation(rep, gran, dec)
        new_sev = "C" if (d_disp <= 1.0 + EPS or conv) else "B"
        d["grid_distance_display_units"] = d_disp
        d["convention_reproducible"] = conv
        d.setdefault("nearest_mean_str", _fmt_mean(nearest, dec))
        if d.get("nearest_total") is not None:
            d.setdefault("nearest_fraction", f"{d['nearest_total']}/{gran}")
        if f.get("severity") != new_sev or f.get("details") != d:
            changed = True
        f["severity"] = new_sev
        f["details"] = d
    if len(grim) >= 2:
        for f in grim:
            d = dict(f.get("details") or {})
            if d.get("grim_cluster_size") != len(grim):
                d["grim_cluster_size"] = len(grim)
                d["grim_cluster"] = True
                f["details"] = d
                changed = True
    return changed, findings


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
        c2, deduped = regrade(deduped)
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
            tags = (" deduped" if c1 else "") + (" regraded" if c2 else "")
            print(f"  updated {ar.get('paper_id')}: {len(findings)}->{len(deduped)} findings{tags}")
    io.close()
    print(f"DONE — updated {n_upd} records.")


if __name__ == "__main__":
    main()
