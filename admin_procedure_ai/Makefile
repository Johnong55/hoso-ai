.PHONY: install dev run worker beat test migrate upgrade downgrade lint format

# ── Setup ─────────────────────────────────────────────────────────────────────
install:
	pip install -r requirements.txt
	playwright install chromium

install-dev:
	pip install -r requirements.txt
	pip install black isort ruff mypy
	playwright install chromium

# ── Run ───────────────────────────────────────────────────────────────────────
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

worker:
	celery -A app.worker.celery_app worker --loglevel=info --concurrency=4

beat:
	celery -A app.worker.celery_app beat --loglevel=info

flower:
	celery -A app.worker.celery_app flower --port=5555

# ── Docker ────────────────────────────────────────────────────────────────────
up:
	docker-compose up -d

down:
	docker-compose down

logs:
	docker-compose logs -f api

rebuild:
	docker-compose up -d --build

# ── Database ──────────────────────────────────────────────────────────────────
migrate:
	alembic revision --autogenerate -m "$(msg)"

upgrade:
	alembic upgrade head

downgrade:
	alembic downgrade -1

db-history:
	alembic history --verbose

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	pytest tests/ -v --asyncio-mode=auto

test-unit:
	pytest tests/unit/ -v --asyncio-mode=auto

test-integration:
	pytest tests/integration/ -v --asyncio-mode=auto

test-cov:
	pytest tests/ -v --asyncio-mode=auto --cov=app --cov-report=html

# ── Linting ───────────────────────────────────────────────────────────────────
lint:
	ruff check app/
	mypy app/ --ignore-missing-imports

format:
	black app/ tests/
	isort app/ tests/
