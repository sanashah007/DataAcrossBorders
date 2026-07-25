"""Aggregate statistics across the federation."""

TOTAL_RECORDS = 2700
PER_NODE = 900


def stats(gateway, auth, **params):
    response = gateway.get("/api/stats", headers=auth, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_totals_match_the_whole_federation(gateway, auth):
    body = stats(gateway, auth)
    assert body["total_studies"] == TOTAL_RECORDS
    assert body["partial"] is False
    assert body["by_hospital"] == {"BCH": PER_NODE, "MGH": PER_NODE, "BWH": PER_NODE}


def test_body_part_breakdown_is_evenly_split(gateway, auth):
    body = stats(gateway, auth)
    assert body["by_body_part"] == {"BRAIN": 900, "FETAL": 900, "HEART": 900}


def test_hospital_by_body_part_matrix(gateway, auth):
    matrix = stats(gateway, auth)["hospital_by_body_part"]
    assert set(matrix) == {"BCH", "MGH", "BWH"}
    for node, counts in matrix.items():
        assert counts == {"BRAIN": 300, "FETAL": 300, "HEART": 300}, node


def test_modality_is_degenerate_but_reported(gateway, auth):
    # Every record in this dataset is MR. Asserted so that a future dataset
    # with real modality variety fails here and prompts a doc update.
    assert stats(gateway, auth)["by_modality"] == {"MR": TOTAL_RECORDS}


def test_sex_breakdown_is_per_hospital_and_skewed(gateway, auth):
    by_sex = stats(gateway, auth)["by_sex"]
    assert set(by_sex) == {"BCH", "MGH", "BWH"}
    for node, counts in by_sex.items():
        assert set(counts) <= {"M", "F"}
        assert sum(counts.values()) == PER_NODE, node
    # BWH is the most female-skewed of the three.
    assert by_sex["BWH"]["F"] > by_sex["BCH"]["F"]


def test_age_histogram_covers_every_record(gateway, auth):
    histogram = stats(gateway, auth)["age_histogram"]
    assert sum(histogram.values()) == TOTAL_RECORDS
    assert "unknown" not in histogram  # all ages in this dataset parse
    assert histogram["<1"] > 0  # BCH's day-unit neonates land here


def test_studies_by_year(gateway, auth):
    by_year = stats(gateway, auth)["studies_by_year"]
    assert set(by_year) == {"2025", "2026"}
    assert sum(by_year.values()) == TOTAL_RECORDS


def test_stats_honour_the_same_filters_as_search(gateway, auth):
    filtered = stats(gateway, auth, body_part="BRAIN")
    assert filtered["total_studies"] == PER_NODE
    assert filtered["by_body_part"] == {"BRAIN": PER_NODE}


def test_filtered_stats_agree_with_search_total(gateway, auth):
    search_total = gateway.get(
        "/api/studies", headers=auth, params={"q": "hydrocephalus", "limit": 1}
    ).json()["total"]
    assert stats(gateway, auth, q="hydrocephalus")["total_studies"] == search_total


def test_node_scoping_applies_to_stats(gateway, auth):
    body = stats(gateway, auth, node="BCH")
    assert body["total_studies"] == PER_NODE
    assert body["by_hospital"] == {"BCH": PER_NODE}


def test_stats_leak_no_patient_identifiers(gateway, auth):
    # Counts only — /api/stats should never carry PII, regardless of what the
    # future access-control layer decides about /api/studies.
    raw = gateway.get("/api/stats", headers=auth).text
    for field in ("PatientName", "PatientID", "PatientBirthDate", "Diagnosis"):
        assert field not in raw
