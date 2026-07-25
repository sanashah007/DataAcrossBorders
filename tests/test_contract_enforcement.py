"""Contract enforcement over real requests.

tests/test_contracts.py proves the engine decides correctly in isolation. These
tests prove the gateway actually asks it, on every route, and that nothing
routes around it.

They run against the contracts really shipped in contracts/, so a change there
that widens what leaves a hospital will fail here.
"""

import pytest

from gateway import main
from gateway.contracts import compile_contract
from gateway.contracts.spec import ContractSpec

FETAL_CARDIAC = "fetal-cardiac-outcomes@1.2.0"
POPULATION_HEALTH = "population-health@1.1.0"
FULL_ACCESS = "clinical-full-access@1.0.0"

IDENTIFIERS = ["PatientName", "PatientID", "PatientBirthDate", "StudyInstanceUID"]


@pytest.fixture
def researcher(auth_as):
    """A researcher, who holds the two de-identified contracts but not full access."""
    return auth_as("researcher")


def search(gateway, headers, contract, **params):
    response = gateway.get(
        "/api/studies", headers=headers, params={"contract": contract, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# Naming a contract
# --------------------------------------------------------------------------


class TestContractSelection:
    @pytest.mark.parametrize("path", ["/api/studies", "/api/stats"])
    def test_a_read_without_a_contract_is_refused(self, gateway, researcher, path):
        response = gateway.get(path, headers=researcher, params={"contract": ""})
        assert response.status_code == 400
        assert "contract" in response.text.lower()

    def test_the_refusal_lists_what_the_caller_may_use(self, gateway, researcher):
        response = gateway.get("/api/studies", headers=researcher, params={"contract": ""})
        assert response.json()["detail"]["available"] == [
            FETAL_CARDIAC,
            POPULATION_HEALTH,
        ]

    def test_an_unknown_contract_is_404(self, gateway, researcher):
        response = gateway.get(
            "/api/studies", headers=researcher, params={"contract": "ghost@1.0.0"}
        )
        assert response.status_code == 404

    def test_a_contract_the_user_lacks_is_403(self, gateway, researcher):
        response = gateway.get(
            "/api/studies", headers=researcher, params={"contract": FULL_ACCESS}
        )
        assert response.status_code == 403
        assert "not been granted" in response.text

    def test_missing_authentication_beats_missing_contract(self, gateway):
        # Dependency ordering: an anonymous caller must get 401, not a 400 that
        # confirms which contracts exist.
        assert gateway.get("/api/studies").status_code == 401


# --------------------------------------------------------------------------
# Column projection
# --------------------------------------------------------------------------


class TestColumnProjection:
    def test_denied_columns_are_absent_not_blank(self, gateway, researcher):
        body = search(gateway, researcher, FETAL_CARDIAC, limit=5)
        for row in body["results"]:
            for column in IDENTIFIERS:
                assert column not in row

    def test_released_columns_are_present(self, gateway, researcher):
        row = search(gateway, researcher, FETAL_CARDIAC, limit=1)["results"][0]
        assert {"PatientAge", "PatientSex", "BodyPartExamined", "StudyDate"} <= set(row)

    def test_a_column_no_rule_mentions_is_withheld(self, gateway, researcher):
        # StudyID is neither allowed nor denied by fetal-cardiac-outcomes.
        row = search(gateway, researcher, FETAL_CARDIAC, limit=1)["results"][0]
        assert "StudyID" not in row

    def test_conditional_column_appears_only_where_permitted(self, gateway, researcher):
        fetal = search(gateway, researcher, FETAL_CARDIAC, body_part="FETAL", limit=5)
        cardiac = search(gateway, researcher, FETAL_CARDIAC, body_part="HEART", limit=5)
        assert all("Diagnosis" in row for row in fetal["results"])
        assert all("Diagnosis" not in row for row in cardiac["results"])

    def test_population_health_releases_no_free_text_at_all(self, gateway, researcher):
        body = search(gateway, researcher, POPULATION_HEALTH, limit=20)
        for row in body["results"]:
            assert "Diagnosis" not in row
            assert "FederatedID" not in row  # embeds StudyID

    def test_full_access_still_returns_everything(self, gateway, auth_as):
        body = search(gateway, auth_as("clinician"), FULL_ACCESS, limit=1)
        assert set(IDENTIFIERS) <= set(body["results"][0])


# --------------------------------------------------------------------------
# Row scope
# --------------------------------------------------------------------------


class TestRowScope:
    def test_out_of_scope_rows_never_appear(self, gateway, researcher):
        body = search(gateway, researcher, FETAL_CARDIAC, limit=500)
        assert {r["BodyPartExamined"] for r in body["results"]} <= {"FETAL", "HEART"}

    def test_out_of_scope_rows_are_not_counted(self, gateway, researcher):
        # 300 FETAL + 300 HEART on each of the two accepting nodes; the 600
        # BRAIN studies are outside the scope and must not inflate `total`.
        assert search(gateway, researcher, FETAL_CARDIAC)["total"] == 1200

    def test_filtering_for_an_out_of_scope_value_finds_nothing(self, gateway, researcher):
        assert search(gateway, researcher, FETAL_CARDIAC, body_part="BRAIN")["total"] == 0

    def test_the_detail_route_404s_rather_than_403s_out_of_scope(self, gateway, researcher):
        # BR-1543 is a BCH BRAIN study — outside row_scope. A 403 here would
        # confirm the record exists, which is what the scope exists to hide.
        response = gateway.get(
            "/api/studies/BCH/BR-1543",
            headers=researcher,
            params={"contract": FETAL_CARDIAC},
        )
        assert response.status_code == 404

    def test_the_detail_route_projects_columns_too(self, gateway, researcher, auth_as):
        # The detail route bypasses the fan-out entirely, so it has to apply
        # projection on its own path. Locate a real in-scope study as a clinician
        # (who can see StudyID), then fetch that same study as a researcher.
        clinician = search(
            gateway, auth_as("clinician"), FULL_ACCESS, body_part="FETAL", node="BCH",
            limit=1,
        )["results"][0]

        response = gateway.get(
            f"/api/studies/BCH/{clinician['StudyID']}",
            headers=researcher,
            params={"contract": FETAL_CARDIAC},
        )
        assert response.status_code == 200, response.text
        row = response.json()

        assert not set(IDENTIFIERS) & set(row)
        assert row["BodyPartExamined"] == "FETAL"
        assert "Diagnosis" in row  # conditional release holds on a fetal study
        assert "StudyID" not in row


# --------------------------------------------------------------------------
# The redaction-bypass oracle
# --------------------------------------------------------------------------


class TestQueryOracle:
    def test_free_text_search_is_refused_when_no_text_column_is_released(
        self, gateway, researcher
    ):
        response = gateway.get(
            "/api/studies",
            headers=researcher,
            params={"contract": FETAL_CARDIAC, "q": "Harrington"},
        )
        assert response.status_code == 400
        assert response.json()["parameter"] == "q"

    @pytest.mark.parametrize(
        "params,column",
        [
            ({"sort_by": "StudyID"}, "StudyID"),
            ({"sort_by": "FederatedID"}, "FederatedID"),
        ],
    )
    def test_sorting_on_a_withheld_column_is_refused(
        self, gateway, researcher, params, column
    ):
        response = gateway.get(
            "/api/studies",
            headers=researcher,
            params={"contract": FETAL_CARDIAC, **params},
        )
        assert response.status_code == 400
        assert response.json()["column"] == column

    def test_a_default_sort_never_fails_a_caller_who_did_not_sort(
        self, gateway, researcher
    ):
        # population-health denies StudyID and FederatedID; the default sort must
        # fall back rather than 400 every plain request.
        assert search(gateway, researcher, POPULATION_HEALTH, limit=1)["results"]

    def test_filtering_on_a_released_column_still_works(self, gateway, researcher):
        body = search(gateway, researcher, FETAL_CARDIAC, sex="F", limit=5)
        assert all(row["PatientSex"] == "F" for row in body["results"])

    def test_the_refusal_names_the_parameter_and_column(self, gateway, researcher):
        response = gateway.get(
            "/api/studies",
            headers=researcher,
            params={"contract": FETAL_CARDIAC, "sort_by": "StudyID"},
        )
        body = response.json()
        assert body["parameter"] == "sort_by" and body["column"] == "StudyID"
        assert "does not release" in body["detail"]


# --------------------------------------------------------------------------
# Hospital acceptance
# --------------------------------------------------------------------------


class TestAcceptance:
    def test_a_hospital_that_declined_is_reported_not_hidden(self, gateway, researcher):
        body = search(gateway, researcher, FETAL_CARDIAC, limit=1)
        statuses = {s["node"]: s["status"] for s in body["sources"]}
        assert statuses["BWH"] == "not_accepted"
        assert statuses["BCH"] == "ok" and statuses["MGH"] == "ok"

    def test_declining_makes_the_result_partial(self, gateway, researcher):
        assert search(gateway, researcher, FETAL_CARDIAC, limit=1)["partial"] is True

    def test_no_rows_come_from_a_declining_hospital(self, gateway, researcher):
        body = search(gateway, researcher, FETAL_CARDIAC, limit=500)
        assert "BWH" not in {row["SourceNode"] for row in body["results"]}

    def test_a_different_contract_excludes_a_different_hospital(self, gateway, researcher):
        body = search(gateway, researcher, POPULATION_HEALTH, limit=1)
        statuses = {s["node"]: s["status"] for s in body["sources"]}
        assert statuses["MGH"] == "not_accepted"
        assert statuses["BCH"] == "ok" and statuses["BWH"] == "ok"

    def test_asking_only_a_declining_hospital_is_403_not_an_empty_200(
        self, gateway, researcher
    ):
        # An empty 200 would read as "no matching studies", which is false —
        # the hospital has data and declined to serve it under this agreement.
        response = gateway.get(
            "/api/studies",
            headers=researcher,
            params={"contract": FETAL_CARDIAC, "node": "BWH"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["accepted_by"] == ["BCH", "MGH"]

    def test_the_detail_route_refuses_a_declining_hospital(self, gateway, researcher):
        response = gateway.get(
            "/api/studies/BWH/BR-8934",
            headers=researcher,
            params={"contract": FETAL_CARDIAC},
        )
        assert response.status_code == 403

    def test_a_declining_hospital_does_not_trigger_the_outage_503(
        self, make_gateway, auth_as
    ):
        # BWH declined this contract and MGH is down; BCH alone must still answer.
        gateway = make_gateway(down={"MGH"})
        headers = {
            "Authorization": "Bearer "
            + gateway.post(
                "/auth/token", data={"username": "researcher", "password": "researcher"}
            ).json()["access_token"]
        }
        body = search(gateway, headers, FETAL_CARDIAC, limit=1)
        statuses = {s["node"]: s["status"] for s in body["sources"]}
        assert statuses == {"BCH": "ok", "MGH": "error", "BWH": "not_accepted"}


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def stats(gateway, headers, contract, **params):
    response = gateway.get(
        "/api/stats", headers=headers, params={"contract": contract, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestStats:
    def test_aggregates_over_released_columns_are_computed(self, gateway, researcher):
        body = stats(gateway, researcher, POPULATION_HEALTH)
        assert body["by_sex"] and body["age_histogram"]
        assert body["suppressed"] == []

    def test_row_scope_applies_to_counts(self, gateway, researcher):
        assert stats(gateway, researcher, FETAL_CARDIAC)["total_studies"] == 1200

    def test_a_declining_hospital_is_absent_from_the_breakdown(self, gateway, researcher):
        assert "BWH" not in stats(gateway, researcher, FETAL_CARDIAC)["by_hospital"]

    def test_breakdowns_are_suppressed_when_the_column_is_denied(
        self, gateway, researcher, monkeypatch
    ):
        """Counting is reading: a sex breakdown discloses PatientSex.

        No shipped contract denies a column that stats reads, so this installs
        one rather than leaving the suppression path unexercised.
        """
        spec = ContractSpec.model_validate(
            {
                "contract": "no-demographics",
                "version": "1.0.0",
                "title": "No demographics",
                "purpose": "Testing suppression.",
                "rules": [
                    {"deny": ["PatientSex", "PatientAge"]},
                    {"allow": ["SourceNode", "BodyPartExamined", "StudyDate", "Modality"]},
                ],
            }
        )
        compiled = compile_contract(spec, "sha256:test")
        compiled = type(compiled)(
            spec=compiled.spec,
            digest=compiled.digest,
            released=compiled.released,
            denied=compiled.denied,
            row_scope=compiled.row_scope,
            accepted_nodes=frozenset({"BCH", "MGH", "BWH"}),
        )
        monkeypatch.setitem(main.REGISTRY.contracts, "no-demographics@1.0.0", compiled)
        monkeypatch.setitem(
            main.REGISTRY.grants, "researcher", frozenset({"no-demographics@1.0.0"})
        )

        body = stats(gateway, researcher, "no-demographics@1.0.0")
        assert body["by_sex"] is None
        assert body["age_histogram"] is None
        assert sorted(body["suppressed"]) == ["PatientAge", "PatientSex"]
        # Breakdowns over released columns are unaffected.
        assert body["by_body_part"]
        assert body["total_studies"] == 2700

    def test_stats_never_carry_identifiers_under_any_contract(self, gateway, auth_as):
        for user, contract in [
            ("clinician", FULL_ACCESS),
            ("researcher", FETAL_CARDIAC),
            ("researcher", POPULATION_HEALTH),
        ]:
            body = stats(gateway, auth_as(user), contract)
            assert not set(IDENTIFIERS) & set(body)


# --------------------------------------------------------------------------
# k-anonymity
# --------------------------------------------------------------------------


class TestKAnonymity:
    """population-health@1.1.0 promises 5-anonymity over
    [PatientSex, InstitutionName, BodyPartExamined, PatientAge].
    """

    def test_a_unique_individual_cannot_be_reached(self, gateway, researcher):
        # Exactly one record in the dataset is a 34-year-old female with a fetal
        # study at BCH. Without k-anonymity this query returns her row and her
        # fetus's diagnosis; with it, she does not exist.
        body = search(
            gateway, researcher, POPULATION_HEALTH,
            node="BCH", sex="F", body_part="FETAL", min_age=34, max_age=34,
        )
        assert body["total"] == 0
        assert body["results"] == []
        assert body["k_anonymity"]["rows_withheld"] is True

    def test_a_large_cohort_passes_through_untouched(self, gateway, researcher):
        # 56 twenty-year-old females with fetal studies at BCH — each hidden
        # among the others, so nothing is withheld.
        body = search(
            gateway, researcher, POPULATION_HEALTH,
            node="BCH", sex="F", body_part="FETAL", min_age=20, max_age=20,
        )
        assert body["total"] >= 5
        assert body["k_anonymity"]["rows_withheld"] is False

    def test_total_counts_only_released_rows(self, gateway, researcher):
        # BCH and BWH accepted this contract, so 1800 rows are in scope; `total`
        # must report what survived suppression, not what matched.
        body = search(gateway, researcher, POPULATION_HEALTH, limit=1)
        assert 0 < body["total"] < 1800
        assert body["k_anonymity"]["rows_withheld"] is True

    def test_the_notice_never_reveals_how_many_rows_were_withheld(
        self, gateway, researcher
    ):
        # A count would hand the caller `returned + withheld` — the exact number
        # of people matching their filter, including the answer "one".
        notice = search(gateway, researcher, POPULATION_HEALTH, limit=1)["k_anonymity"]
        assert set(notice) == {"k", "quasi_identifiers", "rows_withheld", "cells_suppressed"}
        # The only number in the block is the threshold itself. `bool` subclasses
        # `int`, so the flags have to be excluded explicitly.
        numbers = {
            key: value
            for key, value in notice.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        assert numbers == {"k": 5}

    def test_contracts_without_the_promise_carry_no_notice(self, gateway, researcher):
        assert search(gateway, researcher, FETAL_CARDIAC, limit=1)["k_anonymity"] is None

    def test_every_released_row_belongs_to_a_group_of_at_least_k(
        self, gateway, researcher
    ):
        """The actual guarantee, checked against what really came back."""
        import collections

        body = search(gateway, researcher, POPULATION_HEALTH, limit=500)
        qis = body["k_anonymity"]["quasi_identifiers"]
        groups = collections.Counter(
            tuple(row[q] for q in qis) for row in body["results"]
        )
        # The page is a slice of the released table, so a group can be cut by
        # pagination; what must never appear is a group the *contract* would
        # have suppressed. Re-query each thin group to confirm it is only thin
        # because of the page boundary.
        assert all(row[q] is not None for row in body["results"] for q in qis)
        assert groups  # sanity: something came back
        thin = [key for key, count in groups.items() if count < 5]
        for sex, _institution, body_part, age in thin:
            regrouped = search(
                gateway, researcher, POPULATION_HEALTH,
                sex=sex, body_part=body_part, limit=500,
            )
            matching = [r for r in regrouped["results"] if r["PatientAge"] == age]
            assert len(matching) >= 5, (sex, body_part, age, len(matching))


class TestKAnonymityBlocksSingleStudyLookup:
    def test_the_detail_route_is_refused_outright(self, gateway, researcher, auth_as):
        study = search(
            gateway, auth_as("clinician"), FULL_ACCESS, node="BCH", limit=1
        )["results"][0]
        response = gateway.get(
            f"/api/studies/BCH/{study['StudyID']}",
            headers=researcher,
            params={"contract": POPULATION_HEALTH},
        )
        assert response.status_code == 403
        assert "one row is a group of one" in response.json()["detail"]

    def test_the_refusal_is_identical_for_a_study_that_does_not_exist(
        self, gateway, researcher, auth_as
    ):
        # Refusing before the node is contacted is what stops this route being
        # an existence oracle: real and imaginary studies look the same.
        real = search(
            gateway, auth_as("clinician"), FULL_ACCESS, node="BCH", limit=1
        )["results"][0]["StudyID"]

        responses = [
            gateway.get(
                f"/api/studies/BCH/{study_id}",
                headers=researcher,
                params={"contract": POPULATION_HEALTH},
            )
            for study_id in (real, "NOT-A-REAL-STUDY-99999")
        ]
        assert {r.status_code for r in responses} == {403}
        assert responses[0].json() == responses[1].json()


class TestKAnonymityInStats:
    def test_counts_describe_the_released_dataset_not_the_hidden_one(
        self, gateway, researcher
    ):
        # /api/stats and /api/studies must agree: a row suppressed from search
        # cannot be counted in the statistics describing that same search.
        body = stats(gateway, researcher, POPULATION_HEALTH)
        searched = search(gateway, researcher, POPULATION_HEALTH, limit=1)
        assert body["total_studies"] == searched["total"]
        assert body["k_anonymity"]["rows_withheld"] is True

    def test_no_reported_cell_is_smaller_than_k(self, gateway, researcher):
        body = stats(gateway, researcher, POPULATION_HEALTH)
        for breakdown in ("by_hospital", "by_body_part", "by_modality", "age_histogram"):
            for label, count in (body[breakdown] or {}).items():
                assert count >= 5, (breakdown, label, count)
        for node, counts in (body["by_sex"] or {}).items():
            for label, count in counts.items():
                assert count >= 5, (node, label, count)

    def test_stats_under_a_non_k_contract_are_unaffected(self, gateway, researcher):
        assert stats(gateway, researcher, FETAL_CARDIAC)["k_anonymity"] is None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


class TestContractDiscovery:
    def test_listing_shows_only_what_the_caller_may_use(self, gateway, researcher):
        refs = [c["ref"] for c in gateway.get("/api/contracts", headers=researcher).json()]
        assert refs == [FETAL_CARDIAC, POPULATION_HEALTH]

    def test_a_clinician_sees_a_different_list(self, gateway, auth_as):
        refs = [
            c["ref"]
            for c in gateway.get("/api/contracts", headers=auth_as("clinician")).json()
        ]
        assert refs == [FULL_ACCESS]

    def test_detail_reports_the_digest_and_the_column_split(self, gateway, researcher):
        body = gateway.get(f"/api/contracts/{FETAL_CARDIAC}", headers=researcher).json()
        assert body["digest"].startswith("sha256:")
        assert body["accepted_by"] == ["BCH", "MGH"]
        assert list(body["conditional_columns"]) == ["Diagnosis"]
        assert set(body["denied_columns"]) == set(IDENTIFIERS)

    def test_terms_are_readable_without_a_grant(self, gateway, researcher):
        # Reading what a contract says is not the same as being able to use it.
        body = gateway.get(f"/api/contracts/{FULL_ACCESS}", headers=researcher).json()
        assert body["granted"] is False
        assert body["purpose"]

    def test_the_published_schema_matches_what_is_actually_returned(
        self, gateway, researcher
    ):
        schema = gateway.get(
            f"/api/contracts/{FETAL_CARDIAC}/schema", headers=researcher
        ).json()
        allowed = set(schema["items"]["properties"])
        assert schema["items"]["additionalProperties"] is False

        body = search(gateway, researcher, FETAL_CARDIAC, limit=50)
        for row in body["results"]:
            assert set(row) <= allowed, set(row) - allowed
        # Every column the schema calls required is on every row.
        for row in body["results"]:
            assert set(schema["items"]["required"]) <= set(row)

    def test_unavailable_labeled_columns_are_declared_not_hidden(
        self, gateway, researcher
    ):
        body = gateway.get(
            f"/api/contracts/{POPULATION_HEALTH}", headers=researcher
        ).json()
        # The labeling pipeline has not run, so these are promised but absent.
        assert set(body["unavailable_columns"]) == {"GenericCategory", "FindingTags"}
