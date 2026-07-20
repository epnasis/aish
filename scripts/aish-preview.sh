#!/usr/bin/env bash
# Run THIS checkout's aish-web as a branch preview, beside production.
#
# Production (com.aish.web) keeps serving the installed build on :8787; this
# serves the working tree on :8788 from source (uv run), so you can A/B the
# two on your phone before shipping. Reverse-proxy it on the same origin as
# prod (path /preview/) so the browser token is shared — see the nginx block
# in README (Branch preview). Reusable for any future branch: it always runs
# the tree the script lives in.
#
# SHARED STATE: preview points at prod's AISH_STATE_DIR, so sessions, the
# command audit log, the allowlist and ~/.config/aish knowledge are the SAME
# in both UIs (that's the point — compare on identical data). The append-only
# session logs have no cross-process lock, so DO NOT drive the *same* session
# from both / and /preview/ at once — use different or throwaway sessions on
# preview.
#
# Secrets (token + provider key) are read from this shell's environment, or,
# if unset, from the local prod plist as a convenience — never from the repo.
#
#   scripts/aish-preview.sh            # uses prod plist secrets, :8788
#   AISH_PREVIEW_PORT=9001 scripts/aish-preview.sh
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${AISH_PREVIEW_HOST:-192.168.10.20}"
PORT="${AISH_PREVIEW_PORT:-8788}"
MODEL="${AISH_PREVIEW_MODEL:-${AISH_MODEL:-gemini:gemini-3.5-flash}}"
PROD_PLIST="$HOME/Library/LaunchAgents/com.aish.web.plist"

# Fall back to the prod plist for any secret not already in the environment.
plist_env() { # plist_env KEY -> value from EnvironmentVariables, or empty
    /usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:$1" "$PROD_PLIST" 2>/dev/null || true
}
for var in AISH_WEB_TOKEN GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
    if [ -z "${!var:-}" ]; then
        value="$(plist_env "$var")"
        [ -n "$value" ] && export "$var=$value"
    fi
done

: "${AISH_WEB_TOKEN:?set AISH_WEB_TOKEN (or run where the prod plist has it) — a non-localhost bind needs a token}"

# Prod's state dir is the default (~/.local/state/aish); set it explicitly so
# the shared-state contract is visible and survives a future default change.
export AISH_STATE_DIR="${AISH_STATE_DIR:-$HOME/.local/state/aish}"

echo "→ preview: ${MODEL} on http://${HOST}:${PORT}/  (state: ${AISH_STATE_DIR})"
echo "  proxy /preview/ → ${HOST}:${PORT} to reach it at https://aish.wenda.eu/preview/"
echo "  ⚠ shared sessions with prod — don't run the same session on both at once"
exec uv run --project "$PROJECT" aish-web --host "$HOST" --port "$PORT" --model "$MODEL"
