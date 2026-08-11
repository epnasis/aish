.PHONY: preview ship ship-check test lint

# Serve this working tree as a branch preview on :8788 beside production.
# See scripts/aish-preview.sh for the shared-state caveat and the /preview/
# reverse-proxy block in the README.
preview:
	@scripts/aish-preview.sh

# Ship this checkout to the locally installed aish and restart the service.
# Refuses on a dirty tree: the wheel is built from the working tree, not from
# HEAD, so uncommitted work ships silently. See scripts/ship.sh.
ship:
	@scripts/ship.sh

# The preflight alone — what would ship, and whether it is allowed to.
ship-check:
	@scripts/ship.sh --check

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy
