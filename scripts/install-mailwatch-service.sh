#!/usr/bin/env bash
# Schedule `aish-mailwatch --once` as a launchd interval job on THIS machine
# (#142 W5 S1.5): the inbox assistant's shadow pass over both inboxes, every
# 15 minutes. NOT inside aish-web. It writes only the mail state tree; the
# owner reads the results and the watcher's health with /mail.
#
#   scripts/install-mailwatch-service.sh [--print] <gmail-account> <model> [owner-address...]
#
#   --print          write the plist to stdout and change nothing
#   gmail-account    needs ~/.config/gws-accounts/<account>/ (aish-mailwatch
#                    refuses to run without it; it sets the gws file-keyring
#                    variables itself — the email-poll outage lesson)
#   model            the Reader's model spec (it must be admitted:
#                    scripts/role-admission.py --model <spec> mail-reader)
#   owner-address    every address the owner sends from (repeatable). The
#                    Gmail account and the Tuta bridge's own address are
#                    added by aish-mailwatch itself.
#
# Provider keys are NOT written into the plist: when AISH_ENV_FILE (default
# ~/.config/mm-config/aish-web.env, the file aish-web's own job loads) exists,
# the job is started through ~/.local/bin/envrun with it, exactly as aish-web.
# The Tuta bridge secret is read by aish-mailwatch from aish's own secret
# store at run time.
set -euo pipefail

PRINT=0
if [ "${1:-}" = "--print" ]; then
    PRINT=1
    shift
fi
ACCOUNT="${1:?usage: install-mailwatch-service.sh [--print] <gmail-account> <model> [owner...]}"
MODEL="${2:?usage: install-mailwatch-service.sh [--print] <gmail-account> <model> [owner...]}"
shift 2

LABEL="com.aish.mailwatch"
INTERVAL_S="${AISH_MAILWATCH_INTERVAL:-900}"
ENV_FILE="${AISH_ENV_FILE:-$HOME/.config/mm-config/aish-web.env}"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="$HOME/Library/Logs/aish-mailwatch.log"

xml_escape() {
    local s="$1"
    s="${s//&/&amp;}"
    s="${s//</&lt;}"
    s="${s//>/&gt;}"
    printf '%s' "$s"
}

arg() {
    printf '        <string>%s</string>\n' "$(xml_escape "$1")"
}

program_args() {
    if [ -f "$ENV_FILE" ] && [ -x "$HOME/.local/bin/envrun" ]; then
        arg "$HOME/.local/bin/envrun"
        arg "$ENV_FILE"
    fi
    arg "$HOME/.local/bin/aish-mailwatch"
    arg "--once"
    arg "--source"
    arg "gmail"
    arg "--source"
    arg "tuta"
    arg "--account"
    arg "$ACCOUNT"
    arg "--model"
    arg "$MODEL"
    local owner
    for owner in "$@"; do
        arg "--owner"
        arg "$owner"
    done
}

plist() {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
$(program_args "$@")
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>$(xml_escape "$HOME")</string>
        <key>PATH</key><string>$(xml_escape "$HOME")/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key><string>$(xml_escape "$HOME")</string>
    <key>StartInterval</key><integer>${INTERVAL_S}</integer>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>$(xml_escape "$LOG_PATH")</string>
    <key>StandardErrorPath</key><string>$(xml_escape "$LOG_PATH")</string>
</dict>
</plist>
EOF
}

if [ "$PRINT" = 1 ]; then
    plist "$@"
    exit 0
fi

if [ ! -d "$HOME/.config/gws-accounts/$ACCOUNT" ]; then
    echo "no credential directory $HOME/.config/gws-accounts/$ACCOUNT — aish-mailwatch would refuse to run" >&2
    exit 2
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
plist "$@" > "$PLIST_PATH"
chmod 600 "$PLIST_PATH"
plutil -lint "$PLIST_PATH"

DOMAIN="gui/$(id -u)"
echo "→ (re)loading ${LABEL}"
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
if ! launchctl bootstrap "$DOMAIN" "$PLIST_PATH"; then
    # A job still exiting from the bootout refuses a bootstrap (5: I/O error;
    # docs/web-server.md §ship). It is not loaded now: say how to load it.
    echo "bootstrap failed — the job is NOT loaded; retry: launchctl bootstrap $DOMAIN $PLIST_PATH" >&2
    exit 1
fi
echo "✓ ${LABEL} installed: every ${INTERVAL_S}s, log ${LOG_PATH}; read the results with /mail"
