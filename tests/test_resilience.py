"""Partial-failure behavior — the point of a federation gateway.

A node that is down or slow must degrade the result set, never the request.
"""

PER_NODE = 900


def test_one_node_down_still_returns_the_others(make_gateway):
    gateway = make_gateway(down={"BWH"})
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    response = gateway.get("/api/studies", headers=auth, params={"limit": 1})
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2 * PER_NODE
    assert body["partial"] is True
    statuses = {s["node"]: s for s in body["sources"]}
    assert statuses["BCH"]["status"] == "ok"
    assert statuses["MGH"]["status"] == "ok"
    assert statuses["BWH"]["status"] == "error"
    assert statuses["BWH"]["error"]  # the reason is surfaced, not swallowed
    assert statuses["BWH"]["record_count"] == 0
    assert {r["SourceNode"] for r in body["results"]} <= {"BCH", "MGH"}


def test_a_timing_out_node_is_reported_distinctly_from_a_refused_one(make_gateway):
    gateway = make_gateway(down={"BCH"}, timeout={"MGH"})
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    body = gateway.get("/api/studies", headers=auth, params={"limit": 1}).json()
    statuses = {s["node"]: s["status"] for s in body["sources"]}
    assert statuses["BCH"] == "error"
    assert statuses["MGH"] == "timeout"
    assert statuses["BWH"] == "ok"
    assert body["total"] == PER_NODE
    assert body["partial"] is True


def test_whole_federation_down_is_503_not_an_empty_success(make_gateway):
    # A 200 with zero results here would read as "no matches found", which is a
    # materially different and false claim.
    gateway = make_gateway(down={"BCH", "MGH", "BWH"})
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    response = gateway.get("/api/studies", headers=auth, params={"limit": 1})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert {s["node"] for s in detail["sources"]} == {"BCH", "MGH", "BWH"}


def test_gateway_health_is_independent_of_node_health(make_gateway):
    gateway = make_gateway(down={"BCH", "MGH", "BWH"})
    response = gateway.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_nodes_endpoint_reports_unreachable_nodes(make_gateway):
    gateway = make_gateway(down={"MGH"})
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    body = gateway.get(
        "/api/nodes", headers={"Authorization": f"Bearer {token}"}
    ).json()
    reachable = {n["node"]: n["reachable"] for n in body}
    assert reachable == {"BCH": True, "MGH": False, "BWH": True}


def test_direct_lookup_against_a_dead_node_is_502(make_gateway):
    gateway = make_gateway(down={"BWH"})
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    response = gateway.get(
        "/api/studies/BWH/BR-8934", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 502


def test_stats_degrade_partially_too(make_gateway):
    gateway = make_gateway(down={"BWH"})
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    body = gateway.get(
        "/api/stats", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert body["total_studies"] == 2 * PER_NODE
    assert body["partial"] is True
    assert "BWH" not in body["by_hospital"]


def test_excluding_a_node_is_not_reported_as_partial(make_gateway):
    # An explicitly skipped node is a choice, not a failure.
    gateway = make_gateway()
    token = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    ).json()["access_token"]
    body = gateway.get(
        "/api/studies",
        headers={"Authorization": f"Bearer {token}"},
        params={"node": "BCH", "limit": 1},
    ).json()
    assert body["partial"] is False
