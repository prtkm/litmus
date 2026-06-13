# Sourcing — chemistry arXiv preprints (chem-arxiv agent)

Fragment file written by the **chem-arxiv sourcing agent** for the LITMUS discovery corpus (DESIGN §17).
This agent sourced *additional* chemistry preprints only. The main corpus + `manifest.json` + `SOURCING.md`
are owned by a separate agent and were not touched.

Date run: 2026-06-13
Manifest fragment: `study/corpus/manifest-chemarxiv.json`
PDFs: `study/corpus/pdfs/chemistry-arxiv-*.pdf`

## Result summary

- **8 valid chemistry PDFs added** (target was >= 6). All pass validation: `head -c 4` == `%PDF` and size > 40000 bytes.
- Source route for all 8: **arXiv** (HTTPS API + PDF endpoint). arXiv was fully reachable and not rate-limited.
- **ChemRxiv: NOT reachable via curl — Cloudflare "Just a moment..." managed challenge (HTTP 403).** Substituted arXiv chemistry. Details below.

## arXiv queries used (exact)

The brief specified `http://export.arxiv.org/...`, which returned 0 bytes (the plain-HTTP endpoint redirected/blocked). Switching to **HTTPS** (`https://export.arxiv.org/...`) worked; all three queries returned 15 entries each.

1. Recent physics.chem-ph:
   ```
   https://export.arxiv.org/api/query?search_query=cat:physics.chem-ph&start=0&max_results=15&sortBy=submittedDate&sortOrder=descending
   ```
2. Recent materials chemistry:
   ```
   https://export.arxiv.org/api/query?search_query=cat:cond-mat.mtrl-sci&start=0&max_results=15&sortBy=submittedDate&sortOrder=descending
   ```
3. Catalysis within physics.chem-ph:
   ```
   https://export.arxiv.org/api/query?search_query=all:catalysis+AND+cat:physics.chem-ph&start=0&max_results=15&sortBy=submittedDate&sortOrder=descending
   ```

Atom XML parsed with Python `xml.etree`. Candidates were judged on quantitative/checkable content in the `<summary>`
(yields, rate constants, DFT/QMC energies, reaction barriers Ea, hardness in MPa, polarization in uC/cm2, interfacial
fields in V/A, thermal conductivity, Hall transport). Pure-theory/review entries with no numbers were skipped.

PDF download (per paper):
```
curl -sL --max-time 90 -A 'Mozilla/5.0' 'https://arxiv.org/pdf/<arxiv_id>.pdf' \
  -o study/corpus/pdfs/chemistry-arxiv-<arxiv_id>-<slug>.pdf
```

### De-duplication vs. main corpus
At run time the main agent had already placed chemistry PDFs covering: triple-halide perovskite tandem, CO2
electroreduction on Cu-ZIF, Pd-prolinate catalysis, transfer hydrogenation of furfural. The 8 picks below
deliberately avoid those topics (different reactions/materials/observables).

## ChemRxiv outcome (best-effort secondary)

Attempted per the brief. Outcome: **Cloudflare-gated to curl; could not retrieve any PDF.**

- The brief's OpenAlex source id `S4306402135` is **stale/incorrect** — it resolves to a Spanish biomedical/SciELO
  source (returned anxiety-disorder, oncology, sports-science articles; none chemistry, no `10.26434` DOIs).
- Looked up the correct ChemRxiv source via `https://api.openalex.org/sources?search=ChemRxiv` →
  **`S4393918830`** ("ChemRxiv", 56,447 OA works).
- Re-queried works with the correct id:
  ```
  https://api.openalex.org/works?filter=primary_location.source.id:S4393918830,open_access.is_oa:true&per-page=10&mailto=prateekmehta.in@gmail.com
  ```
  Every `best_oa_location.pdf_url` / `oa_url` pointed back to **chemrxiv.org** (Cloudflare host) or to a
  `doi.org` redirect that resolves to chemrxiv.org. No non-Cloudflare OA mirror was available.
- Direct probes both returned the Cloudflare interstitial:
  - `GET https://chemrxiv.org/engage/chemrxiv/public-api/v1/items?limit=5` -> **HTTP 403**, body = `<title>Just a moment...</title>` (cf-challenge).
  - `GET https://chemrxiv.org/` -> **HTTP 403**, Cloudflare `challenge-platform` markers present.

Per instructions, did not fight the challenge; recorded here and substituted arXiv chemistry.

## What was downloaded (all valid)

| arXiv id | primary cat | title (short) | why checkable (numbers in paper) | bytes |
|---|---|---|---|---|
| 2604.14784 | physics.chem-ph | Interfacial electric fields in water nanodroplets | outward field ~1.0–1.2 V/A; linear scaling with H-bond count; curvature/pH dependence | 1,011,558 |
| 2605.09394 | physics.chem-ph | Systematic fine-tuning of MACE potentials for catalysis | 9 MLIPs benchmarked on reaction energies Er and barriers Ea over 141 reactions (CO2->C2/C3, propane dehydrogenation, H on Pd) — Ea/Er mismatch checks | 5,319,917 |
| 2606.12779 | cond-mat.mtrl-sci | QMC molecular reference corrections (ORR on Pt(111)) | SD-FNDMC adsorption/formation energies; gas-phase thermochemistry cycle for oxygenated ORR intermediates | 506,678 |
| 2606.12999 | physics.chem-ph | In-phase current/temperature oscillation effect on PEM fuel-cell cathode impedance | analytic impedance model; CCL impedance & static resistivity vs. ORR exchange current density | 347,807 |
| 2606.13399 | cond-mat.mtrl-sci | Photocurrent in epitaxial 0.5PZT-0.5PFN multiferroic films | remanent polarization ~17 uC/cm2 (PUND); coercive field; photocurrent response | 1,620,485 |
| 2606.13420 | cond-mat.mtrl-sci | Melt-quenched alloy FeCoNiB0.7Si0.3Be phases & properties | cooling rate ~1e6 K/s; B2 + (Fe,Ni,Co)2B phases; microhardness 10400 -> 8900 MPa | 1,035,563 |
| 2606.13561 | cond-mat.mtrl-sci | Ultralow thermal conductivity in perovskite GuaPbI3 | symbolic-regression ML design; lattice thermal-conductivity values from lone-pair-induced softness | 3,503,242 |
| 2606.13664 | cond-mat.mtrl-sci | Magnetotransport in chiral 2D perovskite R-(MBA)2PbI4 | Hall-bar devices; p-type carrier density/mobility; low-T magnetotransport | 13,816,959 |

Field is labelled `chemistry` for all (per fragment schema); several sit at the chemistry/materials-chemistry
boundary (cond-mat.mtrl-sci) — electrocatalysis, perovskite/MOF chemistry, alloy synthesis, ferroelectrics.

DOIs use the arXiv DataCite form `10.48550/arXiv.<id>`. License recorded as arXiv non-exclusive (to be confirmed
per-paper; some carry CC where the authors stated it).
