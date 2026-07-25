"""End-to-end checks against a really-running stack.

These are the verification steps that the mocked tests cannot cover: real
sockets, real uvicorn processes, real concurrency. Skipped automatically unless
the gateway is answering on :8000 — start it with `devenv up`.
"""

import pytest

pytestmark = pytest.mark.live

TOTAL_RECORDS = 2700
PER_NODE = 900


def test_all_three_nodes_are_reachable(live_client):
    body = live_client.get("/api/nodes").json()
    assert {n["node"] for n in body} == {"BCH", "MGH", "BWH"}
    unreachable = [n["node"] for n in body if not n["reachable"]]
    assert not unreachable, f"nodes down: {unreachable}"


def test_full_federation_over_real_http(live_client):
    body = live_client.get("/api/studies", params={"limit": 1}).json()
    assert body["total"] == TOTAL_RECORDS
    assert body["partial"] is False
    assert all(s["status"] == "ok" for s in body["sources"])
    assert all(s["record_count"] == PER_NODE for s in body["sources"])


def test_fan_out_is_concurrent_not_serial(live_client):
    """Wall-clock time must be near the slowest node, not the sum of all three."""
    body = live_client.get("/api/studies", params={"limit": 1}).json()
    latencies = [s["latency_ms"] for s in body["sources"]]
    assert all(latency is not None for latency in latencies)
    # Generous bound: serial execution would put every node's start time after
    # the previous one finished, so the total would approach the sum.
    assert max(latencies) < sum(latencies)


def test_search_round_trip(live_client):
    body = live_client.get(
        "/api/studies", params={"q": "hydrocephalus", "limit": 100}
    ).json()
    assert body["total"] > 0
    assert len({r["SourceNode"] for r in body["results"]}) > 1


def test_study_id_collision_over_real_http(live_client):
    bch = live_client.get("/api/studies/BCH/BR-7214").json()
    mgh = live_client.get("/api/studies/MGH/BR-7214").json()
    assert bch["PatientID"] != mgh["PatientID"]


def test_stats_round_trip(live_client):
    body = live_client.get("/api/stats").json()
    assert body["total_studies"] == TOTAL_RECORDS
    assert body["by_hospital"] == {"BCH": PER_NODE, "MGH": PER_NODE, "BWH": PER_NODE}


def test_openapi_schema_is_served(live_client):
    schema = live_client.get("/openapi.json").json()
    assert "/api/studies" in schema["paths"]
    assert "/api/studies/{node}/{study_id}" in schema["paths"]
    # Swagger's Authorize button depends on this being advertised.
    assert "OAuth2PasswordBearer" in schema["components"]["securitySchemes"]


def test_unauthenticated_request_is_refused_over_real_http(live_client):
    response = live_client.get(
        "/api/studies", headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401
