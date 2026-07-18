#!/usr/bin/env bash
# Deploy aish to the always-on host: sync the working tree, reinstall the
# tool, restart the aish-web service, health-check. Repeatable — run after
# every change you want live. One-time service setup: install-mm-service.sh
set -euo pipefail

HOST="${1:-mm}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ syncing repo to $HOST:dev/aish"
ssh "$HOST" 'mkdir -p ~/dev/aish'
rsync -a --delete \
    --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
    --exclude .ruff_cache --exclude '*.egg-info' \
    "$REPO_DIR/" "$HOST:dev/aish/"

echo "→ reinstalling aish on $HOST"
ssh "$HOST" '~/.local/bin/uv tool install --force --reinstall --no-cache ~/dev/aish >/dev/null 2>&1 && ~/.local/bin/aish-web --help >/dev/null && echo "  installed: $(ls ~/.local/bin/aish-web)"'

echo "→ restarting aish-web service"
if ! ssh "$HOST" 'launchctl kickstart -k "gui/$(id -u)/com.epnasis.aish-web"' 2>/dev/null; then
    echo "  service not installed — run scripts/install-mm-service.sh once"
    exit 1
fi

sleep 2
ssh "$HOST" 'curl -s -o /dev/null -w "  health: HTTP %{http_code}\n" http://127.0.0.1:8787/'
echo "✓ deployed"
