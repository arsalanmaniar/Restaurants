from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import DbSession
from app.core.security import create_access_token, verify_password
from app.models import AdminUser, RestaurantStaff
from app.schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID = "Incorrect email or password"


@router.post("/restaurant/login", response_model=TokenResponse)
def restaurant_login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    staff = db.scalar(select(RestaurantStaff).where(RestaurantStaff.email == payload.email))

    # Same message and same code path whether the email exists or the password is
    # wrong — otherwise this endpoint tells an attacker which emails are real.
    if staff is None or not verify_password(payload.password, staff.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)
    if not staff.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")

    return TokenResponse(
        access_token=create_access_token(
            str(staff.id), role="restaurant", restaurant_id=staff.restaurant_id
        ),
        role="restaurant",
        name=staff.name,
        restaurant_id=staff.restaurant_id,
        restaurant_name=staff.restaurant.name,
    )


@router.post("/admin/login", response_model=TokenResponse)
def admin_login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    admin = db.scalar(select(AdminUser).where(AdminUser.email == payload.email))

    if admin is None or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)
    if not admin.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account has been disabled")

    return TokenResponse(
        access_token=create_access_token(str(admin.id), role="admin"),
        role="admin",
        name=admin.name,
    )
