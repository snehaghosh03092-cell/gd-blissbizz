# Docker Desktop/Rancher ship the `docker compose` plugin; Colima setups often only have `docker-compose`.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo 'docker compose' || echo docker-compose)

.PHONY: dev-all smoke smoke-db ui-check test-services test-libs setup db-up db-stop db-migrate db-seed db-reseed db-check db-facts schema-doc dev-backend dev-frontend check test-mysql
setup:
	./scripts/setup.sh
db-up:
	$(COMPOSE) up -d --wait db
db-stop:
	$(COMPOSE) stop db
db-migrate:
	cd backend && .venv/bin/python3 -m alembic upgrade head
db-seed:
	cd backend && .venv/bin/python3 -m app.seed $(SEED_ARGS)
db-reseed:
	cd backend && .venv/bin/python3 -m app.seed --reset $(SEED_ARGS)
db-check:
	cd backend && .venv/bin/python3 -m app.seed --check
test-libs:
	cd libs/blissbiz_common && ../../backend/.venv/bin/python3 -m pytest -q && ../../backend/.venv/bin/python3 -m ruff check . && ../../backend/.venv/bin/python3 -m ruff format --check .
db-facts:
	cd backend && .venv/bin/python3 -m app.seed --facts
schema-doc:
	backend/.venv/bin/python3 scripts/gen_schema_doc.py
dev-all:
	./scripts/dev_all.sh
smoke:  # real services + its own seeded blissbiz_smoke database (never touches blissbiz)
	backend/.venv/bin/python3 scripts/smoke_e2e.py
ui-check:  # headless Chrome through every screen; needs dev-all + dev-frontend running (use a test database)
	backend/.venv/bin/python3 scripts/ui_check.py
smoke-db:  # once: create the local blissbiz_smoke database for `make smoke` (root password stays in the container)
	$(COMPOSE) exec -T db sh -c 'MYSQL_PWD="$$MYSQL_ROOT_PASSWORD" mysql -uroot -e "CREATE DATABASE IF NOT EXISTS blissbiz_smoke CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; GRANT ALL PRIVILEGES ON blissbiz_smoke.* TO \`$$MYSQL_USER\`@\`%\`;"'
test-services:
	cd services/gateway && ../../backend/.venv/bin/python3 -m pytest -q && ../../backend/.venv/bin/python3 -m ruff check . && ../../backend/.venv/bin/python3 -m ruff format --check .
	for s in identity store catalog supply sales billing agent; do (cd services/$$s && ../../backend/.venv/bin/python3 -m ruff check . && ../../backend/.venv/bin/python3 -m ruff format --check .) || exit 1; done
dev-backend:
	cd backend && WATCHFILES_FORCE_POLLING=true .venv/bin/python3 -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
dev-frontend:
	./scripts/npm.sh --prefix frontend run dev
check: test-libs test-services
	cd backend && .venv/bin/python3 -m ruff check . && .venv/bin/python3 -m ruff format --check . && .venv/bin/python3 -m pytest
	./scripts/npm.sh --prefix frontend run build
test-mysql:
	cd backend && BLISSBIZ_TEST_MYSQL=1 .venv/bin/python3 -m pytest
