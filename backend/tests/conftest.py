"""Shared test fixtures.

**Isolation.** Every test runs inside a transaction that is rolled back afterwards, so
tests cannot see or corrupt each other's data no matter what order they run in. The app
calls `db.commit()` all over the place, so a plain "don't commit" approach would not
work: instead the session joins the outer transaction via SAVEPOINTs
(`join_transaction_mode="create_savepoint"`), which turns those commits into savepoint
releases. The outer transaction is then rolled back and nothing survives.

**The database.** Tests run against a real Postgres — the app leans on Postgres-specific
things (JSONB, enum types, `ON DELETE` behaviour) that SQLite would not catch. Point
`DATABASE_URL` at a scratch database; see backend/tests/README.md.
"""

import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.database import engine, get_db
from app.core.security import create_access_token
from app.main import app
from app.models import (
    AdminUser,
    Conversation,
    Customer,
    MenuItem,
    Order,
    Restaurant,
    RestaurantStaff,
    RestaurantWorkingHours,
)
from app.seed import seed
from app.services import conversations as convo
from app.services import tools


def pytest_configure(config):
    """Refuse to run against anything that isn't obviously a throwaway database.

    Individual tests roll back, but `seed()` commits. Pointing pytest at the wrong
    DATABASE_URL would therefore write demo restaurants into it. The database name must
    end in `_test`, which makes that mistake impossible to make silently.
    """
    if not settings.debug:
        raise pytest.UsageError(
            "Refusing to run tests with DEBUG=false — this suite writes seed data."
        )

    database = urlsplit(settings.database_url).path.lstrip("/")

    if not database.endswith("_test") and not os.environ.get("PYTEST_ALLOW_ANY_DB"):
        raise pytest.UsageError(
            f"Refusing to run tests against database {database!r}: the name must end in "
            "'_test'.\n\n"
            "  docker compose up -d\n"
            "  DATABASE_URL=postgresql+psycopg://abhiaya:abhiaya@localhost:5432/abhiaya_test "
            "pytest\n\n"
            "(Set PYTEST_ALLOW_ANY_DB=1 to override — but the suite seeds data, so only "
            "do that against a database you are happy to write to.)"
        )


@pytest.fixture(scope="session", autouse=True)
def seeded():
    """Bring the test database up to date and load demo data.

    Migrations run here rather than being a documented step you must remember: a fresh
    `docker compose up -d` gives an empty database, and `pytest` should just work.
    Both operations are idempotent.
    """
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    seed()


@pytest.fixture
def db(seeded) -> Session:
    # Each test takes a connection and holds an open transaction for its whole duration.
    # With the app's pooled engine that meant tests queued behind each other's
    # connections, which is what turned a full run into hours. A fresh connection per
    # test, returned immediately, keeps them independent.
    connection = engine.connect()
    transaction = connection.begin()

    factory = sessionmaker(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
        future=True,
    )
    session = factory()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()   # nothing this test did survives
        connection.close()


@pytest.fixture(autouse=True)
def always_open(db):
    """Keep the demo restaurants open for the whole suite.

    The seed gives them realistic lunch/dinner hours, which made the ENTIRE suite
    time-of-day dependent: run it at 5pm and every order test failed with "restaurant is
    closed", because that is a gap between shifts. A test that passes at lunchtime and
    fails at teatime is worse than no test.

    Clearing the schedule means "always open" (see services/opening_hours.py), and it is
    rolled back with the test. test_opening_hours.py installs its own schedule and so is
    unaffected.
    """
    db.execute(delete(RestaurantWorkingHours))
    db.flush()


@pytest.fixture
def client(db) -> TestClient:
    """A client whose requests run inside the SAME transaction as `db`, so a test can
    assert on the database directly after calling the API."""
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def _staff_headers(db: Session, email: str) -> dict[str, str]:
    """Mint the token directly instead of POSTing to /auth/*/login.

    Logging in runs bcrypt, which is deliberately slow (~200ms). Two logins per test
    turned into minutes of pure hashing across the suite. The login endpoints themselves
    are covered properly in test_auth_and_isolation.py::TestLogin — these fixtures only
    need a valid token, not a re-test of password checking.
    """
    staff = db.scalar(select(RestaurantStaff).where(RestaurantStaff.email == email))
    token = create_access_token(
        str(staff.id), role="restaurant", restaurant_id=staff.restaurant_id
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(db, admin) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(admin.id), role='admin')}"}


@pytest.fixture
def pizza_headers(db) -> dict[str, str]:
    return _staff_headers(db, "owner@pizzajunction.pk")


@pytest.fixture
def biryani_headers(db) -> dict[str, str]:
    """A SECOND restaurant — the one we use to prove tenant isolation."""
    return _staff_headers(db, "owner@karachibiryani.pk")


@pytest.fixture
def admin(db) -> AdminUser:
    return db.scalar(select(AdminUser).where(AdminUser.email == "admin@abhiaya.pk"))


# --------------------------------------------------------------------------- #
# Domain objects
# --------------------------------------------------------------------------- #


@pytest.fixture
def pizza(db) -> Restaurant:
    return db.scalar(select(Restaurant).where(Restaurant.name == "Pizza Junction"))


@pytest.fixture
def biryani(db) -> Restaurant:
    return db.scalar(select(Restaurant).where(Restaurant.name == "Karachi Biryani House"))


@pytest.fixture
def conversation(db) -> Conversation:
    """A fresh customer + conversation. The number is unique per test because the
    transaction rolls back, so it can be a constant."""
    customer = convo.get_or_create_customer(db, "923001234567")
    conv = convo.get_or_create_conversation(db, customer)
    db.flush()
    return conv


@pytest.fixture
def customer(conversation) -> Customer:
    return conversation.customer


@pytest.fixture
def menu_item(db, pizza) -> MenuItem:
    return db.scalar(
        select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        )
    )


@pytest.fixture
def cart_with_pizza(db, conversation, pizza, menu_item):
    """A conversation whose cart holds 2 pizzas, grounded through get_menu (which is what
    unlocks add_to_cart — see the grounding guard in services/tools.py)."""
    tools.get_menu(db, conversation, restaurant_id=pizza.id)
    tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)
    db.flush()
    return conversation


@pytest.fixture
def cod_order(db, cart_with_pizza) -> Order:
    result = tools.place_order(
        db, cart_with_pizza, delivery_address="House 1, DHA, Lahore", payment_method="cod"
    )
    db.flush()
    return db.scalar(select(Order).where(Order.order_number == result["order_number"]))


@pytest.fixture
def delivered_order(db, client, cod_order, pizza_headers) -> Order:
    """A COD order driven all the way to DELIVERED through the real API — which is what
    marks it paid, and therefore refundable."""
    for status in ("accepted", "preparing", "ready", "delivered"):
        response = client.patch(
            f"/restaurant/orders/{cod_order.id}/status",
            headers=pizza_headers,
            json={"status": status},
        )
        assert response.status_code == 200, response.text

    db.expire_all()
    return db.get(Order, cod_order.id)


@pytest.fixture
def money():
    return Decimal
