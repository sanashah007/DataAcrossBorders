import os
import sys

# Base URLs are overridable so the gateway can point at nodes running elsewhere
# (another host, a container network, a different port layout).
# Defaults use 127.0.0.1 rather than localhost: uvicorn binds IPv4 only, and on a
# dual-stack machine "localhost" can resolve to ::1 first and stall the request.
NODES = {
    "BCH": {
        "url": os.environ.get("NODE_BCH_URL", "http://127.0.0.1:8001").rstrip("/"),
        "institution": "Boston Children's Hospital",
    },
    "MGH": {
        "url": os.environ.get("NODE_MGH_URL", "http://127.0.0.1:8002").rstrip("/"),
        "institution": "Massachusetts General Hospital",
    },
    "BWH": {
        "url": os.environ.get("NODE_BWH_URL", "http://127.0.0.1:8003").rstrip("/"),
        "institution": "Brigham and Women's Hospital",
    },
}

NODE_TIMEOUT_SECONDS = float(os.environ.get("NODE_TIMEOUT_SECONDS", "5.0"))

_DEV_JWT_SECRET = "dev-secret-do-not-use-in-production"

JWT_SECRET = os.environ.get("GATEWAY_JWT_SECRET", "")

if not JWT_SECRET:
    print(
        "WARNING: GATEWAY_JWT_SECRET is not set. Falling back to a well-known "
        "development secret — anyone can forge tokens. Set GATEWAY_JWT_SECRET "
        "before running this anywhere but localhost.",
        file=sys.stderr,
    )
    JWT_SECRET = _DEV_JWT_SECRET

JWT_ALGORITHM = "HS256"
TOKEN_TTL_MINUTES = int(os.environ.get("GATEWAY_TOKEN_TTL_MINUTES", "60"))

# Demo credentials only — plaintext passwords, no user store, no registration.
# Roles are carried in the JWT so the separate access-control layer has something
# to key off, but the gateway itself does not yet enforce anything per-role.
DEMO_USERS = {
    "researcher": {"password": "researcher", "role": "researcher"},
    "clinician": {"password": "clinician", "role": "clinician"},
    "admin": {"password": "admin", "role": "admin"},
}
