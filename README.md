# AbhiAya

AI-powered WhatsApp multi-restaurant ordering platform.

One WhatsApp number. A customer messages it, an AI takes their order in plain language
(English or Roman Urdu), routes it to the right restaurant, and the restaurant sees it on
a dashboard. Admins oversee every restaurant, order, and rupee of commission.

**Status: Phase 0 (MVP demo) — feature complete, running locally.**

---

## What works today

- **WhatsApp ordering** — browse restaurants, view menus, build a cart, place an order,
  check order status. Cash on delivery only.
- **Restaurant dashboard** — live orders board, status updates, menu CRUD with in-stock
  toggle, opening hours, settings, customer ratings.
- **Admin dashboard** — restaurant approvals, cross-restaurant orders, platform revenue
  and commission, subscription plans, per-restaurant commission overrides.

## What does not work yet

- **Online payments.** COD only. See [`docs/PAYMENTS_PLAN.md`](docs/PAYMENTS_PLAN.md).
- **Customer-facing rating collection.** The data model, API, and dashboard exist; the AI
  does not yet *ask* for a rating after delivery.
- **Notifications.** Order status changes do not yet message the customer. See the
  24-hour-window warning under [WhatsApp](#whatsapp-ultramsg) below — this is not just
  "wire up a send call".
- **Live WhatsApp.** The webhook is built and tested, but has never run against a real
  UltraMsg instance.

---

## Stack

| | |
|---|---|
| Backend | FastAPI (Python 3.13), SQLAlchemy 2, Alembic |
| Database | PostgreSQL (currently Neon, serverless) |
| AI | Groq — `llama-3.3-70b-versatile`, function calling |
| WhatsApp | UltraMsg (migration path to Meta Cloud API) |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind |
| Auth | JWT, two roles: `admin` and `restaurant` |

---

## Setup

### Prerequisites

- Python 3.13+
- Node 20+
- A PostgreSQL database (see below)
- A Groq API key — free at [console.groq.com/keys](https://console.groq.com/keys)

### 1. Database

The project connects to **Neon** (managed Postgres) by default. Create a free project at
[console.neon.tech](https://console.neon.tech) and take the connection string.

> **If you use Neon's pooled endpoint** (the hostname contains `-pooler`), it is fronted by
> PgBouncer, which does not support the prepared statements psycopg3 issues by default. The
> app already handles this with `prepare_threshold: 0` in
> `backend/app/core/database.py` — don't remove it, things will break in confusing ways.

A `docker-compose.yml` for local Postgres + Redis is included as an alternative, if Docker
works on your machine:

```bash
docker compose up -d
# then use: postgresql+psycopg://abhiaya:abhiaya@localhost:5433/abhiaya
```

### 2. Backend

```bash
cd backend
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

cp .env.example .env        # then fill it in — see below
```

Fill in `backend/.env`:

```ini
DEBUG=true

# Note the driver prefix: postgresql+psycopg://  (not plain postgresql://)
DATABASE_URL=postgresql+psycopg://user:pass@host/dbname?sslmode=require
REDIS_URL=redis://localhost:6379/0

GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile

ULTRAMSG_INSTANCE_ID=
ULTRAMSG_TOKEN=
ULTRAMSG_WEBHOOK_SECRET=      # required in production; see Security below

JWT_SECRET=change-me-in-production
```

Create the schema and load demo data:

```bash
./.venv/Scripts/alembic.exe upgrade head
PYTHONPATH=. ./.venv/Scripts/python.exe -m app.seed
```

Run it:

```bash
PYTHONPATH=. ./.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

API docs at `http://localhost:8000/docs`.

### 3. Frontend

```bash
cd frontend
npm install
echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
npm run dev
```

Dashboards at `http://localhost:3000`.

> **Port conflicts:** if 3000/8000 are taken, use any ports you like — but the backend's
> CORS allowlist (`cors_origins` in `backend/app/core/config.py`) must include the
> frontend's origin, and `NEXT_PUBLIC_API_URL` must point at the backend. `NEXT_PUBLIC_*`
> variables are baked in at build time, so **rebuild the frontend after changing it** — a
> stale value is a silent, confusing failure.

---

## Tests

```bash
docker compose up -d
cd backend
DATABASE_URL=postgresql+psycopg://abhiaya:abhiaya@localhost:5433/abhiaya_test pytest
```

No Groq key, no network, no payment credentials needed. Migrations and demo data are
applied automatically. Each test runs in a transaction that is rolled back, so they can
run in any order, and the suite refuses to run against a database not named `*_test`.
See [`backend/tests/README.md`](backend/tests/README.md).

These tests exist to protect specific things that have already gone wrong once: the AI
inventing prices, one restaurant reading another's orders, a forged payment callback
marking an order paid, an admin refunding more than a customer ever paid. **Please run
them before changing anything under `app/services/` or `app/api/`.**

## Demo logins

All use the password **`demo1234`**.

| Role | Email |
|---|---|
| Admin | `admin@abhiaya.pk` |
| Restaurant | `owner@pizzajunction.pk` |
| Restaurant | `owner@karachibiryani.pk` |
| Restaurant | `owner@wokandroll.pk` |

`python -m app.seed` is **idempotent** — re-run it any time to reset menus, hours, and
plans to a known-good state. It updates restaurants in place and **never deletes orders**.

---

## WhatsApp (UltraMsg)

Point your UltraMsg instance's webhook at:

```
https://<your-host>/webhooks/ultramsg?secret=<ULTRAMSG_WEBHOOK_SECRET>
```

For local testing, expose your machine with something like `ngrok http 8000` and use the
public URL.

### ⚠️ The 24-hour window (read this before building notifications)

Meta's WhatsApp Business API only permits free-form messages **within 24 hours of the
customer's last inbound message**. Anything outside that window — a delayed order-status
update, a marketing broadcast, a re-engagement nudge — requires a **pre-approved message
template**.

UltraMsg does not enforce this, so everything will appear to work in development and then
break on the migration to the official Meta API. Any notification feature must be built
with a template abstraction from day one. `backend/app/services/whatsapp.py` is where that
check will live; it is deliberately the only place the app sends a message.

---

## Security notes

Things that are load-bearing. Please don't quietly undo them.

- **The webhook secret is mandatory in production.** Without it, `/webhooks/ultramsg` is a
  public endpoint that spends money on Groq calls and writes to your database. The app
  refuses to serve webhook traffic if `ULTRAMSG_WEBHOOK_SECRET` is unset and `DEBUG=false`.
- **The AI is never trusted with prices.** It passes item ids; prices are read from the
  database and snapshotted onto the order. Without this, a customer can talk the model into
  a Rs. 1 pizza.
- **The AI can only order items it has actually been shown.** `add_to_cart` rejects any
  `menu_item_id` that `get_menu` did not return in this conversation — the model has been
  observed inventing plausible ids and ordering from the wrong restaurant.
- **Tenant isolation is enforced server-side.** Restaurant staff are scoped to their own
  `restaurant_id` from the JWT; a `restaurant_id` in a request body or path is never
  trusted. The frontend's auth check is a convenience redirect, not a security boundary.
- **Restaurants cannot edit their own commission rate.** The settings endpoint physically
  cannot express it.
- **Order state transitions are validated.** A restaurant can only move an order forward
  along legal edges — a delivered order cannot go back to "preparing".

---

## Known constraints and gotchas

- **Groq's free tier is 100,000 tokens/day.** One customer conversation costs roughly
  10–15k tokens, so the free tier supports **around 7–10 conversations per day**. That is
  fine for development and *not* enough for a client demo, let alone production. Budget for
  the paid Dev tier.
- **`llama-3.3-70b-versatile` emits malformed tool calls fairly often.** The agent retries
  them (`MAX_MALFORMED_RETRIES` in `backend/app/services/agent.py`) and hard-blocks raw
  tool-call JSON from ever reaching a customer. If you swap models, keep those guards.
- **A restaurant with no working hours set is treated as always open**, not always closed.
  This is deliberate — the alternative would have made every existing restaurant vanish
  from WhatsApp the moment the feature shipped.
- **"Today" means today in Karachi**, not UTC. Anything time-windowed uses `Asia/Karachi`.

---

## Layout

```
backend/
  app/
    api/          # routes: auth, restaurant, admin, webhooks
    core/         # config, database, security (JWT + bcrypt)
    models/       # SQLAlchemy models
    services/     # agent (Groq), tools, whatsapp, opening_hours, ratings
    seed.py       # idempotent demo data
  alembic/        # migrations
frontend/
  src/
    app/
      login/
      restaurant/ # orders, menu, ratings, settings
      admin/      # overview, restaurants, orders, plans
    components/
    lib/          # api client, types
docs/
  PAYMENTS_PLAN.md
ABHIAYA_MASTER_PLAN.md
```

---

## Roadmap

Phase 1 (V1) per the master plan: JazzCash/EasyPaisa payments, order-status notifications,
customer rating collection over WhatsApp, saved addresses and reorder, coupons, image
upload for menu items, and onboarding the first real 20 restaurants.

See [`ABHIAYA_MASTER_PLAN.md`](ABHIAYA_MASTER_PLAN.md) for the full phased plan.
