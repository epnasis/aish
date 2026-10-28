#!/usr/bin/env bash
# Schedule `aish-mailreport` as a launchd calendar job on THIS machine (#142
# W5): the daily inbox report, every day at 07:30 local time, delivered as a
# new aish-web chat through POST /trigger. It READS the mail state tree (the
# verdict store, the index, the watcher's health) and writes only its own
# ledger beside them; a failed delivery shows in /mail.
#
#   scripts/install-mailreport-service.sh [--print] <gmail-account>
#
#   --print          write the plist to stdout and change nothing
#   gmail-account    the Gmail account the watcher reads — only for the
#                    "open in Gmail" links the report builds
#
# A code-owned job, never an automation file: nothing here is written into
# the automations or runs stores, whose only author is the approval card.
# The /trigger token is read at run time from the Keychain (service "aish",
# account AISH_WEB_TOKEN), never written into the plist. When AISH_ENV_FILE
# (default ~/.config/mm-config/aish-web.env, the file aish-web's job loads)
# exists, the job starts through ~/.local/bin/envrun with it, as aish-web
# does, so it reads the same AISH_OWNER_ADDRESSES and state dir.
set -euo pipefail

PRINT=0
if [ "${1:-}" = "--print" ]; then
    PRINT=1
    shift
fi
ACCOUNT="${1:?usage: install-mailreport-service.sh [--print] <gmail-account>}"

LABEL="com.aish.mailreport"
HOUR="${AISH_MAILREPORT_HOUR:-7}"
MINUTE="${AISH_MAILREPORT_MINUTE:-30}"
WEB_URL="${AISH_WEB_URL:-http://192.168.10.20:8787}"
ENV_FILE="${AISH_ENV_FILE:-$HOME/.config/mm-config/aish-web.env}"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="$HOME/Library/Logs/aish-mailreport.log"

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
    arg "$HOME/.local/bin/aish-mailreport"
    arg "--account"
    arg "$ACCOUNT"
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
$(program_args)
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>$(xml_escape "$HOME")</string>
        <key>PATH</key><string>$(xml_escape "$HOME")/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>AISH_WEB_URL</key><string>$(xml_escape "$WEB_URL")</string>
    </dict>
    <key>WorkingDirectory</key><string>$(xml_escape "$HOME")</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>${HOUR}</integer>
        <key>Minute</key><integer>${MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>$(xml_escape "$LOG_PATH")</string>
    <key>StandardErrorPath</key><string>$(xml_escape "$LOG_PATH")</string>
</dict>
</plist>
EOF
}

if [ "$PRINT" = 1 ]; then
    plist
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
plist > "$PLIST_PATH"
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
printf '✓ %s installed: daily at %02d:%02d, log %s; its health shows in /mail\n' \
    "$LABEL" "$HOUR" "$MINUTE" "$LOG_PATH"
