.PHONY: preview test lint

# Serve this working tree as a branch preview on :8788 beside production.
# See scripts/aish-preview.sh for the shared-state caveat and the /preview/
# reverse-proxy block in the README.
preview:
	@scripts/aish-preview.sh

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy
