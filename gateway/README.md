# Federation Gateway

A centralized FastAPI service on **:8000** that queries and aggregates data across the three siloed hospital
nodes (BCH :8001, MGH :8002, BWH :8003).

The nodes are intentionally dumb — no search, no auth, no awareness of each other. This gateway is the layer
that makes them behave like one searchable federation. It calls all three nodes' HTTP APIs on every request;
it does not read `data/*.json` and it does not modify the nodes.

```
                        ┌──────────────────────────┐
                        │  Federation Gateway :8000│
                        │  search · stats · JWT    │
                        └────────────┬─────────────┘
                   ┌─────────────────┼─────────────────┐
                   ▼                 ▼                 ▼
            ┌────────────┐    ┌────────────┐    ┌────────────┐
            │ BCH  :8001 │    │ MGH  :8002 │    │ BWH  :8003 │
            │ 900 studies│    │ 900 studies│    │ 900 studies│
            └────────────┘    └────────────┘    └────────────┘
```

## Running it

```bash
devenv up      # starts the three nodes and the gateway
```

Or manually, **from the repo root** (the gateway imports `models.py` from there):

```bash
uvicorn gateway.main:app --port 8000 --reload
```

> `:8000` is uvicorn's default port — a bare `uvicorn main:app` will collide with the gateway.

Interactive docs: <http://localhost:8000/docs>. Click **Authorize**, log in with a demo user, and every
endpoint becomes callable from the browser.

## Authentication

JWT bearer tokens, issued by the gateway. Three demo users, all with the password equal to the username:

| Username | Password | Role |
| --- | --- | --- |
| `researcher` | `researcher` | researcher |
| `clinician` | `clinician` | clinician |
| `admin` | `admin` | admin |

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/token \
  -d 'username=researcher&password=researcher' | jq -r .access_token)

curl -s -H "Authorization: Bearer $TOKEN" 'localhost:8000/api/studies?q=hydrocephalus&limit=5'
```

**These credentials are demo-only**: plaintext passwords, no user store, and a well-known default signing
secret. Set `GATEWAY_JWT_SECRET` before running this anywhere but localhost — the gateway prints a warning
to stderr on startup when you haven't.

Roles are carried in the token but **not yet enforced**. Every authenticated caller currently sees every
hospital and every field, including PII. See [Authorization seam](#authorization-seam).

## Endpoints

| Method | Endpoint | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/auth/token` | — | Exchange demo credentials for a JWT |
| `GET` | `/health` | — | Gateway liveness (deliberately independent of node health) |
| `GET` | `/api/nodes` | JWT | Registry + live reachability probe of each node |
| `GET` | `/api/studies` | JWT | Federated search |
| `GET` | `/api/studies/{node}/{study_id}` | JWT | Single study from one node |
| `GET` | `/api/stats` | JWT | Aggregate counts across the federation |

### `GET /api/studies` — federated search

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `q` | string | — | Case-insensitive substring over `Diagnosis`, `PatientName`, `StudyID`, `PatientID` |
| `node` | repeatable | all | `BCH` / `MGH` / `BWH`. Prunes the fan-out — excluded nodes are marked `skipped` |
| `body_part` | repeatable | — | `BRAIN` / `FETAL` / `HEART` |
| `modality` | repeatable | — | Every record in this dataset is `MR`, so this is currently a no-op filter |
| `sex` | string | — | `M` / `F` |
| `min_age` / `max_age` | float | — | **Years.** Compared against the parsed DICOM age, so `005D` correctly reads as 0.01 years |
| `study_date_from` / `study_date_to` | `YYYYMMDD` | — | Inclusive both ends |
| `sort_by` | enum | `StudyDate` | `StudyDate`, `PatientAge`, `InstitutionName`, `StudyID`, `FederatedID` |
| `order` | enum | `desc` | `asc` / `desc` |
| `limit` | int 1–500 | `50` | |
| `offset` | int ≥0 | `0` | |

Response:

```jsonc
{
  "total": 138,        // post-filter, PRE-pagination
  "limit": 5,
  "offset": 0,
  "partial": false,    // true if any queried node failed — `total` is then incomplete
  "sources": [
    {"node": "BCH", "base_url": "http://127.0.0.1:8001", "status": "ok",
     "record_count": 900, "latency_ms": 42.6, "error": null},
    {"node": "MGH", "...": "..."},
    {"node": "BWH", "...": "..."}
  ],
  "results": [
    {
      "PatientName": "Gomez^Sofia",
      "StudyID": "BR-7214",
      "SourceNode": "BCH",            // ← added by the gateway
      "FederatedID": "BCH:BR-7214",   // ← added by the gateway
      "...": "all twelve original StudyRecord fields"
    }
  ]
}
```

Always read `total` alongside `sources`. If a node errored, its studies are simply absent and `partial` is
`true` — the count is real but incomplete.

### `GET /api/studies/{node}/{study_id}`

**`StudyID` is not unique across the federation.** 706 IDs appear on more than one node, referring to
different patients:

```bash
curl -H "$AUTH" localhost:8000/api/studies/BCH/BR-7214   # Gomez^Sofia,    CHB-18239
curl -H "$AUTH" localhost:8000/api/studies/MGH/BR-7214   # O'Connor^Sean,  MGH-67123
```

That is why the node is part of the path. Use the `FederatedID` from any search result (`"BCH:BR-7214"`) to
build this URL. `StudyInstanceUID` is the only globally unique identifier in the dataset — never deduplicate
by `StudyID`, it would delete real records.

Returns `404` if the node doesn't have that study, `400` for an unknown node, `502` if the node is unreachable.

### `GET /api/stats`

Accepts the same filter parameters as search, so you can get aggregates for a subset
(`/api/stats?q=hydrocephalus` breaks that condition down by hospital).

| Field | Notes |
| --- | --- |
| `total_studies`, `by_hospital` | 2700 / 900 each unfiltered; interesting once filtered |
| `by_body_part`, `hospital_by_body_part` | Exactly 300 per body part per node — degenerate unfiltered by construction |
| `by_sex` | Per hospital, and genuinely skewed (BCH 595F/305M, BWH 765F/135M) |
| `age_histogram` | Bucketed years. The one dimension where the hospitals really differ |
| `studies_by_year` | From `StudyDate[:4]` |
| `by_modality` | **Always `{"MR": 2700}`** in this dataset. Reported for completeness; don't build UI on it |
| `sources`, `partial` | Same per-node status block as search |

`/api/stats` returns counts only and carries no PII.

## Behavior when a node is down

Node failure degrades the result set, never the request:

| Situation | Response |
| --- | --- |
| 1–2 nodes down | `200`, `partial: true`, failed nodes carry `status: "error"`/`"timeout"` and an `error` message |
| All queried nodes down | `503` with the `sources` block — *not* a `200` with zero results, which would read as "no matches" |
| Direct lookup on a dead node | `502` |
| Any node down | `GET /health` is still `200` — the gateway is healthy even when the federation isn't |

Recovery needs no gateway restart: because there is no cache, the next request picks the node back up.

## Module map

| File | Owns |
| --- | --- |
| `main.py` | FastAPI app, lifespan, CORS, all routes, the shared `SearchParams` dependency |
| `config.py` | Node registry, timeouts, JWT settings, demo users — all env-overridable |
| `schemas.py` | `FederatedStudy`, `NodeStatus`, `SearchResponse`, `StatsResponse`, `Token`, `Principal` |
| `client.py` | `httpx.AsyncClient` fan-out via `asyncio.gather`, per-node status/latency capture |
| `filters.py` | DICOM age parsing, predicates, sorting, pagination — pure functions, no I/O |
| `auth.py` | JWT issue/verify, `get_current_user`, and the authorization seam |

`FederatedStudy` subclasses `StudyRecord` from the repo-root `models.py` rather than redeclaring the twelve
DICOM fields, so a schema change on the nodes propagates automatically.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `NODE_BCH_URL` / `NODE_MGH_URL` / `NODE_BWH_URL` | `http://127.0.0.1:800{1,2,3}` | Point the gateway at nodes elsewhere |
| `NODE_TIMEOUT_SECONDS` | `5.0` | Per-node request deadline |
| `GATEWAY_JWT_SECRET` | insecure dev default (warns on stderr) | HS256 signing secret |
| `GATEWAY_TOKEN_TTL_MINUTES` | `60` | Token lifetime |

Defaults use `127.0.0.1` rather than `localhost`: uvicorn binds IPv4 only, and on a dual-stack machine
`localhost` can resolve to `::1` first and stall.

## Authorization seam

Field-level and per-hospital access control is a **separate mechanism, not yet built**. The insertion point
is one function:

```python
# gateway/auth.py
def authorize(principal: Principal, records: list[FederatedStudy]) -> list[FederatedStudy]:
    """Currently a pass-through."""
```

Every read route funnels its records through it, so role-based hospital scoping and PII redaction can land
there without touching the routes. One conflict to resolve when that happens: once `PatientName` is
redacted, the `q` filter still matches against it, which makes search a redaction-bypass oracle. The
authorization layer must also constrain which fields are filterable.

## Known limits

- **Every search fetches all 2700 records (~2 MB) from the nodes.** Honest for a federation demo at this
  scale (~60–100 ms on loopback); it will not survive a much larger dataset. Deliberate, not an oversight —
  caching was ruled out so that node outages stay immediately visible.
- **No PII redaction.** Patient names and birthdates flow through to any authenticated caller, pending the
  access-control layer.
- **No cross-node deduplication**, and there should not be: two nodes holding the same `StudyID` are
  different patients.

## Tests

```bash
pytest                  # everything
pytest -m "not live"    # hermetic only — no servers needed
```

See [`tests/README.md`](../tests/README.md).
