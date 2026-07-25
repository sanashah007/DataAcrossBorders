from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm

from gateway import client, config, filters
from gateway.auth import (
    CurrentUser,
    authenticate_user,
    authorize,
    create_access_token,
    project,
)
from gateway.contracts import CompiledContract, QueryNotPermitted, load_registry
from gateway.contracts.spec import GATEWAY_COLUMNS, NODE_COLUMNS
from gateway.schemas import (
    ContractInfo,
    FederatedStudy,
    NodeInfo,
    NodeStatus,
    SearchResponse,
    StatsResponse,
    Token,
)

# Contracts are loaded once, at import, and validated against the columns the
# federation can actually serve. Loading here rather than as a build step keeps
# the YAML the single authority — the digests hospitals pinned are taken over
# these exact bytes — and lets validation see the live schema. A contract that
# fails to load is skipped loudly; it never stops the process.
SERVED_COLUMNS = NODE_COLUMNS | GATEWAY_COLUMNS
REGISTRY = load_registry(config.CONTRACTS_DIR, available_columns=SERVED_COLUMNS)


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


@app.exception_handler(QueryNotPermitted)
async def _query_not_permitted(request: Request, exc: QueryNotPermitted):
    """A query that reads a column the contract withholds is a 400, not a 403.

    The request is well-formed and the caller is entitled to the contract; the
    query itself is the problem, so the response names the parameter and column
    at fault rather than saying "forbidden" and leaving them guessing.
    """
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": exc.detail,
            "parameter": exc.parameter,
            "column": exc.column,
        },
    )


def get_contract(
    user: CurrentUser,
    contract: Annotated[
        Optional[str],
        Query(
            description=(
                "The data contract to query under, as `id@version` "
                "(e.g. `population-health@1.0.0`). Required: every read is "
                "conducted under exactly one agreement. See GET /api/contracts."
            )
        ),
    ] = None,
) -> CompiledContract:
    """Resolve `?contract=` into an enforceable policy, or refuse the request.

    Declared `Optional` with a manual 400 rather than as a required query
    parameter so that a missing token still fails as 401 before a missing
    contract fails as 400 — FastAPI would otherwise reject the request at
    validation time, before authentication runs.
    """
    if not contract:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": (
                    "No data contract specified. Every read is conducted under "
                    "exactly one contract; pass ?contract=<id>@<version>."
                ),
                "available": sorted(REGISTRY.granted_to(user.username)),
            },
        )

    compiled = REGISTRY.get(contract)
    if compiled is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown contract '{contract}'. Known contracts: "
            f"{', '.join(REGISTRY.refs()) or 'none loaded'}.",
        )

    if not REGISTRY.is_granted(user.username, contract):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"'{user.username}' has not been granted contract '{contract}'.",
        )

    if compiled.expired:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Contract '{contract}' expired on {compiled.spec.expires}.",
        )

    return compiled


ContractDep = Annotated[CompiledContract, Depends(get_contract)]


class SearchParams:
    """Shared filter parameters for /api/studies and /api/stats.

    Contract enforcement happens here, during dependency resolution, so an
    impermissible query is refused before any hospital is contacted.
    """

    def __init__(
        self,
        contract: ContractDep,
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
        self.contract = contract
        self.q = q
        self.nodes, self.declined = _resolve_nodes(node, contract)
        self.body_part = body_part
        self.modality = modality
        self.sex = sex
        self.min_age = min_age
        self.max_age = max_age
        self.study_date_from = study_date_from
        self.study_date_to = study_date_to

        # Raises QueryNotPermitted (-> 400) if any of these read a column the
        # contract does not release on every row.
        contract.validate_query(
            q=q,
            body_part=body_part,
            modality=modality,
            sex=sex,
            min_age=min_age,
            max_age=max_age,
            study_date_from=study_date_from,
            study_date_to=study_date_to,
        )

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
            # `q` searches only what the contract releases; searching a hidden
            # column would make the result count an oracle for its value.
            text_columns=self.contract.searchable_columns(),
        )


def _federation_state(sources: list[NodeStatus]) -> bool:
    """Return `partial`, raising 503 if every *queried* node failed.

    Returning 200 with an empty result set when the whole federation is
    unreachable would look like "no matches" — a silent lie. A partial outage
    (some nodes up) stays 200 and is flagged via `partial`.

    Nodes that declined the contract were never queried, so they are not
    failures and cannot trigger the 503. They do make the result partial: the
    caller is seeing less than the whole federation, and for the same reason
    they need to know it.
    """
    queried = [s for s in sources if s.status not in ("skipped", "not_accepted")]
    failed = [s for s in queried if s.status != "ok"]
    if queried and len(failed) == len(queried):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "No hospital node could be reached.",
                "sources": [s.model_dump() for s in sources],
            },
        )
    declined = any(s.status == "not_accepted" for s in sources)
    return bool(failed) or declined


def _resolve_nodes(
    requested: Optional[list[str]], contract: CompiledContract
) -> tuple[list[str], list[str]]:
    """Split the requested nodes into those that will be queried and those that declined.

    A hospital that has not accepted this contract is not asked for data. If
    that leaves nobody, the request is refused with 403 rather than an empty
    200: no hospital agreed to this, which is a governance answer, not "no
    matching studies".
    """
    if not requested:
        candidates = list(config.NODES)
    else:
        resolved = [n.upper() for n in requested]
        unknown = [n for n in resolved if n not in config.NODES]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown node(s): {', '.join(unknown)}. "
                f"Valid nodes: {', '.join(config.NODES)}.",
            )
        candidates = [n for n in config.NODES if n in resolved]

    queryable = [n for n in candidates if n in contract.accepted_nodes]
    declined = [n for n in candidates if n not in contract.accepted_nodes]

    if not queryable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": (
                    f"No requested hospital has agreed to serve data under contract "
                    f"'{contract.ref}'."
                ),
                "requested": candidates,
                "accepted_by": sorted(contract.accepted_nodes),
            },
        )
    return queryable, declined


def _mark_declined(sources: list[NodeStatus], declined: list[str]) -> list[NodeStatus]:
    """Relabel un-queried nodes that declined the contract.

    `client.fetch_studies` reports everything it did not query as "skipped".
    That conflates "you filtered this node out" with "this hospital declined
    your contract", and only the second is a governance fact worth surfacing.
    """
    declined_set = set(declined)
    return [
        source.model_copy(update={"status": "not_accepted"})
        if source.node in declined_set
        else source
        for source in sources
    ]


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


def _contract_info(compiled: CompiledContract, username: str) -> ContractInfo:
    return ContractInfo(
        ref=compiled.ref,
        contract=compiled.spec.contract,
        version=compiled.spec.version,
        title=compiled.spec.title,
        purpose=compiled.spec.purpose.strip(),
        digest=compiled.digest,
        expires=str(compiled.spec.expires) if compiled.spec.expires else None,
        expired=compiled.expired,
        row_scope=compiled.row_scope.source if compiled.row_scope else None,
        columns=sorted(compiled.unconditional_columns),
        conditional_columns={
            column: predicate.source
            for column, predicate in sorted(compiled.released.items())
            if predicate is not None
        },
        denied_columns=dict(sorted(compiled.denied.items())),
        unavailable_columns=sorted(compiled.unavailable),
        accepted_by=sorted(compiled.accepted_nodes),
        granted=REGISTRY.is_granted(username, compiled.ref),
    )


@app.get("/api/contracts", response_model=list[ContractInfo], tags=["contracts"])
def list_contracts(user: CurrentUser):
    """Every contract this user may query under.

    Contracts they hold no grant for are omitted rather than listed as
    forbidden — the point of this route is "what can I ask for", and a catalogue
    of things you cannot use is noise.
    """
    return [
        _contract_info(compiled, user.username)
        for ref, compiled in sorted(REGISTRY.contracts.items())
        if REGISTRY.is_granted(user.username, ref)
    ]


@app.get("/api/contracts/{ref}", response_model=ContractInfo, tags=["contracts"])
def get_contract_info(user: CurrentUser, ref: str):
    """One contract in full: its terms, its digest, and what it releases.

    Readable without a grant. A contract is a governance document, not a secret,
    and being able to read the terms before requesting access is the point.
    """
    compiled = REGISTRY.get(ref)
    if compiled is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown contract '{ref}'. Known contracts: "
            f"{', '.join(REGISTRY.refs()) or 'none loaded'}.",
        )
    return _contract_info(compiled, user.username)


@app.get("/api/contracts/{ref}/schema", tags=["contracts"])
def get_contract_schema(user: CurrentUser, ref: str):
    """The JSON Schema of what a search under this contract can return.

    Generated from the contract rather than hand-maintained, so it cannot drift
    from what is enforced. `SearchResponse.results` is untyped precisely because
    the shape varies per contract; this is where that shape is published.

    `additionalProperties: false` makes it usable as an independent check: a
    caller, an auditor, or a test can validate a real response against this with
    any off-the-shelf validator and catch a column that should not be there.
    """
    compiled = REGISTRY.get(ref)
    if compiled is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown contract '{ref}'.",
        )

    properties = {
        column: {**_column_type(column), "description": _column_note(compiled, column)}
        for column in sorted(compiled.max_columns)
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:contract:{compiled.ref}",
        "title": f"{compiled.spec.title} — permitted output",
        "description": (
            f"Rows released under contract {compiled.ref} "
            f"(digest {compiled.digest}). Only columns listed as required are "
            f"present on every row; the rest are released conditionally."
        ),
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": sorted(compiled.unconditional_columns & compiled.max_columns
                               - compiled.unavailable),
        },
    }


def _column_type(column: str) -> dict[str, Any]:
    """JSON Schema type for a column.

    Every DICOM field is a string, including dates and ages. The labeling
    pipeline's FindingTags is the one structured column — see models.FindingTag.
    """
    if column != "FindingTags":
        return {"type": "string"}
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "dimension": {
                    "enum": ["location", "finding_type", "size", "other"]
                },
                "value": {"type": "string"},
                "status": {"enum": ["present", "absent", "uncertain"]},
            },
            "required": ["dimension", "value", "status"],
        },
    }


def _column_note(compiled: CompiledContract, column: str) -> str:
    predicate = compiled.released.get(column)
    if predicate is not None:
        return f"Released only where: {predicate.source}"
    if column in compiled.unavailable:
        return "Released by this contract, but no node serves it yet."
    return "Released on every row."


@app.get("/api/nodes", response_model=list[NodeInfo], tags=["meta"])
async def list_nodes(request: Request, user: CurrentUser):
    return await client.probe_nodes(request.app.state.http)


@app.get("/api/studies", response_model=SearchResponse, tags=["studies"])
async def search_studies(
    request: Request,
    user: CurrentUser,
    params: Annotated[SearchParams, Depends()],
    sort_by: Annotated[
        Optional[
            Literal[
                "StudyDate", "PatientAge", "InstitutionName", "StudyID", "FederatedID"
            ]
        ],
        Query(description="Defaults to a column the contract releases."),
    ] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Search every hospital that accepted this contract, at once.

    `total` is the post-row-scope, post-filter, pre-pagination count. Read it
    alongside `sources`: a node that errored or declined the contract has its
    studies simply absent from the total, and `partial` is then true.

    Which columns come back depends entirely on the contract — see
    `GET /api/contracts/{ref}` for the shape to expect.
    """
    contract = params.contract

    # An explicit sort on a withheld column is an error; the *default* sort must
    # not be, or a contract that denies StudyDate would 400 every plain request.
    if sort_by is not None:
        contract.validate_query(sort_by=sort_by)
    sort_column = sort_by or contract.default_sort()

    records, sources = await client.fetch_studies(request.app.state.http, params.nodes)
    sources = _mark_declined(sources, params.declined)
    partial = _federation_state(sources)

    records = authorize(user, contract, records)
    matched = params.apply(records)
    ordered = (
        filters.sort_records(matched, sort_column, order) if sort_column else matched
    )
    page = filters.paginate(ordered, limit, offset)

    return SearchResponse(
        contract=contract.ref,
        total=len(matched),
        limit=limit,
        offset=offset,
        partial=partial,
        sources=sources,
        results=project(contract, page),
    )


@app.get(
    "/api/studies/{node}/{study_id}", response_model=dict[str, Any], tags=["studies"]
)
async def get_study(
    request: Request,
    user: CurrentUser,
    contract: ContractDep,
    node: str,
    study_id: str,
):
    """Fetch one study from one node, projected to what the contract releases.

    The node is part of the path because StudyID is *not* unique across the
    federation — BR-7214 exists on both BCH and MGH and refers to different
    patients. Use the `FederatedID` from any search result to build this URL.

    This route talks to the node directly rather than through the fan-out, so it
    applies row scope and projection itself. A study outside the contract's
    `row_scope` returns 404 rather than 403: distinguishing "not permitted" from
    "does not exist" would let this route be used to test for the existence of
    records the contract was written to hide.
    """
    node = node.upper()
    if node not in config.NODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown node '{node}'. Valid nodes: {', '.join(config.NODES)}.",
        )

    if node not in contract.accepted_nodes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"{node} has not agreed to serve data under contract "
                f"'{contract.ref}'."
            ),
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
    permitted = authorize(user, contract, [record])
    if not permitted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Study '{study_id}' not found on node {node}.",
        )
    return project(contract, permitted)[0]


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
    contract = params.contract
    records, sources = await client.fetch_studies(request.app.state.http, params.nodes)
    sources = _mark_declined(sources, params.declined)
    partial = _federation_state(sources)
    records = authorize(user, contract, records)
    matched = params.apply(records)

    # Each breakdown reads a column, and a contract that withholds the column
    # withholds the breakdown too. Counting is still reading: a per-hospital sex
    # split is a disclosure about PatientSex whether or not the column itself
    # was returned.
    suppressed: list[str] = []

    def _if_released(column: str, build):
        if contract.releases(column):
            return build()
        if column not in suppressed:
            suppressed.append(column)
        return None

    by_sex: dict[str, Counter] = defaultdict(Counter)
    hospital_by_body_part: dict[str, Counter] = defaultdict(Counter)
    for record in matched:
        by_sex[record.SourceNode][record.PatientSex] += 1
        hospital_by_body_part[record.SourceNode][record.BodyPartExamined] += 1

    return StatsResponse(
        contract=contract.ref,
        # A count of matching rows is not a disclosure about any one column, and
        # withholding it would leave `partial` and `sources` unreadable.
        total_studies=len(matched),
        partial=partial,
        by_hospital=_if_released(
            "SourceNode", lambda: dict(Counter(r.SourceNode for r in matched))
        ),
        by_body_part=_if_released(
            "BodyPartExamined",
            lambda: dict(Counter(r.BodyPartExamined for r in matched)),
        ),
        by_modality=_if_released(
            "Modality", lambda: dict(Counter(r.Modality for r in matched))
        ),
        by_sex=_if_released(
            "PatientSex",
            lambda: {node: dict(counts) for node, counts in by_sex.items()},
        ),
        hospital_by_body_part=_if_released(
            "BodyPartExamined",
            lambda: {
                node: dict(counts) for node, counts in hospital_by_body_part.items()
            },
        ),
        age_histogram=_if_released(
            "PatientAge",
            lambda: dict(
                Counter(
                    _age_bucket(filters.parse_dicom_age(r.PatientAge)) for r in matched
                )
            ),
        ),
        studies_by_year=_if_released(
            "StudyDate", lambda: dict(Counter(r.StudyDate[:4] for r in matched))
        ),
        sources=sources,
        suppressed=suppressed,
    )
