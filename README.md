# Hospital Node Boilerplate

A lightweight FastAPI application that simulates a single, siloed hospital database node for **The Open Accelerator Healthcare Hackathon — Track 1: Federated Medical Imaging Search**.

> **First time?** Start with the [Pre-Hackathon Setup Guide](PRE_HACK_SETUP.md) to get Python, Git, and everything else installed before the event.

## What This Is

This boilerplate represents an intentionally "dumb" hospital edge node. Each instance:

- Blindly serves its own local study data
- Has **zero awareness** of other hospitals
- Lacks **any authentication**
- Intentionally **leaks PII** (patient names, birthdates)

You will run **three separate instances** on different ports to simulate a disconnected, multi-hospital network. Your challenge is to build the overarching aggregation, privacy, and access-control layer on top.

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  BCH  :8001  │   │  MGH  :8002  │   │  BWH  :8003  │
│  900 studies │   │  900 studies │   │  900 studies │
│  No auth     │   │  No auth     │   │  No auth     │
│  PII exposed │   │  PII exposed │   │  PII exposed │
└──────────────┘   └──────────────┘   └──────────────┘
       ↑                  ↑                  ↑
       └──────────────────┼──────────────────┘
                          │
              ┌───────────────────────┐
              │ Federation Gateway    │  ← gateway/  (:8000)
              │ search · stats · JWT  │
              └───────────────────────┘
                          │
                  (redaction: TBD)
```

The gateway is built — see **[gateway/README.md](gateway/README.md)**. Redaction and per-role access control
are the remaining layer.

## Quick Start

### 1. Install dependencies

```bash
cd hospital-node-boilerplate
pip install -r requirements.txt
```

### 2. Start the three hospital nodes

Open three separate terminal windows:

```bash
# Terminal 1 — Boston Children's Hospital
HOSPITAL_NODE=BCH uvicorn main:app --port 8001 --reload

# Terminal 2 — Massachusetts General Hospital
HOSPITAL_NODE=MGH uvicorn main:app --port 8002 --reload

# Terminal 3 — Brigham and Women's Hospital
HOSPITAL_NODE=BWH uvicorn main:app --port 8003 --reload
```

### 3. Verify they're running

```bash
curl http://localhost:8001/health
# {"status":"healthy","node":"BCH"}

curl http://localhost:8002/health
# {"status":"healthy","node":"MGH"}

curl http://localhost:8003/health
# {"status":"healthy","node":"BWH"}
```

## API Reference

Each node exposes three endpoints. All responses are JSON.


| Method | Endpoint                  | Description                                 |
| ------ | ------------------------- | ------------------------------------------- |
| `GET`  | `/health`                 | Health check — returns node name and status |
| `GET`  | `/api/studies`            | Returns all study records on this node      |
| `GET`  | `/api/studies/{study_id}` | Returns a single study by StudyID, or 404   |


**Examples:**

```bash
# Get all studies from BCH
curl http://localhost:8001/api/studies

# Get a specific study by ID
curl http://localhost:8001/api/studies/BR-7721
```

> **Note:** There is no search endpoint *on the nodes* — that's intentional. Search, filtering, and cross-node
> querying live in the [federation gateway](gateway/README.md) on port 8000.

## Federation Gateway

A centralized FastAPI service on **:8000** that fans out to all three nodes concurrently and serves federated
search, aggregate stats, and JWT auth. Started automatically by `devenv up`.

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/token \
  -d 'username=researcher&password=researcher' | jq -r .access_token)

# One query, all three hospitals
curl -s -H "Authorization: Bearer $TOKEN" \
  'localhost:8000/api/studies?q=hydrocephalus&limit=5'
```

| Method | Endpoint | Purpose |
| ------ | -------- | ------- |
| `POST` | `/auth/token` | Exchange demo credentials for a JWT |
| `GET`  | `/health` | Gateway liveness |
| `GET`  | `/api/nodes` | Per-node reachability |
| `GET`  | `/api/studies` | Federated search + filtering |
| `GET`  | `/api/studies/{node}/{study_id}` | One study from one node |
| `GET`  | `/api/stats` | Aggregate counts across hospitals |

Full parameter reference, demo credentials, and failure semantics: **[gateway/README.md](gateway/README.md)**.

> **`StudyID` is not unique across hospitals.** 706 IDs appear on more than one node and refer to *different
> patients* — `BR-7214` exists on both BCH and MGH. Use `FederatedID` (`"BCH:BR-7214"`) as the identifier.
> `StudyInstanceUID` is the only globally unique field.

## Tests

```bash
pytest                  # full suite
pytest -m "not live"    # hermetic only — no running servers needed
```

The `live` tests skip automatically when the stack isn't up. See [tests/README.md](tests/README.md).

### Interactive API Docs

FastAPI auto-generates interactive Swagger UI docs for each running node:

- BCH: [http://localhost:8001/docs](http://localhost:8001/docs)
- MGH: [http://localhost:8002/docs](http://localhost:8002/docs)
- BWH: [http://localhost:8003/docs](http://localhost:8003/docs)

## Data Schema

Each study record contains these fields (all strings):


| Field              | Format                      | Example                         |
| ------------------ | --------------------------- | ------------------------------- |
| `PatientName`      | `LastName^FirstName`        | `Harrington^Lucas`              |
| `PatientID`        | `PREFIX-NNNNN`              | `CHB-99214`                     |
| `PatientBirthDate` | `YYYYMMDD`                  | `20181104`                      |
| `PatientAge`       | `NNNY` / `NNNM` / `NNND`    | `007Y`                          |
| `PatientSex`       | `M` / `F`                   | `M`                             |
| `InstitutionName`  | Full hospital name          | `Boston Children's Hospital`    |
| `StudyID`          | `PREFIX-NNNN`               | `BR-7721`                       |
| `StudyInstanceUID` | DICOM UID format            | `1.3.12.2.1107.5.2.19.45152...` |
| `StudyDate`        | `YYYYMMDD`                  | `20260715`                      |
| `Modality`         | DICOM modality code         | `MR`                            |
| `BodyPartExamined` | `BRAIN` / `HEART` / `FETAL` | `BRAIN`                         |
| `Diagnosis`        | Full radiology report       | Multi-paragraph clinical text   |


## Data Overview

Each hospital has 900 pre-generated study records (300 brain, 300 heart, 300 fetal):

- **BCH** (Boston Children's Hospital) — Pediatric-skewed, ages 5 days to 35 years (median ~15)
- **MGH** (Massachusetts General Hospital) — Adult patients, ages 22–85 (median ~38)
- **BWH** (Brigham and Women's Hospital) — Adult patients, ages 19–74 (median ~34)

Only BCH uses non-year age units (`005D`, `007M`), so age comparisons must parse the DICOM unit rather than
sorting the string — `"005D"` sorts *after* `"031Y"` lexically but is far younger.

Conditions overlap across hospitals, so a federated search for something like "hydrocephalus" will return results from multiple nodes.

Two dataset quirks worth knowing before you build on this:

- `Modality` is `MR` for **all 2700 records** — filtering or charting by modality is currently meaningless.
- `StudyID` is unique *within* a node but **collides across nodes** (706 IDs on 2+ nodes, different patients).
  `StudyInstanceUID` is globally unique.

## Architecture

```
hospital-node-boilerplate/
├── main.py              # Hospital node app — reads HOSPITAL_NODE env var to pick data file
├── models.py            # Pydantic StudyRecord schema
├── requirements.txt     # Dependencies (fastapi, uvicorn, pydantic, httpx, pyjwt, pytest)
├── devenv.nix           # Runs all four processes: three nodes + the gateway
├── gateway/             # Federation gateway (:8000) — see gateway/README.md
│   ├── main.py          #   routes, app wiring
│   ├── client.py        #   concurrent fan-out to the three nodes
│   ├── filters.py       #   search/filter/sort/paginate
│   ├── auth.py          #   JWT + authorization seam
│   ├── config.py        #   node registry, env overrides
│   └── schemas.py       #   FederatedStudy and response envelopes
├── tests/               # pytest suite — see tests/README.md
└── data/
    ├── bch_data.json    # 900 records — Boston Children's Hospital
    ├── mgh_data.json    # 900 records — Massachusetts General Hospital
    └── bwh_data.json    # 900 records — Brigham and Women's Hospital
```

The `HOSPITAL_NODE` environment variable controls which JSON file gets loaded into memory at startup. The app is completely stateless — no database, no external services.

## Tech Stack

- Python 3.10+
- FastAPI
- Uvicorn
- Pydantic
- httpx — gateway → node fan-out
- PyJWT — gateway bearer tokens
- pytest — test suite

