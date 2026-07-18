.PHONY: help dev db migrate seed test lint clean

help:
	@echo "Activist Terminal — development commands"
	@echo ""
	@echo "  make dev        Start all services (Postgres + web + api)"
	@echo "  make db         Start Postgres only"
	@echo "  make migrate    Run all migrations (Prisma + Alembic)"
	@echo "  make seed       Seed database with sample campaigns"
	@echo "  make test       Run all tests"
	@echo "  make lint       Lint Python + TypeScript"
	@echo "  make clean      Stop and remove containers + volumes"

dev:
	cp -n .env.example .env 2>/dev/null || true
	docker compose up -d db
	docker compose logs -f db &
	cd web && npm install && npm run dev &
	cd pipelines && pip install -e .[dev] -q && uvicorn activist_pipelines.api.main:app --reload --port 8000

db:
	docker compose up -d db
	@echo "Postgres ready on :5432"

migrate:
	cd web && npx prisma migrate deploy
	cd pipelines && alembic upgrade head

seed:
	cd pipelines && python -m activist_pipelines.jobs.seed

event-study:
	cd pipelines && python -m activist_pipelines.jobs.run_event_study

test:
	cd pipelines && pytest tests/ -v --tb=short
	cd web && npm test -- --watchAll=false

lint:
	cd pipelines && ruff check src/ && mypy src/
	cd web && npm run lint

clean:
	docker compose down -v --remove-orphans

db-shell:
	docker compose exec db psql -U activist -d activist_terminal

api-shell:
	cd pipelines && python -c "from activist_pipelines.db import engine; print('Connected:', engine.url)"
