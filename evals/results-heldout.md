# Planner evaluation (heldout set)

17 questions from `planner_questions_heldout.jsonl`, today pinned to 2026-10-15.

## rules

- plan matches: **9/17**
- intent matches (right analysis, grouping, and entities): **10/17**

| group | plan | intent |
|---|---|---|
| ho | 9/17 | 10/17 |

Misses:

- `ho-03` Phase breakdown of trials run by Gilead
  - planner error: unsupported_query: Could not tell what to visualize. Say what to group by (phase, year, country, sponsor, intervention type) or ask for a network, comparison, or histogram.
- `ho-05` What kinds of interventions are being tested for insomnia?
  - planner error: unsupported_query: Could not tell what to visualize. Say what to group by (phase, year, country, sponsor, intervention type) or ask for a network, comparison, or histogram.
- `ho-06` Enrollment totals by sponsor class for trials of ozempic
  - drugs: expected ['ozempic'], got []
  - terms: expected [], got ['ozempic']
- `ho-08` Crohn's disease versus ulcerative colitis: trials started per year since 2015
  - planner error: needs_clarification: A comparison needs at least two things to compare, for example 'drug A vs drug B'.
- `ho-09` In which countries are recruiting trials of tirzepatide taking place?
  - drugs: expected ['tirzepatide'], got ['tirzepatide taking place']
  - entities: expected ['tirzepatide'], got ['tirzepatide taking place']
- `ho-12` Which drugs are combined with nivolumab in the same arm of a trial?
  - analysis: expected network, got distribution
  - network: expected drug_drug, got None
- `ho-13` Which conditions and drugs are connected in trials sponsored by Bayer?
  - analysis: expected network, got distribution
  - network: expected condition_drug, got None
- `ho-14` Do longer ovarian cancer trials enroll more participants? Plot duration against enrollment
  - conditions: expected ['ovarian cancer'], got ['longer ovarian cancer']
  - entities: expected ['ovarian cancer'], got ['longer ovarian cancer']

## llm (claude-sonnet-5-5)

- plan matches: **17/17**
- intent matches (right analysis, grouping, and entities): **17/17**

| group | plan | intent |
|---|---|---|
| ho | 17/17 | 17/17 |
