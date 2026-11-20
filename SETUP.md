# BlissBiz – Setup and Run Guide

## Prerequisites

| Tool | Version |
|---|---|
| Python | 3.11 – 3.13 |
| Node.js | 24 LTS (see `.nvmrc`; Node 20 cannot build Vite 7) |
| Docker + Compose | for the local MySQL container |
| make + bash | macOS / Linux / WSL / Git Bash (Windows users: use **WSL**) |

> `uvloop` (in `backend/requirements.lock`) does not install on native Windows. Use WSL, or remove that line before installing.

## 1. Configure

```sh
make setup        # creates .env files with random DB passwords, Python venv, installs deps
```

If Python isn't detected: `PYTHON_BIN=/path/to/python3.13 make setup`.

### Claude (Anthropic) – optional

Agents use **Anthropic Claude** (Groq is not used). Add to `backend/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
CHAT_MODEL=claude-haiku-5-5     # tool selection, shopper replies
GUIDE_MODEL=claude-sonnet-5-5   # Business Guide
```

Without a key the app still runs; only the LLM features (Business Guide, free-form questions) are disabled. Never commit `.env`.

## 2. Database and migrations

Migrations (`backend/migrations/`) are **not included in this repository**. Obtain the `backend/migrations/` folder (versions `0001`–`0016`) from the project owner and place it in `backend/migrations/` before running:

```sh
make db-up        # start MySQL (127.0.0.1:3307)
make db-migrate   # apply migrations
make db-reseed    # load demo data (7 businesses, ~4,400 orders) – local DB only
```

## 3. Run

```sh
make dev-all        # gateway :8000 + identity/store/catalog/supply/sales/billing/agent :8101-8107
make dev-frontend   # UI on http://127.0.0.1:5173 (owner) and http://threads-co.localhost:5173 (shop)
```

Port 8000 busy? `GATEWAY_PORT=8010 make dev-all` and the same for `make dev-frontend`.

Demo logins (dev password is in `docs/13-seed-data.md`): `owner@threads-co.example`, `platform@blissbiz.example`, shopper `aarav.bhatt86@example.com`.

Health checks: `http://127.0.0.1:8000/api/health`, agent `http://127.0.0.1:8107/health` (shows `llm`, `chat_model`, `guide_model`).

## 4. Test

```sh
make check          # lint + backend tests + frontend build
make test-libs
make test-services
make smoke          # end-to-end live checks (needs `make smoke-db` once)
```

Backend tests use SQLite and need no MySQL, but `DATABASE_URL` must be set (any value, e.g. from `backend/.env`).

## 5. Deployment

See `deploy/README.md` (Docker Compose release bundle, nginx, TLS). Set `ANTHROPIC_API_KEY` in the private `deploy/release.env`.

## Layout

```
backend/    shared FastAPI app, models, seed data
services/   gateway, identity, store, catalog, supply, sales, billing, agent
libs/       blissbiz_common shared library
frontend/   Vue 3 + Vite
deploy/     production compose, Dockerfiles, nginx
scripts/    setup, dev runner, smoke tests
docs/       architecture and seed-data reference
```
