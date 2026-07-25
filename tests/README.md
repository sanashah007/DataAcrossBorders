# Tests

Verification suite for the [federation gateway](../gateway/README.md). **Run this before considering any
change done.**

```bash
pytest                  # everything (98 tests)
pytest -m "not live"    # hermetic only — no servers required
pytest -m live          # end-to-end only — needs `devenv up`
pytest -q tests/test_filters.py -k age    # narrow while iterating
```

## Two tiers

**Hermetic (90 tests)** — an `httpx.MockTransport` impersonates the three nodes and serves the real
`data/*.json` files. No processes, no sockets, no ports. Runs in ~4 seconds and works on a fresh clone.
This tier can simulate things the live stack can't easily reproduce: a node timing out, a node refusing
connections, the entire federation being down.

**Live (8 tests, marked `live`)** — talks to actually-running uvicorn processes on :8000–:8003. Covers what
mocks can't: real sockets, real concurrency, the served OpenAPI schema. **Skipped automatically with an
actionable message when the stack isn't up**, so `pytest` on a fresh clone is always green.

| File | Covers |
| --- | --- |
| `test_filters.py` | Pure units: DICOM age parsing, predicates, sorting, pagination. No HTTP |
| `test_auth.py` | Token issuance, forged/expired/garbage tokens, endpoint protection |
| `test_search.py` | Fan-out, every filter, sorting, pagination stability, StudyID collisions |
| `test_stats.py` | Aggregate correctness against known dataset counts |
| `test_resilience.py` | Node down / timing out / whole federation down |
| `test_live.py` | End-to-end over real HTTP |

## Fixtures

- `gateway` — a `TestClient` with all three nodes mocked and healthy.
- `make_gateway(down=…, timeout=…)` — same, with chosen nodes failing.
  `make_gateway(down={"BWH"}, timeout={"MGH"})`.
- `auth` — ready-to-use `Authorization` header for the `researcher` demo user.
- `live_client` — session-scoped, pre-authenticated client against :8000, or skip.

## What the assertions are pinned to

Tests assert against **measured properties of the real dataset**, not the README (whose stated age ranges are
wrong). Facts currently encoded:

- 2700 records total, 900 per node, 300 per body part per node
- `Modality` is `MR` for every record — `test_modality_is_degenerate_but_reported` will fail if a richer
  dataset ever lands, which is the intended prompt to update the docs
- `StudyID` collides across nodes (`BR-7214` is on both BCH and MGH, different patients)
- Only BCH has non-year ages (`005D`, `007M`), which is what makes unit-aware age parsing observable

If you regenerate `data/*.json`, expect these counts to need updating.

## Adding tests

Prefer the hermetic tier — it's faster, it runs anywhere, and it can simulate failure. Reach for `live` only
when the thing under test is the transport itself.
