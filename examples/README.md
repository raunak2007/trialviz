# Example runs

Produced by `python scripts/run_examples.py` against the live ClinicalTrials.gov API.
Each file in `outputs/` holds the request that was sent and the exact response body.

| # | file | HTTP | type | title | size | planner | retrieval | API calls | total ms |
|---|------|------|------|-------|------|---------|-----------|-----------|----------|
| 1 | `outputs/time_trend_pembrolizumab.json` | 200 | time_series | Pembrolizumab trials started per year, 2015 to 2026 | 12 rows | llm | Pembrolizumab trials: 2000 of 2905 fetched (exact_server_counts) | 14 | 6137 |
| 2 | `outputs/distribution_melanoma_phases.json` | 200 | bar_chart | Melanoma trials by phase | 8 rows | llm | melanoma trials: 2000 of 3140 fetched (exact_server_counts) | 10 | 5530 |
| 3 | `outputs/distribution_pembrolizumab_sponsor_class_strict.json` | 200 | bar_chart | Pembrolizumab trials by lead sponsor class | 8 rows | llm | pembrolizumab trials: 2979 of 2979 fetched (sampled) | 3 | 4867 |
| 4 | `outputs/comparison_nivolumab_vs_ipilimumab.json` | 200 | grouped_bar_chart | Trials by phase: Nivolumab vs Ipilimumab | 16 rows | llm | Nivolumab: 1816 of 1816 fetched (sampled); Ipilimumab: 893 of 893 fetched (sampled) | 3 | 4057 |
| 5 | `outputs/comparison_nivolumab_vs_ipilimumab_partition.json` | 200 | grouped_bar_chart | Trials by phase: Nivolumab vs Ipilimumab | 24 rows | llm | Nivolumab: 1816 of 1816 fetched (sampled); Ipilimumab: 893 of 893 fetched (sampled) | 3 | 2353 |
| 6 | `outputs/geographic_recruiting_t2d.json` | 200 | bar_chart | Countries with the most recruiting type 2 diabetes trials | 20 rows | llm | type 2 diabetes trials: 868 of 868 fetched (sampled) | 1 | 2656 |
| 7 | `outputs/network_sponsors_drugs_melanoma.json` | 200 | network_graph | Sponsor to drug network for melanoma trials | 47 nodes, 50 edges | llm | melanoma trials: 3776 of 3776 fetched (sampled) | 4 | 5316 |
| 8 | `outputs/network_drug_cooccurrence_lung_cancer.json` | 200 | network_graph | Drugs given in combination (same arm) in lung cancer trials | 25 nodes, 50 edges | llm | lung cancer trials: 2000 of 14625 fetched (sampled) | 2 | 3488 |
| 9 | `outputs/scatter_enrollment_breast_cancer.json` | 200 | scatter_plot | Enrollment (participants) vs start year for breast cancer trials | 500 rows | llm | breast cancer trials: 500 of 5346 fetched (sampled) | 1 | 2740 |
| 10 | `outputs/error_needs_clarification.json` | 422 | error: unsupported_query | Participant ages are not available through this backend, which only covers trial-level fields such as enrollment, start  |  |  |  |  |  |
