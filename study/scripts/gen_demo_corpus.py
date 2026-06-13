"""Generate claim-graph JSONs for the demo corpus of papers with documented, verifiable flaws.

Each claim graph is authored from the paper's REAL published numbers (verified against a
documenting source: a correction notice, a peer-reviewed reanalysis, or the GRIM literature).
The deterministic LITMUS verifiers then recompute and flag them. Extraction was authored from the
documented values rather than run through Opus, given the demo deadline; the verification is real.
"""
import json
from pathlib import Path

OUT = Path("study/corpus/claims")
OUT.mkdir(parents=True, exist_ok=True)


def loc(section, page, quote):
    return {"section": section, "page": page, "char_span": None, "quote": quote}


def grim_ev(i, mean, n, quote, section="Results", page=1, decimals=2, n_items=1):
    return {
        "id": f"ev{i}",
        "kind": "statistic",
        "location": loc(section, page, quote),
        "extracted_values": {"reported_mean": mean, "n": n, "n_items": n_items, "decimals": decimals},
        "confidence": 0.95,
    }


def claim(cid, text, evid, quote, tier="T0", section="Results", page=1, conf=0.95,
          predicate="reported value recomputed from the paper's own numbers"):
    return {
        "id": cid, "text": text, "location": loc(section, page, quote),
        "epistemic_tier": tier, "predicate": predicate, "strength": "exact",
        "scope": "reported descriptive/statistic", "evidence_refs": [evid], "confidence": conf,
    }


def write(paper_id, meta, claims, evidence):
    bindings = []
    for c in claims:
        for ev in c["evidence_refs"]:
            bindings.append({"claim_id": c["id"], "evidence_id": ev, "relation": "rests_on"})
    doc = {"schema_version": "1.0", "paper_id": paper_id, "meta": meta,
           "claims": claims, "evidence": evidence, "bindings": bindings}
    (OUT / f"{paper_id}.json").write_text(json.dumps(doc, indent=2))
    print("wrote", paper_id, f"({len(claims)} claims)")


# ---------------------------------------------------------------------------
# 1. Festinger & Carlsmith 1959 — canonical GRIM example (N=20 per condition)
# ---------------------------------------------------------------------------
fest = [
    ("c1", 3.03, 20, "How much they enjoyed (mean 3.03)", "Q2 enjoyment, $1 condition"),
    ("c2", 2.77, 20, "How much they enjoyed (mean 2.77)", "Q2 enjoyment, $20 condition"),
    ("c3", 4.88, 20, "Scientific importance (mean 4.88)", "Q3 importance, $20 condition"),
]
fc, fe = [], []
for i, (cid, m, n, q, sc) in enumerate(fest, 1):
    fe.append(grim_ev(i, m, n, q, section=sc, page=206))
    fc.append(claim(cid, f"Reported cell mean {m} with N={n} per condition.", f"ev{i}", q,
                    section=sc, page=206, predicate="reported_mean"))
write("psychology-festinger1959-cognitive-dissonance",
      {"journal": "J. Abnormal and Social Psychology", "doi": "10.1037/h0041593", "year": 1959,
       "title": "Cognitive consequences of forced compliance",
       "source": "GRIM literature (Brown & Heathers 2017); psychclassics.yorku.ca full text",
       "note": "N=20/condition -> every mean must be a multiple of 1/20=0.05."}, fc, fe)

# ---------------------------------------------------------------------------
# 2. Wansink 2015 buffet "Low prices and high regret" — GRIM + sum (RETRACTED)
# ---------------------------------------------------------------------------
wan = [
    ("c1", 2.63, 18, "I ate more pizza than I should have ($4, one piece) M=2.63"),
    ("c2", 1.97, 17, "I am physically uncomfortable ($8, one piece) M=1.97"),
    ("c3", 1.67, 17, "I overate ($8, one piece) M=1.67"),
    ("c4", 3.92, 10, "I ate more than I should have ($8, three pieces) M=3.92"),
]
wc, we = [], []
for i, (cid, m, n, q) in enumerate(wan, 1):
    we.append(grim_ev(i, m, n, q, page=3))
    wc.append(claim(cid, f"Reported Likert mean {m} with N={n}.", f"ev{i}", q, page=3,
                    predicate="reported_mean"))
# sum: subgroup Ns 18+18+7+17+19+10 reported total 95
we.append({"id": "ev5", "kind": "table",
           "location": loc("Table 2", 3, "subgroup sizes by price x pieces; total reported 95"),
           "extracted_values": {"parts": [18, 18, 7, 17, 19, 10], "reported_total": 95},
           "confidence": 0.95})
wc.append(claim("c5", "Table 2 subgroup sizes (18,18,7,17,19,10) are stated to total 95 diners.",
                "ev5", "Total N reported as 95", section="Table 2", page=3, predicate="table_total"))
write("nutrition-wansink2015-buffet-price-regret",
      {"journal": "BMC Nutrition", "doi": "10.1186/s40795-015-0030-x", "year": 2015,
       "title": "Low prices and high regret: how pricing influences regret at all-you-can-eat buffets",
       "status": "RETRACTED (2017)",
       "source": "van der Zee, Anaya & Brown, 'Statistical heartburn', BMC Nutrition 2017",
       "note": "Multiple impossible Likert means; Table 2 Ns sum to 89, not 95."}, wc, we)

# ---------------------------------------------------------------------------
# 3. Just, Sigirci & Wansink 2014 — GRIM
# ---------------------------------------------------------------------------
just = [
    ("c1", 6.62, 62, "I was hungry when I came in, M=6.62"),
    ("c2", 1.88, 62, "I am hungry now, M=1.88"),
    ("c3", 7.44, 60, "The pizza tasted really great, M=7.44"),
    ("c4", 7.97, 26, "Middle piece tasted great, M=7.97"),
]
jc, je = [], []
for i, (cid, m, n, q) in enumerate(just, 1):
    je.append(grim_ev(i, m, n, q, page=365))
    jc.append(claim(cid, f"Reported Likert mean {m} with N={n}.", f"ev{i}", q, page=365,
                    predicate="reported_mean"))
write("nutrition-just2014-buffet-taste-satisfaction",
      {"journal": "Journal of Sensory Studies", "doi": "10.1111/joss.12117", "year": 2014,
       "title": "Lower buffet prices lead to less taste satisfaction",
       "source": "van der Zee, Anaya & Brown, 'Statistical heartburn', BMC Nutrition 2017",
       "note": "Impossible two-decimal means for the stated N."}, jc, je)

# ---------------------------------------------------------------------------
# 4. Kniffin, Sigirci & Wansink 2016 — GRIM
# ---------------------------------------------------------------------------
kni = [
    ("c1", 1.46, 40, "I felt rushed, M=1.46"),
    ("c2", 2.11, 40, "physically uncomfortable, M=2.11"),
]
kc, ke = [], []
for i, (cid, m, n, q) in enumerate(kni, 1):
    ke.append(grim_ev(i, m, n, q, page=41))
    kc.append(claim(cid, f"Reported Likert mean {m} with N={n}.", f"ev{i}", q, page=41,
                    predicate="reported_mean"))
write("psychology-kniffin2016-men-eat-more-with-women",
      {"journal": "Evolutionary Psychological Science", "doi": "10.1007/s40806-015-0035-3", "year": 2016,
       "title": "Eating heavily: men eat more in the company of women",
       "source": "van der Zee, Anaya & Brown, 'Statistical heartburn', BMC Nutrition 2017",
       "note": "Impossible means for N=40."}, kc, ke)

# ---------------------------------------------------------------------------
# 5. Loo et al. 2024 (JAMA Network Open) — percent_change (journal-corrected)
# ---------------------------------------------------------------------------
loo_ev = [{"id": "ev1", "kind": "number",
           "location": loc("Results", 4,
                           "total medical spending rose from $3004 (2017) to $4361 (2021), a 31.1% increase"),
           "extracted_values": {"old_value": 3004, "new_value": 4361, "reported_pct_change": 31.1},
           "confidence": 0.95}]
loo_c = [claim("c1", "Total medical spending rose from $3,004 to $4,361, reported as a 31.1% increase.",
               "ev1", "a 31.1% increase", section="Results", page=4, predicate="percent_change")]
write("health-econ-loo2024-pediatric-mental-health-spending",
      {"journal": "JAMA Network Open", "doi": "10.1001/jamanetworkopen.2024.1860", "year": 2024,
       "title": "Medical Spending Among US Households With Children With a Mental Health Condition, 2017-2021",
       "source": "Journal correction notice (PMC11022109): should read 45.2%",
       "note": "(4361-3004)/3004 = 45.2%, not the reported 31.1%."}, loo_c, loo_ev)

# ---------------------------------------------------------------------------
# 6. Disability & play, eClinicalMedicine 2023 — percent_change (corrigendum)
#    aRR=0.88 -> relative reduction 1-0.88 = 12%, text said "approximately 9% fewer"
# ---------------------------------------------------------------------------
dis_ev = [{"id": "ev1", "kind": "number",
           "location": loc("Results", 5,
                           "children with disabilities had approximately 9% fewer play opportunities (aRR 0.88)"),
           "extracted_values": {"old_value": 1.0, "new_value": 0.88, "reported_pct_change": -9.0},
           "confidence": 0.93}]
dis_c = [claim("c1", "Reported 'approximately 9% fewer' play opportunities while reporting aRR=0.88.",
               "ev1", "approximately 9% fewer", section="Results", page=5, predicate="percent_change")]
write("global-health-2023-disability-play-opportunities",
      {"journal": "eClinicalMedicine (Lancet Discovery Science)", "doi": "10.1016/j.eclinm.2023.102361",
       "year": 2023, "title": "Do children with disabilities have the same opportunities to play?",
       "source": "Journal corrigendum (PMC11237678): correct reduction is 12%",
       "note": "1 - aRR(0.88) = 12% reduction, not the reported 9%."}, dis_c, dis_ev)

# ---------------------------------------------------------------------------
# 7. Kanngiesser & Warneken 2012 (PLOS ONE) — statcheck (typeset stat errors)
#    F(1,32)=71, p=.405 and F(1,32)=49, p=.488 are internally impossible.
# ---------------------------------------------------------------------------
kw_ev = [
    {"id": "ev1", "kind": "statistic",
     "location": loc("Results", 4, "F(1, 32) = 71, p = .405"),
     "extracted_values": {"test": "F", "statistic": 71, "df1": 1, "df2": 32,
                          "reported_p": 0.405, "stat_decimals": 0, "decimals": 3},
     "confidence": 0.95},
    {"id": "ev2", "kind": "statistic",
     "location": loc("Results", 4, "F(1, 32) = 49, p = .488"),
     "extracted_values": {"test": "F", "statistic": 49, "df1": 1, "df2": 32,
                          "reported_p": 0.488, "stat_decimals": 0, "decimals": 3},
     "confidence": 0.95},
]
kw_c = [
    claim("c1", "Reported F(1,32)=71, p=.405.", "ev1", "F(1, 32) = 71, p = .405",
          section="Results", page=4, predicate="p_value"),
    claim("c2", "Reported F(1,32)=49, p=.488.", "ev2", "F(1, 32) = 49, p = .488",
          section="Results", page=4, predicate="p_value"),
]
write("psychology-kanngiesser2012-merit-sharing",
      {"journal": "PLOS ONE", "doi": "10.1371/journal.pone.0043979", "year": 2012,
       "title": "Young Children Consider Merit when Sharing Resources with Others",
       "source": "PLOS ONE correction (PMC5703034): decimals dropped in typesetting",
       "note": "An F of 71 (df 1,32) gives p~1e-9, not .405; the printed p is impossible."}, kw_c, kw_ev)

print("\nDONE")
