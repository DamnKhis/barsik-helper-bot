up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api bot

index:
	docker compose exec api python -m indexer.run_index --docs-dir /app/docs

seed:
	python scripts/seed_stats.py --n 120

lint:
	ruff check .

format:
	ruff check --fix .

test:
	pytest -q
