# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI project for **The Open Accelerator Healthcare Hackathon — Track 1: Federated Medical Imaging Search**. The repo simulates three siloed, unauthenticated hospital database nodes (BCH, MGH, BWH) and adds a federation gateway on top of them. The nodes are intentionally "dumb": each blindly serves its own local JSON study data, has no auth, and leaks PII (patient names, birthdates) by design. The gateway (`gateway/`) is the aggregation/search/auth layer that makes them behave like one federation. Privacy/redaction and per-role access control are **not yet built**.

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
| `gateway/main.py` | App wiring, lifespan, CORS, all routes, the shared `SearchParams` dependency |
| `gateway/config.py` | Node registry, timeouts, JWT settings, demo users — all env-overridable |
| `gateway/schemas.py` | `FederatedStudy` (subclasses root `StudyRecord`), `NodeStatus`, response envelopes |
| `gateway/client.py` | `httpx.AsyncClient` fan-out via `asyncio.gather`; captures per-node status and latency, never raises on node failure |
| `gateway/filters.py` | DICOM age parsing, predicates, sorting, pagination — pure functions, no I/O |
| `gateway/auth.py` | JWT issue/verify, `get_current_user`, and the `authorize()` authorization seam |

Things that will bite you if you don't know them:

- **`StudyID` is not unique across nodes.** 706 IDs appear on 2+ nodes and refer to different patients. Use `FederatedID` (`"BCH:BR-7214"`); `StudyInstanceUID` is the only globally unique field. Never dedupe by `StudyID`.
- **`PatientAge` is DICOM AS format** (`005D`, `007M`, `031Y`) and must be parsed to years — string comparison is wrong, and only BCH data exposes the bug.
- **`StudyDate` is `YYYYMMDD`**, so lexicographic comparison is already chronological. No date parsing needed.
- **`Modality` is `MR` for all 2700 records.** Any modality filter/breakdown is degenerate today.
- **`gateway/schemas.py` is deliberately not named `models.py`** — uvicorn runs from the repo root, so root `models.py` is importable and a second `models.py` inside the package would be a shadowing trap.
- **`authorize()` in `gateway/auth.py` is a pass-through seam.** Per-role hospital scoping and PII redaction plug in there; every read route already funnels through it.

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
pytest                  # full suite (98 tests)
pytest -m "not live"    # hermetic only — no running servers needed
pytest -m live          # end-to-end only — requires `devenv up`
```

Two tiers, documented in [`tests/README.md`](tests/README.md):

- **Hermetic (90 tests)** — an `httpx.MockTransport` impersonates the three nodes and serves the real `data/*.json`. No processes or ports; runs in ~4s. This tier is where node failure, timeouts, and total-federation-outage are simulated.
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
- `GET /api/studies` — federated search (`q`, `node`, `body_part`, `modality`, `sex`, `min_age`/`max_age`, `study_date_from`/`study_date_to`, `sort_by`, `order`, `limit`, `offset`)
- `GET /api/studies/{node}/{study_id}` — one study from one node (node-qualified because `StudyID` collides)
- `GET /api/stats` — aggregate counts, accepts the same filters as search

Failure semantics: 1–2 nodes down → `200` with `partial: true` and per-node errors in `sources`; all queried nodes down → `503` (never a `200` with zero results, which would read as "no matches"); direct lookup on a dead node → `502`.

Interactive docs are auto-generated by FastAPI at `/docs` on every running process.
