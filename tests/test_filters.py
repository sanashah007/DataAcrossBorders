"""Unit tests for the pure filtering/sorting helpers — no HTTP, no fixtures."""

import pytest

from gateway.filters import apply_filters, paginate, parse_dicom_age, sort_records
from gateway.schemas import FederatedStudy


def make_study(**overrides) -> FederatedStudy:
    base = {
        "PatientName": "Doe^Jane",
        "PatientID": "CHB-00001",
        "PatientBirthDate": "20200101",
        "PatientAge": "005Y",
        "PatientSex": "F",
        "InstitutionName": "Boston Children's Hospital",
        "StudyID": "BR-0001",
        "StudyInstanceUID": "1.2.3.4",
        "StudyDate": "20250601",
        "Modality": "MR",
        "BodyPartExamined": "BRAIN",
        "Diagnosis": "Normal MR of the brain.",
        "SourceNode": "BCH",
        "FederatedID": "BCH:BR-0001",
    }
    base.update(overrides)
    return FederatedStudy(**base)


class TestParseDicomAge:
    @pytest.mark.parametrize(
        "value,expected",
        [("031Y", 31.0), ("001Y", 1.0), ("012M", 1.0), ("006M", 0.5)],
    )
    def test_parses_years_and_months(self, value, expected):
        assert parse_dicom_age(value) == pytest.approx(expected, rel=1e-3)

    def test_days_and_weeks_are_fractions_of_a_year(self):
        assert parse_dicom_age("005D") == pytest.approx(5 / 365.25, rel=1e-3)
        assert parse_dicom_age("002W") == pytest.approx(14 / 365.25, rel=1e-3)

    def test_a_newborn_is_younger_than_a_toddler(self):
        # The whole reason this function exists: "005D" < "002Y" numerically,
        # even though it sorts *after* as a plain string.
        assert parse_dicom_age("005D") < parse_dicom_age("002Y")
        assert "005D" > "002Y"

    @pytest.mark.parametrize("value", ["", "31", "abc", "031X", "XYZ Y", "Y"])
    def test_unparseable_input_returns_none(self, value):
        assert parse_dicom_age(value) is None


class TestApplyFilters:
    def test_free_text_is_case_insensitive_and_spans_fields(self):
        records = [
            make_study(StudyID="BR-1", Diagnosis="Severe HYDROCEPHALUS noted."),
            make_study(StudyID="BR-2", PatientName="Hydrocephalus^Bob"),
            make_study(StudyID="BR-3", Diagnosis="Normal study."),
        ]
        matched = apply_filters(records, q="hydrocephalus")
        assert {r.StudyID for r in matched} == {"BR-1", "BR-2"}

    def test_categorical_filters_are_case_insensitive(self):
        records = [
            make_study(StudyID="A", BodyPartExamined="BRAIN", PatientSex="F"),
            make_study(StudyID="B", BodyPartExamined="HEART", PatientSex="M"),
        ]
        assert [r.StudyID for r in apply_filters(records, body_part=["brain"])] == ["A"]
        assert [r.StudyID for r in apply_filters(records, sex="m")] == ["B"]

    def test_body_part_accepts_multiple_values(self):
        records = [
            make_study(StudyID="A", BodyPartExamined="BRAIN"),
            make_study(StudyID="B", BodyPartExamined="HEART"),
            make_study(StudyID="C", BodyPartExamined="FETAL"),
        ]
        matched = apply_filters(records, body_part=["BRAIN", "FETAL"])
        assert {r.StudyID for r in matched} == {"A", "C"}

    def test_date_range_bounds_are_inclusive(self):
        records = [
            make_study(StudyID="A", StudyDate="20250101"),
            make_study(StudyID="B", StudyDate="20250615"),
            make_study(StudyID="C", StudyDate="20251231"),
        ]
        matched = apply_filters(
            records, study_date_from="20250101", study_date_to="20251231"
        )
        assert len(matched) == 3
        narrowed = apply_filters(
            records, study_date_from="20250102", study_date_to="20251230"
        )
        assert [r.StudyID for r in narrowed] == ["B"]

    def test_age_range_compares_across_dicom_units(self):
        records = [
            make_study(StudyID="NEWBORN", PatientAge="005D"),
            make_study(StudyID="TODDLER", PatientAge="002Y"),
            make_study(StudyID="ADULT", PatientAge="045Y"),
        ]
        assert [r.StudyID for r in apply_filters(records, max_age=1)] == ["NEWBORN"]
        assert [r.StudyID for r in apply_filters(records, min_age=18)] == ["ADULT"]

    def test_unparseable_age_is_dropped_only_when_age_filtering(self):
        records = [make_study(StudyID="BAD", PatientAge="???")]
        assert len(apply_filters(records, sex="F")) == 1
        assert apply_filters(records, min_age=0) == []

    def test_filters_combine_as_and(self):
        records = [
            make_study(StudyID="A", BodyPartExamined="BRAIN", PatientSex="F"),
            make_study(StudyID="B", BodyPartExamined="BRAIN", PatientSex="M"),
        ]
        matched = apply_filters(records, body_part=["BRAIN"], sex="F")
        assert [r.StudyID for r in matched] == ["A"]

    def test_no_filters_returns_everything(self):
        records = [make_study(StudyID="A"), make_study(StudyID="B")]
        assert len(apply_filters(records)) == 2


class TestSortAndPaginate:
    def test_patient_age_sorts_numerically_not_lexically(self):
        records = [
            make_study(StudyID="ADULT", PatientAge="031Y", FederatedID="BCH:ADULT"),
            make_study(StudyID="BABY", PatientAge="005D", FederatedID="BCH:BABY"),
        ]
        ordered = sort_records(records, "PatientAge", "asc")
        assert [r.StudyID for r in ordered] == ["BABY", "ADULT"]

    def test_order_desc_reverses(self):
        records = [
            make_study(StudyID="A", StudyDate="20250101", FederatedID="BCH:A"),
            make_study(StudyID="B", StudyDate="20260101", FederatedID="BCH:B"),
        ]
        assert [r.StudyID for r in sort_records(records, "StudyDate", "desc")] == [
            "B",
            "A",
        ]

    def test_ties_break_deterministically_on_federated_id(self):
        # Without a tie-break, equal sort keys leave ordering at the mercy of
        # node arrival order, which makes pagination skip or repeat rows.
        records = [
            make_study(StudyID="Z", StudyDate="20250101", FederatedID="MGH:Z"),
            make_study(StudyID="A", StudyDate="20250101", FederatedID="BCH:A"),
        ]
        ordered = sort_records(records, "StudyDate", "asc")
        assert [r.FederatedID for r in ordered] == ["BCH:A", "MGH:Z"]
        assert ordered == sort_records(list(reversed(records)), "StudyDate", "asc")

    def test_paginate_slices_and_tolerates_overrun(self):
        records = [make_study(StudyID=str(i), FederatedID=f"BCH:{i}") for i in range(10)]
        assert [r.StudyID for r in paginate(records, limit=3, offset=0)] == ["0", "1", "2"]
        assert [r.StudyID for r in paginate(records, limit=3, offset=9)] == ["9"]
        assert paginate(records, limit=3, offset=99) == []
