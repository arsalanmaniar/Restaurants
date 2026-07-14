"""Auth dependencies.

Two roles, deliberately separate: restaurant staff may only ever touch their own
restaurant's rows, admins may touch everything. `CurrentStaff` carries the
restaurant_id from the token — route handlers must scope their queries by it and
never trust a restaurant_id from the request body or path.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models import AdminUser, RestaurantStaff

bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


@dataclass
class StaffPrincipal:
    staff: RestaurantStaff
    restaurant_id: int


def _payload(credentials: HTTPAuthorizationCredentials | None) -> dict:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return payload


def get_current_staff(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> StaffPrincipal:
    payload = _payload(credentials)
    if payload.get("role") != "restaurant":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Restaurant access required")

    staff = db.get(RestaurantStaff, int(payload["sub"]))
    if staff is None or not staff.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")

    # Trust the DB, not the token, for the restaurant binding.
    return StaffPrincipal(staff=staff, restaurant_id=staff.restaurant_id)


def get_current_admin(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> AdminUser:
    payload = _payload(credentials)
    if payload.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")

    admin = db.get(AdminUser, int(payload["sub"]))
    if admin is None or not admin.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is not active")
    return admin


CurrentStaff = Annotated[StaffPrincipal, Depends(get_current_staff)]
CurrentAdmin = Annotated[AdminUser, Depends(get_current_admin)]
