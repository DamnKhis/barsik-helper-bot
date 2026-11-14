.PHONY: install ingest sync publish builds rollback eval status fixtures test clean restore query slides

PY := .venv/bin/python
BARS := .venv/bin/bars-kb

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip -q
	.venv/bin/pip install -e .

ingest:
	$(BARS) ingest

sync:
	$(BARS) sync

publish:
	$(BARS) sync --publish

builds:
	$(BARS) builds

rollback:
	$(BARS) rollback

eval:
	$(BARS) eval --top-k 8

status:
	$(BARS) status

fixtures:
	$(BARS) fixtures

test:
	$(PY) -m unittest discover -s tests -v

restore:
	$(PY) examples/restore_from_export.py --export dist --qdrant-url $${QDRANT_URL:-http://127.0.0.1:6333}

query:
	$(PY) examples/query.py "Почему не работает фильтр !> 200?" --top-k 5

clean:
	rm -rf data/qdrant data/cache dist data/builds/*.json
