from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

from gateway import client, config, filters
from gateway.auth import CurrentUser, authorize, authenticate_user, create_access_token
from gateway.schemas import (
    FederatedStudy,
    NodeInfo,
    SearchResponse,
    StatsResponse,
    Token,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One connection pool for the process, not one per request.
    async with httpx.AsyncClient() as http_client:
        app.state.http = http_client
        yield


app = FastAPI(
    title="Federation Gateway",
    description=(
        "Centralized search and aggregation layer over the BCH, MGH, and BWH "
        "hospital nodes. Queries every node concurrently on each request — there "
        "is no cache, so a node going down is immediately visible in the "
        "`sources` block of any response."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchParams:
    """Shared filter parameters for /api/studies and /api/stats."""

    def __init__(
        self,
        q: Annotated[
            Optional[str],
            Query(description="Free-text search over Diagnosis, PatientName, StudyID, PatientID."),
        ] = None,
        node: Annotated[
            Optional[list[str]],
            Query(description="Restrict to these nodes. Repeatable. Defaults to all."),
        ] = None,
        body_part: Annotated[
            Optional[list[str]],
            Query(description="BRAIN | FETAL | HEART. Repeatable."),
        ] = None,
        modality: Annotated[
            Optional[list[str]],
            Query(description="DICOM modality. Repeatable. Every record is currently MR."),
        ] = None,
        sex: Annotated[Optional[str], Query(description="M or F.")] = None,
        min_age: Annotated[
            Optional[float], Query(ge=0, description="Minimum patient age in years.")
        ] = None,
        max_age: Annotated[
            Optional[float], Query(ge=0, description="Maximum patient age in years.")
        ] = None,
        study_date_from: Annotated[
            Optional[str],
            Query(pattern=r"^\d{8}$", description="Inclusive lower bound, YYYYMMDD."),
        ] = None,
        study_date_to: Annotated[
            Optional[str],
            Query(pattern=r"^\d{8}$", description="Inclusive upper bound, YYYYMMDD."),
        ] = None,
    ):
        self.q = q
        self.nodes = _resolve_nodes(node)
        self.body_part = body_part
        self.modality = modality
        self.sex = sex
        self.min_age = min_age
        self.max_age = max_age
        self.study_date_from = study_date_from
        self.study_date_to = study_date_to

    def apply(self, records: list[FederatedStudy]) -> list[FederatedStudy]:
        return filters.apply_filters(
            records,
            q=self.q,
            body_part=self.body_part,
            modality=self.modality,
            sex=self.sex,
            min_age=self.min_age,
            max_age=self.max_age,
            study_date_from=self.study_date_from,
            study_date_to=self.study_date_to,
        )


def _federation_state(sources: list) -> bool:
    """Return `partial`, raising 503 if every queried node failed.

    Returning 200 with an empty result set when the whole federation is
    unreachable would look like "no matches" — a silent lie. A partial outage
    (some nodes up) stays 200 and is flagged via `partial`.
    """
    queried = [s for s in sources if s.status != "skipped"]
    failed = [s for s in queried if s.status != "ok"]
    if queried and len(failed) == len(queried):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "No hospital node could be reached.",
                "sources": [s.model_dump() for s in sources],
            },
        )
    return bool(failed)


def _resolve_nodes(requested: Optional[list[str]]) -> list[str]:
    if not requested:
        return list(config.NODES)
    resolved = [n.upper() for n in requested]
    unknown = [n for n in resolved if n not in config.NODES]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown node(s): {', '.join(unknown)}. "
            f"Valid nodes: {', '.join(config.NODES)}.",
        )
    return [n for n in config.NODES if n in resolved]


@app.post("/auth/token", response_model=Token, tags=["auth"])
def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    user = authenticate_user(form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(form_data.username, user["role"])
    return Token(access_token=token, expires_in=expires_in, role=user["role"])


@app.get("/health", tags=["meta"])
def health():
    """Gateway liveness only — deliberately independent of node reachability."""
    return {"status": "healthy", "service": "gateway", "nodes": list(config.NODES)}


@app.get("/api/nodes", response_model=list[NodeInfo], tags=["meta"])
async def list_nodes(request: Request, user: CurrentUser):
    return await client.probe_nodes(request.app.state.http)


@app.get("/api/studies", response_model=SearchResponse, tags=["studies"])
async def search_studies(
    request: Request,
    user: CurrentUser,
    params: Annotated[SearchParams, Depends()],
    sort_by: Annotated[
        Literal["StudyDate", "PatientAge", "InstitutionName", "StudyID", "FederatedID"],
        Query(),
    ] = "StudyDate",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Search across every hospital node at once.

    `total` is the post-filter, pre-pagination count. Read it alongside `sources`:
    if a node reported an error, its studies are simply absent from the total.
    """
    records, sources = await client.fetch_studies(request.app.state.http, params.nodes)
    partial = _federation_state(sources)
    records = authorize(user, records)
    matched = params.apply(records)
    page = filters.paginate(filters.sort_records(matched, sort_by, order), limit, offset)
    return SearchResponse(
        total=len(matched),
        limit=limit,
        offset=offset,
        partial=partial,
        sources=sources,
        results=page,
    )


@app.get(
    "/api/studies/{node}/{study_id}", response_model=FederatedStudy, tags=["studies"]
)
async def get_study(request: Request, user: CurrentUser, node: str, study_id: str):
    """Fetch one study from one node.

    The node is part of the path because StudyID is *not* unique across the
    federation — BR-7214 exists on both BCH and MGH and refers to different
    patients. Use the `FederatedID` from any search result to build this URL.
    """
    node = node.upper()
    if node not in config.NODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown node '{node}'. Valid nodes: {', '.join(config.NODES)}.",
        )

    base_url = config.NODES[node]["url"]
    try:
        response = await request.app.state.http.get(
            f"{base_url}/api/studies/{study_id}", timeout=config.NODE_TIMEOUT_SECONDS
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Node {node} is unreachable: {type(exc).__name__}: {exc}",
        )

    if response.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Study '{study_id}' not found on node {node}.",
        )
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Node {node} returned HTTP {response.status_code}.",
        )

    record = FederatedStudy(
        **response.json(), SourceNode=node, FederatedID=f"{node}:{study_id}"
    )
    permitted = authorize(user, [record])
    if not permitted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to view this study.",
        )
    return permitted[0]


# Upper bounds in years; the last bucket catches everything above.
_AGE_BUCKETS = [(1, "<1"), (5, "1-4"), (13, "5-12"), (18, "13-17"), (30, "18-29"),
                (50, "30-49"), (70, "50-69")]


def _age_bucket(age: Optional[float]) -> str:
    if age is None:
        return "unknown"
    for upper, label in _AGE_BUCKETS:
        if age < upper:
            return label
    return "70+"


@app.get("/api/stats", response_model=StatsResponse, tags=["stats"])
async def stats(
    request: Request, user: CurrentUser, params: Annotated[SearchParams, Depends()]
):
    """Aggregate counts across the federation, over the same filters as search.

    Note that `by_modality` is degenerate on the current dataset — every record is
    MR. It is reported anyway so the shape stays honest if richer data lands.
    """
    records, sources = await client.fetch_studies(request.app.state.http, params.nodes)
    partial = _federation_state(sources)
    records = authorize(user, records)
    matched = params.apply(records)

    by_sex: dict[str, Counter] = defaultdict(Counter)
    hospital_by_body_part: dict[str, Counter] = defaultdict(Counter)
    for record in matched:
        by_sex[record.SourceNode][record.PatientSex] += 1
        hospital_by_body_part[record.SourceNode][record.BodyPartExamined] += 1

    return StatsResponse(
        total_studies=len(matched),
        partial=partial,
        by_hospital=dict(Counter(r.SourceNode for r in matched)),
        by_body_part=dict(Counter(r.BodyPartExamined for r in matched)),
        by_modality=dict(Counter(r.Modality for r in matched)),
        by_sex={node: dict(counts) for node, counts in by_sex.items()},
        hospital_by_body_part={
            node: dict(counts) for node, counts in hospital_by_body_part.items()
        },
        age_histogram=dict(
            Counter(
                _age_bucket(filters.parse_dicom_age(r.PatientAge)) for r in matched
            )
        ),
        studies_by_year=dict(Counter(r.StudyDate[:4] for r in matched)),
        sources=sources,
    )
