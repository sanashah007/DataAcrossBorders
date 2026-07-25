from typing import List, Literal

from pydantic import BaseModel


class StudyRecord(BaseModel):
    PatientName: str
    PatientID: str
    PatientBirthDate: str
    PatientAge: str
    PatientSex: str
    InstitutionName: str
    StudyID: str
    StudyInstanceUID: str
    StudyDate: str
    Modality: str
    BodyPartExamined: str
    Diagnosis: str


class FindingTag(BaseModel):
    dimension: Literal["location", "finding_type", "size", "other"]
    value: str
    status: Literal["present", "absent", "uncertain"]


class LabeledStudyRecord(StudyRecord):
    GenericCategory: str
    FindingTags: List[FindingTag]
