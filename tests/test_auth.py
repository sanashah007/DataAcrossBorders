"""Authentication: token issuance, rejection, and endpoint protection."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from gateway import config

PROTECTED_PATHS = [
    "/api/studies",
    "/api/studies/BCH/BR-7214",
    "/api/stats",
    "/api/nodes",
]


def test_login_returns_a_usable_token(gateway):
    response = gateway.post(
        "/auth/token", data={"username": "researcher", "password": "researcher"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == "researcher"
    assert body["expires_in"] == config.TOKEN_TTL_MINUTES * 60

    claims = jwt.decode(
        body["access_token"], config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM]
    )
    assert claims["sub"] == "researcher"
    assert claims["role"] == "researcher"


@pytest.mark.parametrize(
    "username,password",
    [
        ("researcher", "wrong"),
        ("nobody", "researcher"),
        ("RESEARCHER", "researcher"),  # usernames are case-sensitive
    ],
)
def test_bad_credentials_are_rejected(gateway, username, password):
    response = gateway.post(
        "/auth/token", data={"username": username, "password": password}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "data", [{}, {"username": "researcher"}, {"username": "researcher", "password": ""}]
)
def test_incomplete_form_is_a_validation_error(gateway, data):
    # Distinct from bad credentials: the request never formed a login attempt.
    # Note an empty value counts as absent to the form parser, hence 422 not 401.
    assert gateway.post("/auth/token", data=data).status_code == 422


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_endpoints_require_a_token(gateway, path):
    assert gateway.get(path).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_protected_endpoints_reject_a_garbage_token(gateway, path):
    response = gateway.get(path, headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_health_needs_no_token(gateway):
    response = gateway.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_token_signed_with_the_wrong_secret_is_rejected(gateway):
    forged = jwt.encode(
        {
            "sub": "researcher",
            "role": "admin",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "not-the-gateway-secret",
        algorithm="HS256",
    )
    response = gateway.get("/api/studies", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_expired_token_is_rejected(gateway):
    # Well beyond the 30s decode leeway.
    expired = jwt.encode(
        {
            "sub": "researcher",
            "role": "researcher",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        config.JWT_SECRET,
        algorithm=config.JWT_ALGORITHM,
    )
    response = gateway.get(
        "/api/studies", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401


def test_every_demo_user_can_log_in(gateway):
    for username, user in config.DEMO_USERS.items():
        response = gateway.post(
            "/auth/token", data={"username": username, "password": user["password"]}
        )
        assert response.status_code == 200, username
        assert response.json()["role"] == user["role"]
