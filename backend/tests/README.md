# Tests

```bash
docker compose up -d                       # local Postgres (from the repo root)
cd backend
DATABASE_URL=postgresql+psycopg://abhiaya:abhiaya@localhost:5433/abhiaya_test pytest
```

No Groq key, no network calls, no payment credentials.

Migrations and demo data are applied automatically on the first test, so a fresh
`docker compose up -d` is all the setup there is.

> **Put that `DATABASE_URL` in your shell profile** (or a `.env.test`) and the command is
> just `pytest`. Environment variables override `backend/.env`, which is why this works
> without editing any file.

## Why a separate database

`abhiaya_test` is created by `docker/init-db.sql` when the Postgres volume is first
built. The suite **refuses to run against a database whose name doesn't end in `_test`** —
individual tests roll back, but `seed()` commits, so pointing pytest at your dev database
would quietly write demo restaurants into it. (`PYTEST_ALLOW_ANY_DB=1` overrides, if you
really mean it.)

`DEBUG=true` is also required, for the same reason.

## Why real Postgres and not SQLite

The app leans on JSONB, Postgres enum types, and `ON DELETE RESTRICT`/`SET NULL`
behaviour. A suite that passes on a database we don't actually ship is worse than no
suite.

## Note the port: 5433, not 5432

Our Postgres publishes on **5433**. Port 5432 is the Postgres default and is very likely
already taken on a developer machine (it was here — by an unrelated Odoo stack). When that
happens Docker quietly does not publish the port and your client connects to **the other
database instead**, which surfaces as a completely baffling
`password authentication failed for user "abhiaya"` even though the credentials are right.

## Don't run it against Neon

It works, but every query is a round trip to another continent: a full run took **36
minutes** there versus **~21 seconds** locally. If you must (no Docker, say), use a Neon
*branch* whose database is named `..._test`, and expect to wait.

## How isolation works

Each test runs inside a transaction that is rolled back afterwards. The app calls
`db.commit()` freely, so the test session joins the outer transaction using SAVEPOINTs
(`join_transaction_mode="create_savepoint"`), which turns those commits into savepoint
releases. Nothing a test does survives it, so tests can run in any order and cannot
corrupt each other.

The one exception is `seed()`, which runs once per session and does commit. It is
idempotent and only touches the demo restaurants.

## What's covered

| File | What it protects |
|---|---|
| `test_ai_tools.py` | The AI can't invent prices, invent item ids, mix restaurants in one cart, or place a duplicate order |
| `test_agent_guards.py` | Duplicate tool calls don't double the food; raw tool-call JSON never reaches a customer. Groq is stubbed |
| `test_auth_and_isolation.py` | One restaurant cannot read or edit another's menu or orders; a restaurant can't raise its own commission |
| `test_orders.py` | Order status can only move forward along legal edges; delivered COD counts as paid |
| `test_opening_hours.py` | Split shifts, past-midnight kitchens, Karachi time; closed restaurants are unorderable |
| `test_payments.py` | Forged callbacks, tampered amounts, replays, and lost callbacks — the money boundary |
| `test_refunds.py` | Admin-only; never refund more than was paid |
| `test_webhooks.py` | The bot doesn't talk to itself, retries don't double-order, the endpoint isn't open to the internet |
| `test_admin.py` | Approvals gate customer visibility; commission is frozen on past orders |

## Useful invocations

```bash
pytest -k payments          # one area
pytest -x                   # stop at the first failure
pytest -v                   # show every test name
pytest --lf                 # re-run only what failed last time
```

## Two things that will bite you if you change the fixtures

**Don't log in through `/auth/*/login` in a fixture.** bcrypt is deliberately slow
(~200ms per verify). Two logins per test added minutes of pure hashing across the suite.
The `*_headers` fixtures mint JWTs directly; the login endpoints are tested properly in
`test_auth_and_isolation.py::TestLogin`, which is where that cost belongs.

**Don't let the seeded working hours stand.** The seed gives restaurants realistic
lunch/dinner hours, which made the whole suite depend on *what time of day you ran it* —
run it at 5pm (between shifts) and every order test failed with "restaurant is closed".
The autouse `always_open` fixture clears the schedule; `test_opening_hours.py` installs
its own. A test that passes at lunchtime and fails at teatime is worse than no test.

## Speed

**159 tests in ~21 seconds** against local Postgres. If it is taking minutes, you are
pointed at a remote database — check `DATABASE_URL`.
