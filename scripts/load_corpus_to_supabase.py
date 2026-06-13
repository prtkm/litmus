"""Load LITMUS audited papers into Supabase (DESIGN §2 cache, §10, §15).

Reads the locally-produced claim graphs + audit reports + corpus manifest + discovery
catalogs (for bibliographic title/doi) and upserts one `papers` row per audited paper. The
live app's data layer reads these rows (RLS public-read); writes use the service key.

Run with the Supabase creds in the environment:
    SUPABASE_URL=... SUPABASE_SECRET_KEY=... .venv/bin/python scripts/load_corpus_to_supabase.py
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import urllib.request

SUPA = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SECRET_KEY"]


def _manifest() -> dict:
    try:
        return {p["id"]: p for p in json.load(open("study/corpus/manifest.json"))["papers"]}
    except FileNotFoundError:
        return {}


def _catalog_meta(pid: str) -> tuple[str | None, str | None]:
    f = f"study/discovery/catalog-{pid}.json"
    if os.path.exists(f):
        try:
            c = json.load(open(f))
            return c.get("title"), c.get("doi")
        except Exception:
            pass
    return None, None


def _field_from_id(pid: str) -> str:
    head = pid.split("-")[0]
    return head or "unknown"


def _humanize(pid: str) -> str:
    import re

    toks = [t for t in pid.split("-") if not re.match(r"^[a-z]+\d{2,4}[a-z]?$", t)]
    if toks and toks[0] in {
        "nutrition", "psychology", "health", "chemistry", "biology", "medicine",
        "economics", "physics", "ml", "econ",
    }:
        toks = toks[1:]
    return " ".join(w.capitalize() for w in toks) or pid


def main() -> None:
    manifest = _manifest()
    rows = []
    for af in sorted(glob.glob("study/corpus/audits/*.json")):
        pid = os.path.basename(af)[:-5]
        audit = json.load(open(af))
        cg_f = f"study/corpus/claims/{pid}.json"
        claim_graph = json.load(open(cg_f)) if os.path.exists(cg_f) else None
        title, doi = _catalog_meta(pid)
        m = manifest.get(pid, {})
        cg_meta = (claim_graph or {}).get("meta", {}) if claim_graph else {}
        title = title or m.get("title") or cg_meta.get("title") or _humanize(pid)
        doi = doi or m.get("doi") or cg_meta.get("doi")
        field = m.get("field") or _field_from_id(pid)
        pdf_path = f"study/corpus/pdfs/{pid}.pdf"
        if os.path.exists(pdf_path):
            # sha256 of the actual PDF — the §2 cache key the upload path matches on.
            ch = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()
        else:
            ch = m.get("sha256") or hashlib.sha256(pid.encode()).hexdigest()
        rows.append(
            {
                "content_hash": ch,
                "doi": doi,
                "title": title,
                "field": field,
                "status": "done",
                "claim_graph": claim_graph,
                "audit_report": audit,
            }
        )

    body = json.dumps(rows).encode()
    url = f"{SUPA}/rest/v1/papers?on_conflict=content_hash"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": KEY,
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
    )
    with urllib.request.urlopen(req) as resp:
        out = json.load(resp)
    print(f"HTTP {resp.status}: upserted {len(out)} papers")
    for r in sorted(out, key=lambda x: -len([f for f in (x.get("audit_report") or {}).get("findings", []) if f["status"] == "fail"])):
        ar = r.get("audit_report") or {}
        flags = len([f for f in ar.get("findings", []) if f["status"] == "fail"])
        routed = len(ar.get("routed_to_human", []))
        print(f"  {(r.get('title') or r['content_hash'])[:52]:52}  {r['field']:10} flags={flags} routed_to_human={routed}")


if __name__ == "__main__":
    main()
