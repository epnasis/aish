#!/usr/bin/env bash
# Ship the current working tree to the machine running aish-web as a
# service and restart it. Repeatable — run after every change you want
# live. One-time service setup: scripts/install-web-service.sh
set -euo pipefail

HOST="${1:?usage: deploy-web.sh <ssh-host>}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="${AISH_REMOTE_DIR:-dev/aish}"
LABEL="com.aish.web"

echo "→ syncing repo to $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p ~/$REMOTE_DIR"
rsync -a --delete \
    --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
    --exclude .ruff_cache --exclude '*.egg-info' \
    "$REPO_DIR/" "$HOST:$REMOTE_DIR/"

echo "→ reinstalling aish on $HOST"
ssh "$HOST" "~/.local/bin/uv tool install --force --reinstall --no-cache ~/$REMOTE_DIR >/dev/null 2>&1 && ls ~/.local/bin/aish-web >/dev/null && echo '  installed'"

echo "→ restarting aish-web service"
if ! ssh "$HOST" "launchctl kickstart -k \"gui/\$(id -u)/${LABEL}\"" 2>/dev/null; then
    echo "  service not installed — run scripts/install-web-service.sh once"
    exit 1
fi

sleep 2
# The service may bind one interface only, so probe whatever it listens on.
ssh "$HOST" 'ADDR=$(netstat -an | awk "/\.8787.*LISTEN/{print \$4; exit}" | sed "s/\.8787$//"); [ "$ADDR" = "*" ] && ADDR=127.0.0.1; curl -s -o /dev/null --connect-timeout 5 -w "  health (${ADDR}): HTTP %{http_code}\n" "http://${ADDR}:8787/"'
echo "✓ deployed"
