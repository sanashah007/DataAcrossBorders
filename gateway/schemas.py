from typing import Any, Literal, Optional

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
    """Per-node outcome for one federated request.

    `not_accepted` is a governance outcome, not a failure: the node is healthy
    and was simply never queried, because it has not agreed to serve data under
    the contract this request named. It is reported rather than hidden so a
    caller can tell "this hospital declined" from "this hospital is down".
    """

    node: str
    base_url: str
    status: Literal["ok", "error", "timeout", "skipped", "not_accepted"]
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
    """A page of federated results, projected down to what the contract releases.

    `results` is a list of plain objects rather than `FederatedStudy` because a
    contract omits columns entirely rather than blanking them, and every field on
    `FederatedStudy` is required. Which keys can appear is not fixed across
    contracts, so the per-contract shape is published at
    `GET /api/contracts/{ref}/schema` instead of in this model.
    """

    contract: str
    total: int  # post-filter, pre-pagination, and post-row-scope
    limit: int
    offset: int
    partial: bool  # true if any queried node failed or declined the contract
    sources: list[NodeStatus]
    results: list[dict[str, Any]]


class StatsResponse(BaseModel):
    """Aggregate counts, with each breakdown gated on the column it reads.

    An aggregate is `null` when the contract does not release its source column,
    and the column is listed in `suppressed`. Reporting the omission beats
    returning an empty dict, which would read as "no data" rather than "not
    permitted to compute this".
    """

    contract: str
    total_studies: int
    partial: bool
    #: Columns whose breakdowns were withheld because the contract denies them.
    suppressed: list[str] = []
    by_hospital: Optional[dict[str, int]] = None
    by_body_part: Optional[dict[str, int]] = None
    by_modality: Optional[dict[str, int]] = None
    by_sex: Optional[dict[str, dict[str, int]]] = None
    hospital_by_body_part: Optional[dict[str, dict[str, int]]] = None
    age_histogram: Optional[dict[str, int]] = None
    studies_by_year: Optional[dict[str, int]] = None
    sources: list[NodeStatus]


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class Principal(BaseModel):
    username: str
    role: str


class ContractInfo(BaseModel):
    """What a contract permits, as served by /api/contracts.

    This is the machine-readable summary of an agreement: the digest ties it to
    exact reviewed bytes, and the column lists say what can actually come out.
    """

    ref: str
    contract: str
    version: str
    title: str
    purpose: str
    digest: str
    expires: Optional[str] = None
    expired: bool = False
    row_scope: Optional[str] = None
    #: Released on every row — these may also be filtered and sorted on.
    columns: list[str]
    #: Released only on rows matching a predicate; never filterable.
    conditional_columns: dict[str, str] = {}
    #: Denied columns mapped to the reason the contract gives, where it gives one.
    denied_columns: dict[str, str] = {}
    #: Named by the contract but not served by any node yet (e.g. labeled data).
    unavailable_columns: list[str] = []
    accepted_by: list[str]
    granted: bool = False
