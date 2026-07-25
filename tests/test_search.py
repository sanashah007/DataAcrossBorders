"""Federated search: fan-out, filtering, sorting, pagination, and study lookup."""

import pytest

TOTAL_RECORDS = 2700
PER_NODE = 900


def search(gateway, auth, **params):
    response = gateway.get("/api/studies", headers=auth, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_fan_out_merges_all_three_nodes(gateway, auth):
    body = search(gateway, auth, limit=1)
    assert body["total"] == TOTAL_RECORDS
    assert body["partial"] is False
    assert {(s["node"], s["status"], s["record_count"]) for s in body["sources"]} == {
        ("BCH", "ok", PER_NODE),
        ("MGH", "ok", PER_NODE),
        ("BWH", "ok", PER_NODE),
    }


def test_every_result_is_tagged_with_its_origin(gateway, auth):
    body = search(gateway, auth, limit=10)
    for record in body["results"]:
        assert record["SourceNode"] in {"BCH", "MGH", "BWH"}
        assert record["FederatedID"] == f"{record['SourceNode']}:{record['StudyID']}"


def test_free_text_search_spans_hospitals(gateway, auth):
    body = search(gateway, auth, q="hydrocephalus", limit=500)
    assert body["total"] > 0
    # The README promises a term like this returns hits from multiple nodes.
    assert len({r["SourceNode"] for r in body["results"]}) == 3
    for record in body["results"]:
        assert "hydrocephalus" in record["Diagnosis"].lower()


def test_free_text_search_is_case_insensitive(gateway, auth):
    lower = search(gateway, auth, q="hydrocephalus", limit=1)["total"]
    upper = search(gateway, auth, q="HYDROCEPHALUS", limit=1)["total"]
    assert lower == upper > 0


def test_body_part_filter(gateway, auth):
    body = search(gateway, auth, body_part="BRAIN", limit=1)
    assert body["total"] == PER_NODE  # 300 per node across three nodes


def test_node_scoping_prunes_the_fan_out(gateway, auth):
    body = search(gateway, auth, node="BCH", limit=1)
    assert body["total"] == PER_NODE
    statuses = {s["node"]: s["status"] for s in body["sources"]}
    assert statuses == {"BCH": "ok", "MGH": "skipped", "BWH": "skipped"}


def test_age_filter_isolates_the_pediatric_hospital(gateway, auth):
    body = search(gateway, auth, max_age=18, limit=500)
    assert body["total"] > 0
    assert {r["SourceNode"] for r in body["results"]} == {"BCH"}


def test_date_range_filter(gateway, auth):
    body = search(
        gateway, auth, study_date_from="20250101", study_date_to="20251231", limit=500
    )
    assert body["total"] > 0
    assert all(r["StudyDate"].startswith("2025") for r in body["results"])


def test_filters_compose(gateway, auth):
    both = search(gateway, auth, body_part="BRAIN", sex="F", limit=1)["total"]
    brain_only = search(gateway, auth, body_part="BRAIN", limit=1)["total"]
    assert 0 < both < brain_only


def test_total_is_post_filter_but_pre_pagination(gateway, auth):
    body = search(gateway, auth, body_part="BRAIN", limit=5)
    assert body["total"] == PER_NODE
    assert len(body["results"]) == 5


def test_pages_do_not_overlap_and_cover_the_set(gateway, auth):
    first = search(gateway, auth, limit=50, offset=0)
    second = search(gateway, auth, limit=50, offset=50)
    ids_a = [r["FederatedID"] for r in first["results"]]
    ids_b = [r["FederatedID"] for r in second["results"]]
    assert len(ids_a) == len(ids_b) == 50
    assert not set(ids_a) & set(ids_b)
    assert first["total"] == second["total"] == TOTAL_RECORDS


def test_pagination_is_stable_across_identical_requests(gateway, auth):
    a = search(gateway, auth, sort_by="StudyDate", limit=20, offset=40)
    b = search(gateway, auth, sort_by="StudyDate", limit=20, offset=40)
    assert [r["FederatedID"] for r in a["results"]] == [
        r["FederatedID"] for r in b["results"]
    ]


def test_offset_past_the_end_returns_an_empty_page(gateway, auth):
    body = search(gateway, auth, limit=10, offset=TOTAL_RECORDS + 100)
    assert body["total"] == TOTAL_RECORDS
    assert body["results"] == []


def test_age_sort_is_unit_aware(gateway, auth):
    body = search(gateway, auth, sort_by="PatientAge", order="asc", limit=5)
    ages = [r["PatientAge"] for r in body["results"]]
    # Youngest first means day-unit ages lead, despite sorting last as strings.
    assert all(age.endswith("D") for age in ages), ages


def test_sort_order_reverses(gateway, auth):
    asc = search(gateway, auth, sort_by="StudyDate", order="asc", limit=1)
    desc = search(gateway, auth, sort_by="StudyDate", order="desc", limit=1)
    assert asc["results"][0]["StudyDate"] < desc["results"][0]["StudyDate"]


@pytest.mark.parametrize(
    "params",
    [
        {"node": "NOPE"},
        {"limit": 0},
        {"limit": 100000},
        {"offset": -1},
        {"study_date_from": "not-a-date"},
        {"sort_by": "PatientBloodType"},
        {"order": "sideways"},
    ],
)
def test_invalid_query_parameters_are_rejected(gateway, auth, params):
    response = gateway.get("/api/studies", headers=auth, params=params)
    assert response.status_code in (400, 422), response.text


class TestSingleStudyLookup:
    def test_same_study_id_on_two_nodes_returns_different_patients(self, gateway, auth):
        # StudyID is NOT unique across the federation — this is the case the
        # node-qualified route exists to disambiguate.
        bch = gateway.get("/api/studies/BCH/BR-7214", headers=auth).json()
        mgh = gateway.get("/api/studies/MGH/BR-7214", headers=auth).json()
        assert bch["StudyID"] == mgh["StudyID"] == "BR-7214"
        assert bch["PatientID"] != mgh["PatientID"]
        assert bch["StudyInstanceUID"] != mgh["StudyInstanceUID"]
        assert bch["FederatedID"] == "BCH:BR-7214"
        assert mgh["FederatedID"] == "MGH:BR-7214"

    def test_node_name_is_case_insensitive(self, gateway, auth):
        assert gateway.get("/api/studies/bch/BR-7214", headers=auth).status_code == 200

    def test_unknown_study_is_404(self, gateway, auth):
        response = gateway.get("/api/studies/BCH/NOPE-0000", headers=auth)
        assert response.status_code == 404

    def test_unknown_node_is_400(self, gateway, auth):
        response = gateway.get("/api/studies/XXX/BR-7214", headers=auth)
        assert response.status_code == 400
        assert "BCH" in response.json()["detail"]

    def test_federated_id_from_search_resolves(self, gateway, auth):
        record = search(gateway, auth, limit=1)["results"][0]
        node, study_id = record["FederatedID"].split(":")
        response = gateway.get(f"/api/studies/{node}/{study_id}", headers=auth)
        assert response.status_code == 200
        assert response.json()["StudyInstanceUID"] == record["StudyInstanceUID"]


def test_nodes_endpoint_reports_the_registry(gateway, auth):
    body = gateway.get("/api/nodes", headers=auth).json()
    assert {n["node"] for n in body} == {"BCH", "MGH", "BWH"}
    assert all(n["reachable"] for n in body)
    assert {n["institution"] for n in body} == {
        "Boston Children's Hospital",
        "Massachusetts General Hospital",
        "Brigham and Women's Hospital",
    }
