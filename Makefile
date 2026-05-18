.PHONY: install backfill daily rank qa test lint clean

install:
	uv sync

backfill:
	uv run mfs ingest navs --backfill
	uv run mfs ingest benchmarks
	uv run mfs ingest tbill --backfill
	uv run mfs build scheme-master

daily:
	uv run mfs ingest navs
	uv run mfs ingest benchmarks
	uv run mfs ingest tbill
	uv run mfs build scheme-master
	uv run mfs compute metrics
	uv run mfs rank

rank:
	uv run mfs rank

qa:
	uv run mfs validate

status:
	uv run mfs status

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff format src tests

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache
