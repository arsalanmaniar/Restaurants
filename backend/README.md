---
title: AbhiAya Backend
emoji: 🍽️
colorFrom: yellow
colorTo: red
sdk: docker
pinned: false
---

# AbhiAya Backend

FastAPI backend for AbhiAya — an AI-powered WhatsApp multi-restaurant ordering platform.

One WhatsApp number handles orders for multiple restaurants. A Groq-powered agent
takes orders in English or Roman Urdu, routes them to the right kitchen, and staff
work them through a dashboard.

## Stack

- FastAPI + SQLAlchemy 2.0 + Alembic
- Neon PostgreSQL (managed, external)
- psycopg3 (binary distribution)
- Groq LLM API for conversational ordering

## Endpoints

- `GET /health` — liveness check
- `POST /webhooks/wassender?secret=<WASSENDER_WEBHOOK_SECRET>` — Wassender inbound
- `/auth/*`, `/admin/*`, `/restaurant/*` — dashboard APIs

## Required secrets (set in HF Spaces Settings > Variables and secrets)

| Variable | Description |
|---|---|
| `DATABASE_URL` | Neon PostgreSQL connection string |
| `JWT_SECRET` | Random 32+ character string for JWT signing |
| `GROQ_API_KEY` | Groq API key from console.groq.com |
| `WASSENDER_API_KEY` | Wassender API key (Bearer token) |
| `WASSENDER_INSTANCE_ID` | Wassender instance ID |
| `WASSENDER_WEBHOOK_SECRET` | Shared secret appended to the webhook URL |
| `CORS_ALLOWED_ORIGINS` | Comma-separated Vercel frontend URL(s) |
| `PUBLIC_BASE_URL` | This space's public URL (https://<user>-<space>.hf.space) |

Set `DEBUG=false` in production.

For full project documentation see the repository root README.
