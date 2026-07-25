from typing import Optional

from gateway.schemas import FederatedStudy

# DICOM Age String (AS) units, expressed in years.
_AGE_UNITS = {"D": 1 / 365.25, "W": 7 / 365.25, "M": 1 / 12, "Y": 1.0}


def parse_dicom_age(value: str) -> Optional[float]:
    """'031Y' -> 31.0, '005D' -> ~0.0137. Returns None if unparseable.

    BCH mixes D/M/Y units (neonates through young adults); MGH and BWH are all Y.
    """
    if not value:
        return None
    unit = value[-1].upper()
    if unit not in _AGE_UNITS:
        return None
    try:
        magnitude = float(value[:-1])
    except ValueError:
        return None
    return magnitude * _AGE_UNITS[unit]


# The columns free-text `q` may search. A contract narrows this to the ones it
# releases — searching a column the caller cannot read turns the result count
# into an oracle for its value, which is the trap gateway/README.md warns about.
TEXT_COLUMNS: tuple[str, ...] = ("Diagnosis", "PatientName", "StudyID", "PatientID")


def _matches_text(
    record: FederatedStudy, needle: str, columns: tuple[str, ...] = TEXT_COLUMNS
) -> bool:
    return any(needle in getattr(record, column).lower() for column in columns)


def apply_filters(
    records: list[FederatedStudy],
    *,
    q: Optional[str] = None,
    body_part: Optional[list[str]] = None,
    modality: Optional[list[str]] = None,
    sex: Optional[str] = None,
    min_age: Optional[float] = None,
    max_age: Optional[float] = None,
    study_date_from: Optional[str] = None,
    study_date_to: Optional[str] = None,
    text_columns: tuple[str, ...] = TEXT_COLUMNS,
) -> list[FederatedStudy]:
    needle = q.lower() if q else None
    body_parts = {b.upper() for b in body_part} if body_part else None
    modalities = {m.upper() for m in modality} if modality else None
    sex_value = sex.upper() if sex else None

    result = []
    for record in records:
        if needle and not _matches_text(record, needle, text_columns):
            continue
        if body_parts and record.BodyPartExamined.upper() not in body_parts:
            continue
        if modalities and record.Modality.upper() not in modalities:
            continue
        if sex_value and record.PatientSex.upper() != sex_value:
            continue
        # StudyDate is YYYYMMDD, so string comparison is chronological.
        if study_date_from and record.StudyDate < study_date_from:
            continue
        if study_date_to and record.StudyDate > study_date_to:
            continue
        if min_age is not None or max_age is not None:
            age = parse_dicom_age(record.PatientAge)
            # An unparseable age can't satisfy an age filter, so drop it.
            if age is None:
                continue
            if min_age is not None and age < min_age:
                continue
            if max_age is not None and age > max_age:
                continue
        result.append(record)
    return result


_SORT_KEYS = {
    "StudyDate": lambda r: r.StudyDate,
    "PatientAge": lambda r: (parse_dicom_age(r.PatientAge) or -1.0),
    "InstitutionName": lambda r: r.InstitutionName,
    "StudyID": lambda r: r.StudyID,
    "FederatedID": lambda r: r.FederatedID,
}


def sort_records(
    records: list[FederatedStudy], sort_by: str, order: str
) -> list[FederatedStudy]:
    key = _SORT_KEYS[sort_by]
    # Secondary key keeps ordering stable when the primary key ties.
    return sorted(
        records, key=lambda r: (key(r), r.FederatedID), reverse=(order == "desc")
    )


def paginate(
    records: list[FederatedStudy], limit: int, offset: int
) -> list[FederatedStudy]:
    return records[offset : offset + limit]
