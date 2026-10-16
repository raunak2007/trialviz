"""Enumerations shared by the request, plan, and response schemas.

The ClinicalTrials.gov values (Phase, OverallStatus, StudyType, InterventionType,
SponsorClass) mirror the enumerated values the v2 API returns, so they can be
compared against raw API data without translation.
"""

from enum import StrEnum


class Phase(StrEnum):
    EARLY_PHASE1 = "EARLY_PHASE1"
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    NA = "NA"


class OverallStatus(StrEnum):
    NOT_YET_RECRUITING = "NOT_YET_RECRUITING"
    RECRUITING = "RECRUITING"
    ENROLLING_BY_INVITATION = "ENROLLING_BY_INVITATION"
    ACTIVE_NOT_RECRUITING = "ACTIVE_NOT_RECRUITING"
    COMPLETED = "COMPLETED"
    SUSPENDED = "SUSPENDED"
    TERMINATED = "TERMINATED"
    WITHDRAWN = "WITHDRAWN"
    AVAILABLE = "AVAILABLE"
    NO_LONGER_AVAILABLE = "NO_LONGER_AVAILABLE"
    TEMPORARILY_NOT_AVAILABLE = "TEMPORARILY_NOT_AVAILABLE"
    APPROVED_FOR_MARKETING = "APPROVED_FOR_MARKETING"
    WITHHELD = "WITHHELD"
    UNKNOWN = "UNKNOWN"


class StudyType(StrEnum):
    INTERVENTIONAL = "INTERVENTIONAL"
    OBSERVATIONAL = "OBSERVATIONAL"
    EXPANDED_ACCESS = "EXPANDED_ACCESS"


class InterventionType(StrEnum):
    DRUG = "DRUG"
    BIOLOGICAL = "BIOLOGICAL"
    DEVICE = "DEVICE"
    PROCEDURE = "PROCEDURE"
    RADIATION = "RADIATION"
    BEHAVIORAL = "BEHAVIORAL"
    GENETIC = "GENETIC"
    DIETARY_SUPPLEMENT = "DIETARY_SUPPLEMENT"
    COMBINATION_PRODUCT = "COMBINATION_PRODUCT"
    DIAGNOSTIC_TEST = "DIAGNOSTIC_TEST"
    OTHER = "OTHER"


class SponsorClass(StrEnum):
    NIH = "NIH"
    FED = "FED"
    OTHER_GOV = "OTHER_GOV"
    INDIV = "INDIV"
    INDUSTRY = "INDUSTRY"
    NETWORK = "NETWORK"
    AMBIG = "AMBIG"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class AnalysisType(StrEnum):
    """What kind of question is being asked. Drives retrieval and the chart type."""

    TIME_TREND = "time_trend"  # counts per start period, optionally split by a dimension
    DISTRIBUTION = "distribution"  # counts across a categorical dimension
    COMPARISON = "comparison"  # same dimension, two or more entity series side by side
    GEOGRAPHIC = "geographic"  # counts per country
    NETWORK = "network"  # co-occurrence graph between two entity kinds
    SCATTER = "scatter"  # one point per trial, two numeric fields
    HISTOGRAM = "histogram"  # binned distribution of one numeric field
    UNSUPPORTED = "unsupported"  # the question cannot be answered with this menu


class Dimension(StrEnum):
    """Categorical fields trials can be grouped by. Values double as data row keys."""

    PHASE = "phase"
    START_YEAR = "start_year"
    COUNTRY = "country"
    INTERVENTION_TYPE = "intervention_type"
    SPONSOR_CLASS = "sponsor_class"
    STATUS = "status"
    STUDY_TYPE = "study_type"
    CONDITION = "condition"
    SPONSOR = "sponsor"
    INTERVENTION = "intervention"


class Measure(StrEnum):
    TRIAL_COUNT = "trial_count"
    TOTAL_ENROLLMENT = "total_enrollment"


class NumericField(StrEnum):
    """Per-trial numeric fields usable on scatter axes and histograms."""

    ENROLLMENT = "enrollment"
    START_YEAR = "start_year"
    DURATION_MONTHS = "duration_months"
    SITE_COUNT = "site_count"


class NetworkKind(StrEnum):
    SPONSOR_DRUG = "sponsor_drug"  # bipartite: lead sponsors to drugs they test
    DRUG_DRUG = "drug_drug"  # drugs given in combination: listed in the same arm group
    DRUG_DRUG_SAME_TRIAL = "drug_drug_same_trial"  # drugs listed anywhere in the same trial, comparators included
    CONDITION_DRUG = "condition_drug"  # bipartite: conditions to drugs
    SPONSOR_CONDITION = "sponsor_condition"  # bipartite: lead sponsors to conditions
    SPONSOR_COLLABORATOR = "sponsor_collaborator"  # lead sponsor to collaborators


class TimeGranularity(StrEnum):
    YEAR = "year"
    QUARTER = "quarter"
    MONTH = "month"


class VizType(StrEnum):
    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    TIME_SERIES = "time_series"
    HISTOGRAM = "histogram"
    SCATTER_PLOT = "scatter_plot"
    NETWORK_GRAPH = "network_graph"
