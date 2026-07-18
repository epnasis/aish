#!/usr/bin/env bash
# One-time setup of aish-web as a launchd service on a remote Mac you can
# ssh into (an always-on home server). Re-run to change model/bind/token.
#
# Secrets come from THIS shell's environment and are written only to the
# remote plist (chmod 600) — they never touch the repo:
#
#   AISH_WEB_TOKEN=$(openssl rand -hex 16) GEMINI_API_KEY=... \
#       scripts/install-web-service.sh <ssh-host> [model] [bind-address]
#
# bind-address defaults to 0.0.0.0 (all interfaces); pass the machine's IP
# on one network (e.g. 192.168.1.20) to serve only that subnet. Pass any
# provider key your chosen model needs (GEMINI_API_KEY / OPENAI_API_KEY /
# ANTHROPIC_API_KEY) the same way.
set -euo pipefail

HOST="${1:?usage: install-web-service.sh <ssh-host> [model] [bind-address]}"
MODEL="${2:-gemini:gemini-3.5-flash}"
BIND="${3:-0.0.0.0}"
: "${AISH_WEB_TOKEN:?set AISH_WEB_TOKEN (e.g. \$(openssl rand -hex 16))}"

LABEL="com.aish.web"
ENV_LINES=""
for var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
    value="${!var:-}"
    if [ -n "$value" ]; then
        ENV_LINES="${ENV_LINES}        <key>${var}</key><string>${value}</string>
"
    fi
done

REMOTE_HOME="$(ssh "$HOST" 'echo $HOME')"
PLIST_PATH="$REMOTE_HOME/Library/LaunchAgents/${LABEL}.plist"

ssh "$HOST" "mkdir -p ~/Library/LaunchAgents ~/Library/Logs && cat > '$PLIST_PATH' && chmod 600 '$PLIST_PATH'" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REMOTE_HOME}/.local/bin/aish-web</string>
        <string>--host</string><string>${BIND}</string>
        <string>--model</string><string>${MODEL}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AISH_WEB_TOKEN</key><string>${AISH_WEB_TOKEN}</string>
${ENV_LINES}        <key>HOME</key><string>${REMOTE_HOME}</string>
        <key>PATH</key><string>${REMOTE_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key><string>${REMOTE_HOME}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${REMOTE_HOME}/Library/Logs/aish-web.log</string>
    <key>StandardErrorPath</key><string>${REMOTE_HOME}/Library/Logs/aish-web.log</string>
</dict>
</plist>
EOF

echo "→ (re)loading service"
ssh "$HOST" "launchctl bootout \"gui/\$(id -u)/${LABEL}\" 2>/dev/null; sleep 2; launchctl bootstrap \"gui/\$(id -u)\" '$PLIST_PATH' && launchctl kickstart \"gui/\$(id -u)/${LABEL}\""
sleep 2
ssh "$HOST" "curl -s -o /dev/null --connect-timeout 5 -w 'health: HTTP %{http_code}\n' 'http://${BIND/0.0.0.0/127.0.0.1}:8787/'"
echo "✓ service installed — open http://${BIND/0.0.0.0/<host>}:8787/?token=${AISH_WEB_TOKEN}"
