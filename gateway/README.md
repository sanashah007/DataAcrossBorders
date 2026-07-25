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

Who you are decides **which contracts you may invoke** (`contracts/grants.yaml`). What comes back is decided
by the contract, not by your role. See [Data contracts](#data-contracts).

## Endpoints

| Method | Endpoint | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/auth/token` | — | Exchange demo credentials for a JWT |
| `GET` | `/health` | — | Gateway liveness (deliberately independent of node health) |
| `GET` | `/api/nodes` | JWT | Registry + live reachability probe of each node |
| `GET` | `/api/contracts` | JWT | Contracts this caller may query under |
| `GET` | `/api/contracts/{ref}` | JWT | One contract's terms, digest, and column split |
| `GET` | `/api/contracts/{ref}/schema` | JWT | JSON Schema of what that contract can return |
| `GET` | `/api/studies` | JWT + contract | Federated search |
| `GET` | `/api/studies/{node}/{study_id}` | JWT + contract | Single study from one node |
| `GET` | `/api/stats` | JWT + contract | Aggregate counts across the federation |

### `GET /api/studies` — federated search

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `contract` | `id@version` | **required** | The agreement this query is conducted under. Omitting it is a `400` |
| `q` | string | — | Case-insensitive substring over the contract's subset of `Diagnosis`, `PatientName`, `StudyID`, `PatientID` |
| `node` | repeatable | all | `BCH` / `MGH` / `BWH`. Prunes the fan-out — excluded nodes are marked `skipped` |
| `body_part` | repeatable | — | `BRAIN` / `FETAL` / `HEART` |
| `modality` | repeatable | — | Every record in this dataset is `MR`, so this is currently a no-op filter |
| `sex` | string | — | `M` / `F` |
| `min_age` / `max_age` | float | — | **Years.** Compared against the parsed DICOM age, so `005D` correctly reads as 0.01 years |
| `study_date_from` / `study_date_to` | `YYYYMMDD` | — | Inclusive both ends |
| `sort_by` | enum | first column the contract releases | `StudyDate`, `PatientAge`, `InstitutionName`, `StudyID`, `FederatedID`. Sorting on a withheld column is a `400` |
| `order` | enum | `desc` | `asc` / `desc` |
| `limit` | int 1–500 | `50` | |
| `offset` | int ≥0 | `0` | |

Response:

```jsonc
{
  "contract": "fetal-cardiac-outcomes@1.2.0",
  "total": 1200,       // post-row-scope, post-filter, PRE-pagination
  "limit": 5,
  "offset": 0,
  "partial": true,     // a node failed OR declined this contract
  "sources": [
    {"node": "BCH", "base_url": "http://127.0.0.1:8001", "status": "ok",
     "record_count": 900, "latency_ms": 42.6, "error": null},
    {"node": "MGH", "...": "..."},
    {"node": "BWH", "status": "not_accepted", "...": "..."}   // ← declined, not down
  ],
  "results": [
    {
      "SourceNode": "BCH",
      "BodyPartExamined": "FETAL",
      "PatientAge": "020Y",
      "Diagnosis": "...",     // only on FETAL rows under this contract
      "...": "whatever this contract releases — no PatientName here"
    }
  ]
}
```

`results` is a list of plain objects, not a fixed schema: a contract omits columns entirely rather than
blanking them, so which keys appear depends on the contract and, for conditional columns, on the row. The
shape for a given contract is published at `GET /api/contracts/{ref}/schema`.

Always read `total` alongside `sources`. If a node errored or declined the contract, its studies are simply
absent and `partial` is `true` — the count is real but incomplete.

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

A hospital declining a contract looks deliberately different from one being down — see
[Declining is not an outage](#declining-is-not-an-outage).

## Full status reference

| Condition | Status |
| --- | --- |
| No token, or a bad one | `401` |
| No `contract` parameter | `400`, listing the contracts the caller may use |
| Unknown contract or version | `404` |
| Caller not granted the contract | `403` |
| Contract past its `expires` date | `403` |
| Query filters or sorts on a column the contract withholds | `400`, naming the parameter and column |
| Unknown node in `?node=` | `400` |
| Some hospitals declined the contract | `200`, `partial: true`, `status: "not_accepted"` |
| No requested hospital accepted the contract | `403` |
| Detail route, hospital declined the contract | `403` |
| Detail route, row outside the contract's `row_scope` | `404` — never `403`, which would confirm it exists |
| 1–2 nodes down | `200`, `partial: true` |
| All queried nodes down | `503` |
| Direct lookup on a dead node | `502` |

## A worked example

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/token \
  -d 'username=researcher&password=researcher' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# What may this caller use?
curl -s -H "$AUTH" localhost:8000/api/contracts | jq -r '.[].ref'
# fetal-cardiac-outcomes@1.2.0
# population-health@1.0.0

# What does one of them actually release?
curl -s -H "$AUTH" localhost:8000/api/contracts/fetal-cardiac-outcomes@1.2.0 \
  | jq '{columns, conditional_columns, denied_columns, accepted_by}'

# Query under it. Note BWH is absent — it declined, and says so.
curl -s -H "$AUTH" \
  "localhost:8000/api/studies?contract=fetal-cardiac-outcomes@1.2.0&body_part=FETAL&limit=1" \
  | jq '{partial, sources: [.sources[] | {node, status}], keys: (.results[0] | keys)}'

# Diagnosis is released on FETAL rows and withheld on HEART rows, same contract:
curl -s -H "$AUTH" \
  "localhost:8000/api/studies?contract=fetal-cardiac-outcomes@1.2.0&body_part=HEART&limit=1" \
  | jq '.results[0] | keys'

# And the oracle is closed:
curl -s -H "$AUTH" \
  "localhost:8000/api/studies?contract=fetal-cardiac-outcomes@1.2.0&q=Harrington" | jq .detail
```

## Module map

| File | Owns |
| --- | --- |
| `main.py` | FastAPI app, lifespan, CORS, all routes, the `SearchParams` and `get_contract` dependencies |
| `config.py` | Node registry, timeouts, JWT settings, demo users, contracts directory — all env-overridable |
| `schemas.py` | `FederatedStudy`, `NodeStatus`, `SearchResponse`, `StatsResponse`, `ContractInfo`, `Token`, `Principal` |
| `client.py` | `httpx.AsyncClient` fan-out via `asyncio.gather`, per-node status/latency capture |
| `filters.py` | DICOM age parsing, predicates, sorting, pagination — pure functions, no I/O |
| `auth.py` | JWT issue/verify, `get_current_user`, `authorize()` (rows) and `project()` (columns) |
| `contracts/spec.py` | What a contract document may say; the column registry and derivation rules |
| `contracts/expr.py` | The `when:` / `row_scope:` predicate language — parsed, never `eval`'d |
| `contracts/engine.py` | Resolving a document into released columns, visible rows, legal queries |
| `contracts/loader.py` | Reading `contracts/` at startup: digests, acceptance, grants |

`FederatedStudy` subclasses `StudyRecord` from the repo-root `models.py` rather than redeclaring the twelve
DICOM fields, so a schema change on the nodes propagates automatically.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `NODE_BCH_URL` / `NODE_MGH_URL` / `NODE_BWH_URL` | `http://127.0.0.1:800{1,2,3}` | Point the gateway at nodes elsewhere |
| `NODE_TIMEOUT_SECONDS` | `5.0` | Per-node request deadline |
| `GATEWAY_JWT_SECRET` | insecure dev default (warns on stderr) | HS256 signing secret |
| `GATEWAY_TOKEN_TTL_MINUTES` | `60` | Token lifetime |
| `GATEWAY_CONTRACTS_DIR` | `<repo>/contracts` | Where contract documents, grants, and acceptance lists are read from |

Defaults use `127.0.0.1` rather than `localhost`: uvicorn binds IPv4 only, and on a dual-stack machine
`localhost` can resolve to `::1` first and stall.

## Data contracts

Access control is a **data contract** system. A contract is a versioned, digest-pinned YAML document
declaring which columns may leave a hospital and under what row conditions; each hospital independently
records which contracts it accepted. The documents live in [`contracts/`](../contracts/README.md) — that
README is the reference for the format. This section covers how the gateway enforces them.

Every read names exactly one contract. Three independent checks must pass:

| Check | Source | Failure |
| --- | --- | --- |
| The caller may invoke this contract | `contracts/grants.yaml` | `403` |
| The hospital agreed to serve under it | `contracts/acceptance/<node>.yaml` | node drops out, `not_accepted` |
| The query only reads columns it releases | the contract's rules | `400` |

### The request pipeline

Order is load-bearing:

1. Resolve `?contract=`, check the grant, drop hospitals that did not accept it.
2. Fan out, then apply `row_scope` — **before** filtering, so `total` counts only rows inside the
   agreement and cannot be watched to infer how much data sits outside it.
3. Validate the query's columns (this happens during dependency resolution, so an impermissible query is
   refused before any hospital is contacted).
4. Filter, sort, paginate.
5. **Project columns last.** Row predicates, filters, and statistics all need values the caller will never
   see, so records stay whole until the final step.

`gateway/auth.py` holds the two halves: `authorize()` drops rows, `project()` drops columns.

### The redaction-bypass oracle, and how it is closed

Hiding `PatientName` from a response accomplishes nothing if `?q=` still matches against it — the result
count answers the question the column would have. So **a query may only filter or sort on columns the
contract releases on every row**:

```bash
# fetal-cardiac-outcomes releases none of the text columns
curl "…/api/studies?contract=fetal-cardiac-outcomes@1.2.0&q=Harrington"
# 400  {"detail": "…releases none of the free-text searchable columns…",
#       "parameter": "q", "column": "Diagnosis, PatientName, StudyID, PatientID"}
```

Conditionally released columns (`allow … when:`) cannot be filtered or sorted on **at all**, even for the
rows where they apply: a whole-query predicate over a column that exists on only some rows discloses
exactly which rows those are.

The *default* sort is exempt — it falls back to a column the contract releases rather than 400-ing a caller
who never asked to sort.

### Declining is not an outage

A hospital that has not accepted a contract is reported as `not_accepted`, distinct from `error`, `timeout`,
and `skipped`. It makes the result `partial`, but it cannot trigger the `503`, because it was never queried.
If *no* requested hospital accepted, the answer is `403` — an empty `200` would read as "no matching
studies", which is false: the data exists and the hospital declined to serve it under this agreement.

### Statistics

`/api/stats` gates each breakdown on the column it reads and reports what it withheld in `suppressed`.
Counting is still reading — a per-hospital sex split discloses `PatientSex` whether or not the column itself
came back. `total_studies` is always present; a row count is not a disclosure about any one column, and
withholding it would make `partial` and `sources` unreadable.

### Editing a contract revokes it

Each acceptance pins a `sha256` of the contract file. The gateway recomputes it at startup; if the bytes
changed, that hospital's acceptance no longer applies and it stops serving under it, loudly. This is what
makes it a contract rather than a config file — the terms cannot drift out from under the party that signed
them. To change one, bump the version and have hospitals re-approve.

## Known limits

- **Every search fetches all 2700 records (~2 MB) from the nodes.** Honest for a federation demo at this
  scale (~60–100 ms on loopback); it will not survive a much larger dataset. Deliberate, not an oversight —
  caching was ruled out so that node outages stay immediately visible.
- **Contracts release or withhold columns; they do not yet transform them.** Binning ages, truncating dates,
  and redacting names out of free text are reserved in the format but unimplemented, so a contract using
  `transform:` is rejected at load rather than silently ignored. Until then, a column carrying re-identifying
  detail can only be denied outright — which is why `population-health` drops `Diagnosis` entirely and leans
  on the derived `GenericCategory` instead.
- **No k-anonymity or small-cell suppression.** A filter narrow enough to match one study returns that one
  study, and `/api/stats` will report a cell of size 1. Contracts bound *which* columns are readable, not how
  precisely they can be sliced.
- **Grants are per-user and static**, read from `contracts/grants.yaml` at startup. No expiry per grant, no
  delegation, no audit log of who queried what under which contract.
- **No cross-node deduplication**, and there should not be: two nodes holding the same `StudyID` are
  different patients.

## Tests

```bash
pytest                  # everything
pytest -m "not live"    # hermetic only — no servers needed
```

See [`tests/README.md`](../tests/README.md).
