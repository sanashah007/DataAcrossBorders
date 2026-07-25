from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from gateway import config
from gateway.contracts import CompiledContract
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


def authorize(
    principal: Principal,
    contract: CompiledContract,
    records: list[FederatedStudy],
) -> list[FederatedStudy]:
    """Drop the rows this contract cannot see.

    The first half of the authorization seam. Runs before filtering so that
    `total` counts only rows within the contract's `row_scope` — a caller cannot
    infer how much data sits outside their agreement by watching counts move.

    Records stay whole here. Columns are removed by `project()` at the very end
    of the request, because filters, sorts, and statistics all need values that
    the caller will never see.
    """
    return contract.scope_rows(records)


def project(
    contract: CompiledContract, records: list[FederatedStudy]
) -> list[dict[str, object]]:
    """Reduce records to the columns the contract releases.

    The second half of the seam, and the last thing that happens to data before
    it is serialized. Nothing downstream of this may reach back to the whole
    record.
    """
    return contract.project(records)
