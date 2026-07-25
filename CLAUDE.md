# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI project for **The Open Accelerator Healthcare Hackathon — Track 1: Federated Medical Imaging Search**. The repo simulates three siloed, unauthenticated hospital database nodes (BCH, MGH, BWH) and adds a federation gateway on top of them. The nodes are intentionally "dumb": each blindly serves its own local JSON study data, has no auth, and leaks PII (patient names, birthdates) by design. The gateway (`gateway/`) is the aggregation/search/auth layer that makes them behave like one federation, and it enforces a **data contract** system that decides which columns actually leave a hospital.

## Architecture

### The hospital nodes (simulated silos)

- `main.py` — single FastAPI app. On import, it reads the `HOSPITAL_NODE` env var (`BCH`/`MGH`/`BWH`), loads the matching JSON file from `data/` into memory, and validates every record against `StudyRecord`. There is no database and no persistence — the app is stateless and the dataset is fixed at process start (changing `data/*.json` requires a restart).
- `models.py` — `StudyRecord`, the DICOM-style study schema (see field table in README.md); all fields are strings, including dates and ages. Also contains `FindingTag` / `LabeledStudyRecord`, which belong to the `scripts/` labeling pipeline. Labeled records now exist in `LLM_output/*_data_labeled.json`, but the nodes load from `data/` and **do not serve them** — so `GenericCategory` and `FindingTags` are not reachable through the gateway either. If that output is ever promoted into `data/`, `gateway/filters.py` is where a tag/category facet would go.
- `data/{bch,mgh,bwh}_data.json` — 900 pre-generated study records per hospital (300 each of brain/heart/fetal). Same schema across all three files; `InstitutionName` and patient age ranges differ per hospital (BCH is pediatric-skewed 5 days–35 years, MGH and BWH are adult).
- The three "nodes" are the **same codebase** run three times with a different `HOSPITAL_NODE` env var and port — not three separate services.

> **Hard rule: `main.py` and `models.py` must stay dumb.** Do not add search, auth, filtering, or cross-node awareness to them. The siloed single-node behavior is the premise of the whole exercise; any federation logic belongs in `gateway/`, which reaches the nodes only over HTTP.

### The federation gateway

`gateway/` — a separate FastAPI app on **:8000** that fans out to all three nodes concurrently on every request. No cache, so node outages are immediately visible. Full documentation in [`gateway/README.md`](gateway/README.md).

| File | Owns |
| --- | --- |
| `gateway/main.py` | App wiring, lifespan, CORS, all routes, the `SearchParams` and `get_contract` dependencies |
| `gateway/config.py` | Node registry, timeouts, JWT settings, demo users, contracts dir — all env-overridable |
| `gateway/schemas.py` | `FederatedStudy` (subclasses root `StudyRecord`), `NodeStatus`, `ContractInfo`, response envelopes |
| `gateway/client.py` | `httpx.AsyncClient` fan-out via `asyncio.gather`; captures per-node status and latency, never raises on node failure |
| `gateway/filters.py` | DICOM age parsing, predicates, sorting, pagination — pure functions, no I/O |
| `gateway/auth.py` | JWT issue/verify, `get_current_user`, `authorize()` (rows) and `project()` (columns) |
| `gateway/contracts/` | The data contract layer — see below |

### The data contract layer

`contracts/` (repo root) holds the governance documents: one YAML per contract, `grants.yaml` mapping users
to the contracts they may invoke, and `acceptance/{bch,mgh,bwh}.yaml` recording what each hospital agreed
to. **[`contracts/README.md`](contracts/README.md) is the reference for the format** — read it before
editing any contract. `gateway/contracts/` turns those documents into enforcement:

| File | Owns |
| --- | --- |
| `gateway/contracts/spec.py` | The document schema; the column registry and reversible-derivation rules |
| `gateway/contracts/expr.py` | The `when:` / `row_scope:` predicate language |
| `gateway/contracts/engine.py` | Released columns, visible rows, which queries are legal |
| `gateway/contracts/loader.py` | Startup load: digests, acceptance, grants |

Non-obvious things about it:

- **Every read route requires `?contract=<id>@<version>`.** There is no default and no implicit full access.
- **A query may only filter or sort on columns the contract releases** — otherwise `?q=` remains a
  redaction-bypass oracle for a hidden column. Conditionally released columns (`allow … when:`) are not
  filterable at all.
- **Projection happens last**, after filtering and sorting, because those steps need values the caller will
  never see. `authorize()` drops rows early; `project()` drops columns at the very end.
- **Never use `eval()` for predicates.** `expr.py` parses with `ast.parse` and walks a whitelist; contract
  files are loaded from disk and must not be a code-execution surface.
- **Acceptance pins a sha256 of the contract file.** Editing a contract silently revokes every hospital's
  acceptance of it — that is intended. Bump the version and re-approve instead.
- **`SearchResponse.results` is `list[dict]`, not `list[FederatedStudy]`**, because contracts omit columns
  and every `FederatedStudy` field is required. Per-contract shape is published at
  `/api/contracts/{ref}/schema`.

Things that will bite you if you don't know them:

- **`StudyID` is not unique across nodes.** 706 IDs appear on 2+ nodes and refer to different patients. Use `FederatedID` (`"BCH:BR-7214"`); `StudyInstanceUID` is the only globally unique field. Never dedupe by `StudyID`.
- **`PatientAge` is DICOM AS format** (`005D`, `007M`, `031Y`) and must be parsed to years — string comparison is wrong, and only BCH data exposes the bug.
- **`StudyDate` is `YYYYMMDD`**, so lexicographic comparison is already chronological. No date parsing needed.
- **`Modality` is `MR` for all 2700 records.** Any modality filter/breakdown is degenerate today.
- **`gateway/schemas.py` is deliberately not named `models.py`** — uvicorn runs from the repo root, so root `models.py` is importable and a second `models.py` inside the package would be a shadowing trap.
- **Roles are inert.** A user's role appears in the JWT but nothing branches on it. Access is decided by the contract they invoke plus the grant in `contracts/grants.yaml`.
- **A hospital that declined a contract reports `not_accepted`**, which is distinct from `error`/`timeout`/`skipped`. It makes a result `partial` but never triggers the 503, because it was never queried.

## Running everything

Via devenv (preferred — `devenv.nix` defines all four processes with health-check readiness on `/health`):

```bash
devenv up          # node-bch (:8001), node-mgh (:8002), node-bwh (:8003), gateway (:8000)
```

Manually, **from the repo root**:

```bash
HOSPITAL_NODE=BCH uvicorn main:app --port 8001 --reload
HOSPITAL_NODE=MGH uvicorn main:app --port 8002 --reload
HOSPITAL_NODE=BWH uvicorn main:app --port 8003 --reload
uvicorn gateway.main:app --port 8000 --reload
```

Install deps: `pip install -r requirements.txt` (or let devenv's Python/venv integration handle it — `devenv.nix` points `languages.python.venv.requirements` at `requirements.txt`).

Verify: `curl http://localhost:8001/health` → `{"status":"healthy","node":"BCH"}`; `curl http://localhost:8000/health` → gateway status.

devenv notes:
- devenv 2.0+ uses the **native** process manager, where inter-process ordering is `after = [ "devenv:processes:node-bch@ready" ]`. `processes.<name>.process-compose.depends_on` is **silently ignored** unless `process.manager.implementation` is set to `"process-compose"` — don't reach for it.
- The node processes pass `--reload-exclude 'gateway/*'` so gateway edits don't restart all three nodes.
- `:8000` is uvicorn's default port, so a bare `uvicorn main:app` collides with the gateway.

## Testing — run this before calling any change done

```bash
pytest                  # full suite (216 tests)
pytest -m "not live"    # hermetic only — no running servers needed
pytest -m live          # end-to-end only — requires `devenv up`
```

Two tiers, documented in [`tests/README.md`](tests/README.md):

- **Hermetic (208 tests)** — an `httpx.MockTransport` impersonates the three nodes and serves the real `data/*.json`. No processes or ports; runs in ~5s. This tier is where node failure, timeouts, and total-federation-outage are simulated.
- **Live (8 tests, `@pytest.mark.live`)** — real uvicorn processes on :8000–:8003. **Skipped automatically** with an actionable message when the stack isn't up, so `pytest` on a fresh clone is green.

Test assertions are pinned to measured properties of the real dataset (2700 records, 900/node, 300 per body part per node, the `BR-7214` collision, `Modality == MR` everywhere). Regenerating `data/*.json` will require updating them. There is no lint config or build step.

## API surface

### Per node (:8001–:8003)

- `GET /health` — node name + status
- `GET /api/studies` — all study records on that node
- `GET /api/studies/{study_id}` — single study by `StudyID`, or 404

Intentionally **no search endpoint** and **no cross-node awareness** — each node only knows its own data.

### Gateway (:8000)

- `POST /auth/token` — demo credentials → JWT (users: `researcher`/`clinician`/`admin`, password == username)
- `GET /health` — gateway liveness, deliberately independent of node health
- `GET /api/nodes` — per-node reachability probe
- `GET /api/contracts` — contracts this caller may invoke
- `GET /api/contracts/{ref}` — one contract's terms, digest, and column split (readable without a grant)
- `GET /api/contracts/{ref}/schema` — JSON Schema of what that contract can return
- `GET /api/studies` — federated search (**`contract` required**, plus `q`, `node`, `body_part`, `modality`, `sex`, `min_age`/`max_age`, `study_date_from`/`study_date_to`, `sort_by`, `order`, `limit`, `offset`)
- `GET /api/studies/{node}/{study_id}` — one study from one node (node-qualified because `StudyID` collides)
- `GET /api/stats` — aggregate counts, accepts the same filters as search

Grants for the demo users: `clinician` → `clinical-full-access`; `researcher` → `fetal-cardiac-outcomes` + `population-health`; `admin` → all three.

Failure semantics: 1–2 nodes down → `200` with `partial: true` and per-node errors in `sources`; all queried nodes down → `503` (never a `200` with zero results, which would read as "no matches"); direct lookup on a dead node → `502`. Contract failures: missing `contract` → `400`; unknown → `404`; not granted, expired, or accepted by no requested hospital → `403`; filtering/sorting on a withheld column → `400`; row outside `row_scope` on the detail route → `404`, never `403`. Full table in [`gateway/README.md`](gateway/README.md).

Interactive docs are auto-generated by FastAPI at `/docs` on every running process.
