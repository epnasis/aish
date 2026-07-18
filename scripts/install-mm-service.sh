#!/usr/bin/env bash
# One-time (or config-change) setup of the aish-web launchd service on the
# always-on host. Secrets come from THIS shell's environment and are written
# only to the remote plist (chmod 600) — never into the repo.
#
#   AISH_WEB_TOKEN=$(openssl rand -hex 16) GEMINI_API_KEY=... \
#       scripts/install-mm-service.sh [host] [model]
set -euo pipefail

HOST="${1:-mm}"
MODEL="${2:-gemini:gemini-3.5-flash}"
: "${AISH_WEB_TOKEN:?set AISH_WEB_TOKEN (e.g. \$(openssl rand -hex 16))}"

GEMINI_LINE=""
if [ -n "${GEMINI_API_KEY:-}" ]; then
    GEMINI_LINE="        <key>GEMINI_API_KEY</key><string>${GEMINI_API_KEY}</string>"
fi

REMOTE_HOME="$(ssh "$HOST" 'echo $HOME')"
PLIST_PATH="$REMOTE_HOME/Library/LaunchAgents/com.epnasis.aish-web.plist"

ssh "$HOST" "mkdir -p ~/Library/LaunchAgents ~/Library/Logs && cat > '$PLIST_PATH' && chmod 600 '$PLIST_PATH'" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.epnasis.aish-web</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REMOTE_HOME}/.local/bin/aish-web</string>
        <string>--host</string><string>0.0.0.0</string>
        <string>--model</string><string>${MODEL}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AISH_WEB_TOKEN</key><string>${AISH_WEB_TOKEN}</string>
${GEMINI_LINE}
        <key>HOME</key><string>${REMOTE_HOME}</string>
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
ssh "$HOST" 'launchctl bootout "gui/$(id -u)/com.epnasis.aish-web" 2>/dev/null; launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.epnasis.aish-web.plist && launchctl kickstart "gui/$(id -u)/com.epnasis.aish-web"'
sleep 2
ssh "$HOST" 'curl -s -o /dev/null -w "health: HTTP %{http_code}\n" http://127.0.0.1:8787/'
echo "✓ service installed — open http://$HOST.local:8787/?token=$AISH_WEB_TOKEN"
