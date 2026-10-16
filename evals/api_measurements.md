# Live API measurements

Measured on 2026-10-16 04:04 UTC against https://clinicaltrials.gov/api/v2.

### 1. query.intr=pembrolizumab: matches that do not list the drug as an intervention

- 2,979 trials match `query.intr=pembrolizumab`; 2,979 fetched (all of them)
- 2,637 list it as an intervention by name, other name, MeSH term, or arm entry; 342 (11.5%) only mention it elsewhere in the intervention area
- examples of mention-only matches: NCT07691645 (Shuganhuazhuo Formula for Advanced Hepatocellular Carcinoma ), NCT07720180 (Study of Autologous, Metabolically Optimized, CD137+ Tumor-I), NCT07791615 (A Phase II Study of Stapokibart Combined With Immune Checkpo)
- consequence: `meta.retrieval[].drug_match` reports both counts on every response; `drug_match=strict` drops the mention-only trials

Reproduce:

```
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1000&query.intr=pembrolizumab&fields=NCTId%2CBriefTitle%2COverallStatus%2CStartDate%2CPrimaryCompletionDate%2CCompletionDate%2CPhase%2CStudyType%2CEnrollmentCount%2CLeadSponsorName%2CLeadSponsorClass%2CCollaboratorName%2CCondition%2CInterventionType%2CInterventionName%2CInterventionOtherName%2CArmGroupInterventionName%2CLocationCountry%2CInterventionMeshTerm%2CConditionMeshTerm&sort=StartDate%3Adesc&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1000&query.intr=pembrolizumab&fields=NCTId%2CBriefTitle%2COverallStatus%2CStartDate%2CPrimaryCompletionDate%2CCompletionDate%2CPhase%2CStudyType%2CEnrollmentCount%2CLeadSponsorName%2CLeadSponsorClass%2CCollaboratorName%2CCondition%2CInterventionType%2CInterventionName%2CInterventionOtherName%2CArmGroupInterventionName%2CLocationCountry%2CInterventionMeshTerm%2CConditionMeshTerm&sort=StartDate%3Adesc&countTotal=true&pageToken=ZVNj7o2Elu8o3lpzBdv-p-numpOQJJxuY_am2Pwbmj6SuP08FCfCFffS_9pM1iI'
```

### 2. An explicit 'Not Applicable' phase is not the same as no phase

- 607,160 studies in the registry
- 238,901 carry `AREA[Phase]NA` (an explicit 'Not Applicable', typical of device and behavioral trials)
- 463,201 are covered by the mutually exclusive phase buckets the service uses (Early Phase 1: 6,506, Phase 1: 48,854, Phase 1/Phase 2: 17,138, Phase 2: 65,822, Phase 2/Phase 3: 7,623, Phase 3: 42,403, Phase 4: 35,954, Not Applicable: 238,901)
- 143,959 have no phase value at all; 141,774 are observational studies, which never have one
- consequence: phase charts restrict to interventional studies and label the remainder 'Not specified' rather than folding it into 'Not Applicable'

Reproduce:

```
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&filter.advanced=AREA%5BPhase%5DNA&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&filter.advanced=AREA%5BPhase%5DEARLY_PHASE1&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&filter.advanced=AREA%5BPhase%5DPHASE1+AND+NOT+AREA%5BPhase%5DPHASE2&fields=NCTId&countTotal=true'
```

### 3. UNKNOWN status: records whose last known status was never updated

- 97,949 of 607,160 studies (16.1%) have overallStatus UNKNOWN
- for scale, 64,768 are RECRUITING
- consequence: status charts show 'Unknown status' as its own bar instead of dropping those trials, and a 'recruiting' filter never includes them

Reproduce:

```
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&filter.overallStatus=UNKNOWN&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&filter.overallStatus=RECRUITING&fields=NCTId&countTotal=true'
```

### 4. query.spons vs query.lead for 'Pfizer'

- `query.spons=Pfizer` (lead sponsor or collaborator): 6,094 trials
- `query.lead=Pfizer` (lead sponsor only): 3,887 trials
- consequence: the service searches with query.spons, so 'trials sponsored by X' includes trials where X is a collaborator; the sponsor-to-collaborator network separates the two roles, and the response says which search area was used in meta.api_calls

Reproduce:

```
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&query.spons=Pfizer&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&query.lead=Pfizer&pageSize=1&countTotal=true'
```

### 5. Drug pairs in the same trial vs the same arm (melanoma, interventional)

- 3,140 trials match; 3,140 fetched (all of them)
- 4,052 distinct drug pairs co-occur somewhere in a trial; 2,265 of them are listed together in at least one arm group
- 1,787 pairs (44.1%) never share an arm: they are comparators or alternatives, not combinations
- 320 of 3,140 fetched trials list no arm groups and cannot contribute to the same-arm network
- consequence: `drug_drug` links drugs only when they share an arm; `drug_drug_same_trial` is the looser graph, and the response names which one it built in `edge_semantics`

Reproduce:

```
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1000&query.cond=melanoma&filter.advanced=AREA%5BStudyType%5DINTERVENTIONAL&fields=NCTId%2CBriefTitle%2COverallStatus%2CStartDate%2CPrimaryCompletionDate%2CCompletionDate%2CPhase%2CStudyType%2CEnrollmentCount%2CLeadSponsorName%2CLeadSponsorClass%2CCollaboratorName%2CCondition%2CInterventionType%2CInterventionName%2CInterventionOtherName%2CArmGroupInterventionName%2CLocationCountry%2CInterventionMeshTerm%2CConditionMeshTerm&sort=StartDate%3Adesc&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1000&query.cond=melanoma&filter.advanced=AREA%5BStudyType%5DINTERVENTIONAL&fields=NCTId%2CBriefTitle%2COverallStatus%2CStartDate%2CPrimaryCompletionDate%2CCompletionDate%2CPhase%2CStudyType%2CEnrollmentCount%2CLeadSponsorName%2CLeadSponsorClass%2CCollaboratorName%2CCondition%2CInterventionType%2CInterventionName%2CInterventionOtherName%2CArmGroupInterventionName%2CLocationCountry%2CInterventionMeshTerm%2CConditionMeshTerm&sort=StartDate%3Adesc&countTotal=true&pageToken=ZVNj7o2Elu8o3lpzBdv-p-numpOQJJxuYPmj3vUdmj6SuP08FCfCFfDQ9NFP0SI'
```

### 6. A missing AREA[] prefix turns a phase filter into a free-text search (melanoma)

- `filter.advanced=AREA[Phase]PHASE3`: 221 trials
- `filter.advanced=PHASE3`: 230 trials (matches the token anywhere in the record)
- consequence: every Essie clause the service emits is built by `ctgov.params.essie_area`, which always writes the AREA[] prefix

Reproduce:

```
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&query.cond=melanoma&filter.advanced=AREA%5BPhase%5DPHASE3&fields=NCTId&countTotal=true'
curl -s 'https://clinicaltrials.gov/api/v2/studies?format=json&pageSize=1&query.cond=melanoma&filter.advanced=PHASE3&fields=NCTId&countTotal=true'
```
