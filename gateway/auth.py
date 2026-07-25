from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from gateway import config
from gateway.schemas import FederatedStudy, Principal

# tokenUrl is what makes Swagger's "Authorize" button post to the right place.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


def authenticate_user(username: str, password: str) -> dict | None:
    user = config.DEMO_USERS.get(username)
    if user is None or user["password"] != password:
        return None
    return user


def create_access_token(username: str, role: str) -> tuple[str, int]:
    """Return (encoded_jwt, expires_in_seconds)."""
    ttl = timedelta(minutes=config.TOKEN_TTL_MINUTES)
    expires_at = datetime.now(timezone.utc) + ttl
    payload = {"sub": username, "role": role, "exp": expires_at}
    token = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
    return token, int(ttl.total_seconds())


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> Principal:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # leeway absorbs small clock skew between the gateway and whatever
        # issued the token, which otherwise rejects freshly-minted tokens.
        payload = jwt.decode(
            token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM], leeway=30
        )
    except jwt.PyJWTError:
        raise credentials_error

    username = payload.get("sub")
    role = payload.get("role")
    if not username or not role:
        raise credentials_error

    return Principal(username=username, role=role)


CurrentUser = Annotated[Principal, Depends(get_current_user)]


def authorize(principal: Principal, records: list[FederatedStudy]) -> list[FederatedStudy]:
    """Authorization seam — currently a pass-through.

    Every read route funnels its records through here on the way out. The separate
    access-control mechanism (per-role hospital scoping, PII field redaction) plugs
    in at this one function; nothing else needs to change to enforce it.
    """
    return records
