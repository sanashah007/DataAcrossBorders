from typing import Literal, Optional

from pydantic import BaseModel

from models import StudyRecord


class FederatedStudy(StudyRecord):
    """A node's study record, tagged with where it came from.

    StudyID is not unique across nodes (BR-7214 exists on both BCH and MGH), so
    FederatedID is what callers should treat as the identifier.
    """

    SourceNode: str
    FederatedID: str


class NodeStatus(BaseModel):
    node: str
    base_url: str
    status: Literal["ok", "error", "timeout", "skipped"]
    record_count: int = 0
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class NodeInfo(BaseModel):
    node: str
    base_url: str
    institution: str
    reachable: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class SearchResponse(BaseModel):
    total: int  # post-filter, pre-pagination
    limit: int
    offset: int
    partial: bool  # true if any queried node failed — `total` is then incomplete
    sources: list[NodeStatus]
    results: list[FederatedStudy]


class StatsResponse(BaseModel):
    total_studies: int
    partial: bool
    by_hospital: dict[str, int]
    by_body_part: dict[str, int]
    by_modality: dict[str, int]
    by_sex: dict[str, dict[str, int]]
    hospital_by_body_part: dict[str, dict[str, int]]
    age_histogram: dict[str, int]
    studies_by_year: dict[str, int]
    sources: list[NodeStatus]


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class Principal(BaseModel):
    username: str
    role: str
