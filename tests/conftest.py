"""Shared fixtures.

Most tests run fully hermetically: an `httpx.MockTransport` stands in for the
three hospital nodes and serves the real `data/*.json` files, so the gateway's
fan-out, filtering, and error handling are exercised without any process running.
Tests marked `live` instead talk to actually-running servers and are skipped when
those aren't up.
"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import config
from gateway.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent

NODE_DATA_FILES = {
    "BCH": "data/bch_data.json",
    "MGH": "data/mgh_data.json",
    "BWH": "data/bwh_data.json",
}

GATEWAY_URL = "http://127.0.0.1:8000"


@pytest.fixture(scope="session")
def node_data() -> dict[str, list[dict]]:
    """The three hospitals' raw records, loaded once per session."""
    return {
        node: json.loads((REPO_ROOT / path).read_text())
        for node, path in NODE_DATA_FILES.items()
    }


def _node_for_url(url: httpx.URL) -> str | None:
    for node, meta in config.NODES.items():
        if str(url).startswith(meta["url"]):
            return node
    return None


def _make_transport(node_data, down=(), timeout=()):
    """Build a MockTransport impersonating the three nodes.

    `down` nodes raise ConnectError, `timeout` nodes raise ReadTimeout — the two
    failure modes the gateway reports differently.
    """
    down, timeout = set(down), set(timeout)

    def handler(request: httpx.Request) -> httpx.Response:
        node = _node_for_url(request.url)
        if node is None:
            return httpx.Response(404)
        if node in down:
            raise httpx.ConnectError("All connection attempts failed", request=request)
        if node in timeout:
            raise httpx.ReadTimeout("timed out", request=request)

        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "healthy", "node": node})
        if path == "/api/studies":
            return httpx.Response(200, json=node_data[node])
        if path.startswith("/api/studies/"):
            study_id = path.rsplit("/", 1)[-1]
            for record in node_data[node]:
                if record["StudyID"] == study_id:
                    return httpx.Response(200, json=record)
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def make_gateway(node_data):
    """Factory returning a TestClient whose nodes are mocked.

    Usage: `client = make_gateway()` or `make_gateway(down={"BWH"})`.
    """
    clients = []

    def _make(down=(), timeout=()):
        test_client = TestClient(app)
        test_client.__enter__()  # runs lifespan, which sets a real app.state.http
        clients.append(test_client)
        # Swap in the mock afterwards so no real sockets are ever opened.
        app.state.http = httpx.AsyncClient(
            transport=_make_transport(node_data, down=down, timeout=timeout)
        )
        return test_client

    yield _make

    for test_client in clients:
        test_client.__exit__(None, None, None)


@pytest.fixture
def gateway(make_gateway):
    """A gateway with all three nodes healthy."""
    return make_gateway()


def _token(test_client, username="researcher", password="researcher") -> str:
    response = test_client.post(
        "/auth/token", data={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth(gateway) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(gateway)}"}


@pytest.fixture(scope="session")
def live_client():
    """Session-scoped client against a really-running gateway, or skip."""
    try:
        response = httpx.get(f"{GATEWAY_URL}/health", timeout=2.0)
        response.raise_for_status()
    except Exception:
        pytest.skip(
            "live stack not running — start it with `devenv up` "
            "(or run the four uvicorn processes manually)"
        )
    with httpx.Client(base_url=GATEWAY_URL, timeout=30.0) as client:
        response = client.post(
            "/auth/token",
            data={"username": "researcher", "password": "researcher"},
        )
        response.raise_for_status()
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client
