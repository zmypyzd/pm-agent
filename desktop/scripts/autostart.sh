#!/usr/bin/env bash
# Install / uninstall the pm-agent pet as a macOS LaunchAgent so it starts
# on login. Idempotent.
#
# Usage:
#   ./autostart.sh install [/path/to/pm-agent-pet.app]
#   ./autostart.sh uninstall
#   ./autostart.sh status
#
# Default app location when none is passed: /Applications/pm-agent-pet.app

set -euo pipefail

LABEL="com.pmagent.pet"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

usage() {
    sed -n '2,11p' "$0"
    exit 1
}

cmd_install() {
    local app="${1:-/Applications/pm-agent-pet.app}"
    if [ ! -d "$app" ]; then
        echo "error: app not found at $app" >&2
        echo "       pass a path or move pm-agent-pet.app to /Applications/" >&2
        exit 2
    fi
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-a</string>
        <string>${app}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
EOF
    # bootout silently if already loaded; then bootstrap fresh.
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "[autostart] installed at $PLIST"
    echo "[autostart] will launch $app on every login"
}

cmd_uninstall() {
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST"
    echo "[autostart] uninstalled"
}

cmd_status() {
    if [ -f "$PLIST" ]; then
        echo "plist:      $PLIST"
        if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
            echo "launchctl:  loaded"
        else
            echo "launchctl:  plist present but not loaded (run install)"
        fi
    else
        echo "plist:      not installed"
    fi
}

case "${1:-}" in
    install)   shift; cmd_install "${1:-}" ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status ;;
    *)         usage ;;
esac
