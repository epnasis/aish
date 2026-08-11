#!/usr/bin/env bash
# Ship the current working tree to the machine running aish-web as a
# service and restart it. Repeatable — run after every change you want
# live. One-time service setup: scripts/install-web-service.sh
#
# Shipping the WORKING TREE is deliberate here (that is the point of a remote
# dev loop), but doing it by accident is not: an uncommitted file you forgot
# about goes out just the same. So a dirty tree warns and asks, unless
# --dirty says you meant it. Same reasoning as scripts/ship.sh, which refuses
# outright because the local path has no iterate-against-a-remote excuse.
#
#   scripts/deploy-web.sh <ssh-host>
#   scripts/deploy-web.sh <ssh-host> --dirty
set -euo pipefail

HOST="${1:?usage: deploy-web.sh <ssh-host> [--dirty]}"
ALLOW_DIRTY=0
[ "${2:-}" = "--dirty" ] && ALLOW_DIRTY=1
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="${AISH_REMOTE_DIR:-dev/aish}"
LABEL="com.aish.web"

DIRTY="$(cd "$REPO_DIR" && git status --porcelain 2>/dev/null || true)"
if [ -n "$DIRTY" ] && [ "$ALLOW_DIRTY" -eq 0 ]; then
    echo "⚠ the working tree is not clean — these uncommitted changes will ship:"
    printf '%s\n' "$DIRTY" | sed 's/^/    /'
    if [ -t 0 ]; then
        printf 'continue? [y/N] '
        read -r reply
        case "$reply" in [yY]*) ;; *) echo "aborted"; exit 1 ;; esac
    else
        # Non-interactive (CI, a script, an agent): never guess. Silence is not
        # consent, and this is the case that shipped someone else's half-done
        # work while nobody was looking at the terminal.
        echo "  no tty to confirm on — commit, stash, or pass --dirty" >&2
        exit 1
    fi
fi

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

# The service may bind one interface only, so probe whatever it listens
# on — retrying while it comes back up after the restart.
ssh "$HOST" 'for i in 1 2 3 4 5 6 7 8 9 10; do
    ADDR=$(netstat -an | awk "/\.8787.*LISTEN/{print \$4; exit}" | sed "s/\.8787$//")
    if [ -n "$ADDR" ]; then
        [ "$ADDR" = "*" ] && ADDR=127.0.0.1
        curl -s -o /dev/null --connect-timeout 5 -w "  health (${ADDR}): HTTP %{http_code}\n" "http://${ADDR}:8787/"
        exit 0
    fi
    sleep 1
done
echo "  health: no listener on 8787 after 10s — check ~/Library/Logs/aish-web.log"
exit 1'
echo "✓ deployed"
