"""Generates tests/fixtures/studies.json: a small, hand-written set of study records
in the exact shape the ClinicalTrials.gov v2 API returns for a `fields`-restricted
request. Run `python tests/fixtures/make_fixture.py` to regenerate.

The records are synthetic but modeled on real registry entries, and they
deliberately cover the awkward cases: combined phases, observational studies
with no phase, missing start dates, brand/code names that fold to one MeSH
term, placebo arms, multi-country trials, collaborators, arm groups that
separate combinations from comparators, a trial with no arm data, and a
trial that matches a drug search without listing the drug as an intervention.
"""

import json
from pathlib import Path


def study(
    nct_id,
    title,
    status,
    start,
    completion=None,
    phases=None,
    study_type="INTERVENTIONAL",
    enrollment=None,
    sponsor=None,
    sponsor_class=None,
    collaborators=(),
    conditions=(),
    interventions=(),
    countries=(),
    intervention_mesh=(),
    condition_mesh=(),
    arms=(),
):
    protocol = {
        "identificationModule": {"nctId": nct_id, "briefTitle": title},
        "statusModule": {"overallStatus": status},
        "sponsorCollaboratorsModule": {},
        "conditionsModule": {"conditions": list(conditions)},
        "designModule": {"studyType": study_type},
    }
    if start:
        protocol["statusModule"]["startDateStruct"] = {"date": start, "type": "ACTUAL"}
    if completion:
        protocol["statusModule"]["completionDateStruct"] = {"date": completion, "type": "ESTIMATED"}
    if phases is not None:
        protocol["designModule"]["phases"] = list(phases)
    if enrollment is not None:
        protocol["designModule"]["enrollmentInfo"] = {"count": enrollment, "type": "ESTIMATED"}
    if sponsor:
        protocol["sponsorCollaboratorsModule"]["leadSponsor"] = {"name": sponsor, "class": sponsor_class}
    if collaborators:
        protocol["sponsorCollaboratorsModule"]["collaborators"] = [{"name": c, "class": "INDUSTRY"} for c in collaborators]
    if interventions or arms:
        protocol["armsInterventionsModule"] = {}
    if arms:
        # (label, type, intervention names as the registry lists them, description)
        protocol["armsInterventionsModule"]["armGroups"] = [
            {"label": label, "type": kind, "description": desc, "interventionNames": list(names)}
            for (label, kind, names, desc) in arms
        ]
    if interventions:
        protocol["armsInterventionsModule"]["interventions"] = [
            {"type": t, "name": n, **({"otherNames": list(o)} if o else {})} for (t, n, o) in interventions
        ]
    if countries:
        protocol["contactsLocationsModule"] = {"locations": [{"country": c} for c in countries]}
    derived = {}
    if intervention_mesh:
        derived["interventionBrowseModule"] = {
            "meshes": [{"id": f"D{i:06d}", "term": t} for i, t in enumerate(intervention_mesh)]
        }
    if condition_mesh:
        derived["conditionBrowseModule"] = {"meshes": [{"id": f"C{i:06d}", "term": t} for i, t in enumerate(condition_mesh)]}
    record = {"protocolSection": protocol}
    if derived:
        record["derivedSection"] = derived
    return record


STUDIES = [
    study(
        "NCT05000001",
        "Pembrolizumab Plus Lenvatinib Versus Pembrolizumab Alone in Advanced Melanoma",
        "RECRUITING",
        "2021-03-15",
        "2025-06-30",
        ["PHASE3"],
        enrollment=530,
        sponsor="Merck Sharp & Dohme LLC",
        sponsor_class="INDUSTRY",
        conditions=["Melanoma"],
        interventions=[("BIOLOGICAL", "Pembrolizumab", ["MK-3475", "KEYTRUDA"]), ("DRUG", "Lenvatinib", ["E7080"])],
        countries=["United States", "United States", "France", "Germany", "Japan"],
        intervention_mesh=["Pembrolizumab", "Lenvatinib"],
        condition_mesh=["Melanoma"],
        arms=[
            ("Pembrolizumab + Lenvatinib", "EXPERIMENTAL", ["Biological: Pembrolizumab", "Drug: Lenvatinib"], "Combination arm"),
            ("Pembrolizumab", "ACTIVE_COMPARATOR", ["Biological: Pembrolizumab"], "Monotherapy arm"),
        ],
    ),
    study(
        "NCT05000002",
        "MK-3475 Versus Placebo as Adjuvant Therapy in Resected Stage III Melanoma",
        "COMPLETED",
        "2018-06",
        "2022-01",
        ["PHASE2"],
        enrollment=120,
        sponsor="Merck Sharp & Dohme LLC",
        sponsor_class="INDUSTRY",
        conditions=["Melanoma", "Stage III Melanoma"],
        interventions=[("BIOLOGICAL", "MK-3475", ["Pembrolizumab", "Keytruda"]), ("DRUG", "Placebo", [])],
        countries=["United States", "Canada"],
        intervention_mesh=["Pembrolizumab"],
        condition_mesh=["Melanoma"],
        arms=[
            ("MK-3475", "EXPERIMENTAL", ["Biological: MK-3475"], "200 mg every 3 weeks"),
            ("Placebo", "PLACEBO_COMPARATOR", ["Drug: Placebo"], "Saline every 3 weeks"),
        ],
    ),
    study(
        "NCT05000003",
        "Nivolumab Combined With Ipilimumab in Treatment-Naive Metastatic Melanoma",
        "ACTIVE_NOT_RECRUITING",
        "2019-11-01",
        "2024-12-31",
        ["PHASE1", "PHASE2"],
        enrollment=85,
        sponsor="Bristol-Myers Squibb",
        sponsor_class="INDUSTRY",
        conditions=["Metastatic Melanoma"],
        interventions=[("BIOLOGICAL", "Nivolumab", ["BMS-936558", "Opdivo"]), ("BIOLOGICAL", "Ipilimumab", ["Yervoy"])],
        countries=["United States", "Australia"],
        intervention_mesh=["Nivolumab", "Ipilimumab"],
        condition_mesh=["Melanoma"],
        arms=[("Nivolumab + Ipilimumab", "EXPERIMENTAL", ["Biological: Nivolumab", "Biological: Ipilimumab"], "Both drugs")],
    ),
    study(
        "NCT05000004",
        "Nivolumab Versus Observation After Complete Resection of Stage IIIB/C Melanoma",
        "COMPLETED",
        "2015-04-20",
        "2020-03-01",
        ["PHASE3"],
        enrollment=1200,
        sponsor="National Cancer Institute (NCI)",
        sponsor_class="NIH",
        collaborators=["Bristol-Myers Squibb"],
        conditions=["Melanoma"],
        interventions=[("BIOLOGICAL", "Nivolumab", []), ("OTHER", "Observation", [])],
        countries=["United States", "United States", "United States", "Canada"],
        intervention_mesh=["Nivolumab"],
        condition_mesh=["Melanoma"],
        arms=[
            ("Nivolumab", "EXPERIMENTAL", ["Biological: Nivolumab"], "Adjuvant nivolumab"),
            ("Observation", "NO_INTERVENTION", ["Other: Observation"], "Observation only"),
        ],
    ),
    study(
        "NCT05000005",
        "Dabrafenib and Trametinib in BRAF V600 Mutant Unresectable Melanoma",
        "COMPLETED",
        "2013-01",
        "2018-05",
        ["PHASE3"],
        enrollment=870,
        sponsor="Novartis Pharmaceuticals",
        sponsor_class="INDUSTRY",
        conditions=["Melanoma"],
        interventions=[("DRUG", "Dabrafenib 150 mg", ["GSK2118436"]), ("DRUG", "Trametinib 2 mg", ["GSK1120212"])],
        countries=["United States", "United Kingdom", "Germany", "Italy", "Spain"],
        intervention_mesh=["Dabrafenib", "Trametinib"],
        condition_mesh=["Melanoma"],
        arms=[("Dabrafenib + Trametinib", "EXPERIMENTAL", ["Drug: Dabrafenib 150 mg", "Drug: Trametinib 2 mg"], "Doublet")],
    ),
    study(
        "NCT05000006",
        "Prospective Registry of Patients With Cutaneous Melanoma",
        "RECRUITING",
        "2022-09-01",
        "2030-12-31",
        None,
        study_type="OBSERVATIONAL",
        enrollment=300,
        sponsor="University Hospital, Basel, Switzerland",
        sponsor_class="OTHER",
        conditions=["Cutaneous Melanoma"],
        countries=["Switzerland"],
        condition_mesh=["Melanoma"],
    ),
    study(
        "NCT05000007",
        "Pembrolizumab With Stereotactic Radiosurgery for Melanoma Brain Metastases",
        "RECRUITING",
        "2020-02-10",
        "2026-02-10",
        ["PHASE2"],
        enrollment=60,
        sponsor="M.D. Anderson Cancer Center",
        sponsor_class="OTHER",
        collaborators=["Merck Sharp & Dohme LLC"],
        conditions=["Melanoma", "Brain Metastases"],
        interventions=[("BIOLOGICAL", "Pembrolizumab 200 mg IV", []), ("RADIATION", "Stereotactic radiosurgery", [])],
        countries=["United States"],
        intervention_mesh=["Pembrolizumab"],
        condition_mesh=["Melanoma", "Brain Neoplasms"],
        arms=[
            (
                "Pembrolizumab + SRS",
                "EXPERIMENTAL",
                ["Biological: Pembrolizumab 200 mg IV", "Radiation: Stereotactic radiosurgery"],
                "Radiosurgery within 7 days of the first dose",
            )
        ],
    ),
    study(
        "NCT05000008",
        "Ipilimumab Re-induction in Melanoma Patients Who Progressed",
        "TERMINATED",
        "2016-08",
        "2017-12",
        ["PHASE2"],
        enrollment=40,
        sponsor="Bristol-Myers Squibb",
        sponsor_class="INDUSTRY",
        conditions=["Melanoma"],
        interventions=[("BIOLOGICAL", "Ipilimumab", [])],
        countries=["France"],
        intervention_mesh=["Ipilimumab"],
        condition_mesh=["Melanoma"],
        arms=[("Ipilimumab", "EXPERIMENTAL", ["Biological: Ipilimumab"], "Re-induction")],
    ),
    study(
        "NCT05000009",
        "Pembrolizumab Versus Chemotherapy in PD-L1 Positive Non-Small Cell Lung Cancer",
        "COMPLETED",
        "2017-05-01",
        "2021-11-30",
        ["PHASE3"],
        enrollment=1000,
        sponsor="Merck Sharp & Dohme LLC",
        sponsor_class="INDUSTRY",
        conditions=["Non-Small Cell Lung Cancer"],
        interventions=[("BIOLOGICAL", "Pembrolizumab", ["MK-3475"]), ("DRUG", "Docetaxel", [])],
        countries=["United States", "China", "Japan", "Korea, Republic of"],
        intervention_mesh=["Pembrolizumab", "Docetaxel"],
        condition_mesh=["Carcinoma, Non-Small-Cell Lung"],
        arms=[
            ("Pembrolizumab", "EXPERIMENTAL", ["Biological: Pembrolizumab"], "200 mg every 3 weeks"),
            ("Docetaxel", "ACTIVE_COMPARATOR", ["Drug: Docetaxel"], "75 mg/m2 every 3 weeks"),
        ],
    ),
    study(
        "NCT05000010",
        "Pembrolizumab Plus Carboplatin and Pemetrexed in Nonsquamous NSCLC",
        "ACTIVE_NOT_RECRUITING",
        "2019-01-15",
        "2023-12-31",
        ["PHASE3"],
        enrollment=600,
        sponsor="Merck Sharp & Dohme LLC",
        sponsor_class="INDUSTRY",
        conditions=["Non-Small Cell Lung Cancer"],
        interventions=[("BIOLOGICAL", "Pembrolizumab", []), ("DRUG", "Carboplatin", []), ("DRUG", "Pemetrexed", [])],
        countries=["United States", "Germany"],
        intervention_mesh=["Pembrolizumab", "Carboplatin", "Pemetrexed"],
        condition_mesh=["Carcinoma, Non-Small-Cell Lung"],
        arms=[
            (
                "Pembrolizumab + chemotherapy",
                "EXPERIMENTAL",
                ["Biological: Pembrolizumab", "Drug: Carboplatin", "Drug: Pemetrexed"],
                "Triplet",
            )
        ],
    ),
    study(
        "NCT05000011",
        "AI-Assisted Dermoscopy for Early Melanoma Detection",
        "NOT_YET_RECRUITING",
        "2024-05",
        "2026-05",
        ["NA"],
        enrollment=500,
        sponsor="Stanford University",
        sponsor_class="OTHER",
        conditions=["Melanoma"],
        interventions=[("DEVICE", "AI dermoscopy decision support", [])],
        countries=["United States"],
        condition_mesh=["Melanoma"],
        arms=[("Decision support", "EXPERIMENTAL", ["Device: AI dermoscopy decision support"], "Dermoscopy with AI")],
    ),
    study(
        "NCT05000012",
        "Nivolumab Plus Relatlimab Versus Nivolumab in Previously Untreated Melanoma",
        "RECRUITING",
        "2021-07-01",
        "2027-01-31",
        ["PHASE3"],
        enrollment=700,
        sponsor="Bristol-Myers Squibb",
        sponsor_class="INDUSTRY",
        conditions=["Melanoma"],
        interventions=[("BIOLOGICAL", "Nivolumab", []), ("BIOLOGICAL", "Relatlimab (BMS-986016)", [])],
        countries=["United States", "France", "Germany", "Brazil"],
        intervention_mesh=["Nivolumab"],
        condition_mesh=["Melanoma"],
        arms=[
            (
                "Nivolumab + Relatlimab",
                "EXPERIMENTAL",
                ["Biological: Nivolumab", "Biological: Relatlimab (BMS-986016)"],
                "Fixed dose",
            ),
            ("Nivolumab", "ACTIVE_COMPARATOR", ["Biological: Nivolumab"], "Monotherapy"),
        ],
    ),
    study(
        "NCT05000013",
        "First-in-Human Study of PF-07284890 in Advanced Melanoma",
        "WITHDRAWN",
        None,
        None,
        ["PHASE1"],
        enrollment=0,
        sponsor="Pfizer",
        sponsor_class="INDUSTRY",
        conditions=["Melanoma"],
        interventions=[("DRUG", "PF-07284890", [])],
        condition_mesh=["Melanoma"],
    ),
    study(
        "NCT05000014",
        "Keytruda in Mucosal Melanoma: A Single-Arm Study",
        "COMPLETED",
        "2016-02-01",
        "2019-08-01",
        ["PHASE2"],
        enrollment=50,
        sponsor="Gustave Roussy, Cancer Campus, Grand Paris",
        sponsor_class="OTHER",
        conditions=["Mucosal Melanoma"],
        interventions=[("BIOLOGICAL", "Keytruda", [])],
        countries=["France"],
        intervention_mesh=["Pembrolizumab"],
        condition_mesh=["Melanoma"],
        arms=[("Keytruda", "EXPERIMENTAL", ["Biological: Keytruda"], "Single arm")],
    ),
    # Matches an intervention search for "pembrolizumab" through the arm description only: the drug is
    # not an intervention of this trial. Exercises the drug-match check.
    study(
        "NCT05000015",
        "Nivolumab in Advanced Renal Cell Carcinoma After Progression on Pembrolizumab",
        "COMPLETED",
        "2018-09-01",
        "2022-03-01",
        ["PHASE2"],
        enrollment=45,
        sponsor="Dana-Farber Cancer Institute",
        sponsor_class="OTHER",
        conditions=["Renal Cell Carcinoma"],
        interventions=[("BIOLOGICAL", "Nivolumab", [])],
        countries=["Canada"],
        intervention_mesh=["Nivolumab"],
        condition_mesh=["Carcinoma, Renal Cell"],
        arms=[
            (
                "Nivolumab",
                "EXPERIMENTAL",
                ["Biological: Nivolumab"],
                "Nivolumab 240 mg every two weeks for patients whose disease progressed on prior pembrolizumab",
            )
        ],
    ),
]


if __name__ == "__main__":
    out = Path(__file__).with_name("studies.json")
    out.write_text(json.dumps({"studies": STUDIES, "totalCount": len(STUDIES)}, indent=2))
    print(f"wrote {len(STUDIES)} studies to {out}")
