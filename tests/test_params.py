from trialviz.ctgov.params import build_count_params, build_search_params, essie_date_range, essie_term, search_text
from trialviz.schemas.enums import OverallStatus, Phase, StudyType
from trialviz.schemas.plan import EntityFilter


def test_single_terms_pass_through_and_multiple_are_ored():
    assert search_text(["pembrolizumab"]) == "pembrolizumab"
    assert search_text(["breast cancer"]) == "breast cancer"
    assert search_text(["breast cancer", "melanoma"]) == "(breast cancer) OR melanoma"
    assert search_text(["", "  "]) == ""


def test_essie_quoting():
    assert essie_term("France") == "France"
    assert essie_term("United States") == '"United States"'
    assert essie_term("Korea, Republic of") == '"Korea, Republic of"'


def test_date_ranges():
    assert essie_date_range(2015, 2020) == "AREA[StartDate]RANGE[2015-01-01,2020-12-31]"
    assert essie_date_range(2015, None) == "AREA[StartDate]RANGE[2015-01-01,MAX]"
    assert essie_date_range(None, 2020) == "AREA[StartDate]RANGE[MIN,2020-12-31]"
    assert essie_date_range(None, None) == ""


def test_full_parameter_set():
    f = EntityFilter(
        drugs=["pembrolizumab"],
        conditions=["melanoma"],
        sponsors=["Merck"],
        countries=["United States", "France"],
        phases=[Phase.PHASE2, Phase.PHASE3],
        statuses=[OverallStatus.RECRUITING, OverallStatus.COMPLETED],
        study_types=[StudyType.INTERVENTIONAL],
        start_year=2015,
        end_year=2024,
    )
    params = build_search_params(f, page_size=500)
    assert params["query.intr"] == "pembrolizumab"
    assert params["query.cond"] == "melanoma"
    assert params["query.spons"] == "Merck"
    assert params["filter.overallStatus"] == "RECRUITING,COMPLETED"
    assert params["filter.advanced"] == (
        '(AREA[LocationCountry]"United States" OR AREA[LocationCountry]France) AND '
        "(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND AREA[StudyType]INTERVENTIONAL AND "
        "AREA[StartDate]RANGE[2015-01-01,2024-12-31]"
    )
    assert params["pageSize"] == "500"
    assert params["countTotal"] == "true"
    assert params["sort"] == "StartDate:desc"
    assert "NCTId" in params["fields"] and "LocationCountry" in params["fields"]


def test_empty_filter_has_no_query_keys():
    params = build_search_params(EntityFilter(), page_size=10)
    assert not any(k.startswith("query.") or k.startswith("filter.") for k in params)


def test_count_params_are_minimal_and_combine_extra_clause():
    params = build_count_params(EntityFilter(conditions=["melanoma"], start_year=2015), extra_advanced="AREA[Phase]PHASE3")
    assert params["pageSize"] == "1"
    assert params["fields"] == "NCTId"
    assert "sort" not in params
    assert params["filter.advanced"] == "AREA[StartDate]RANGE[2015-01-01,MAX] AND AREA[Phase]PHASE3"
