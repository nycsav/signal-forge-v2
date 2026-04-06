#!/bin/bash
# Signal Forge v2 — Install launchd auto-restart daemon
# Usage: bash scripts/install_daemon.sh

PLIST_PATH="$HOME/Library/LaunchAgents/com.signalforge.v2.plist"
VENV_PYTHON="$HOME/signal-forge-v2/venv/bin/python"
MAIN_PY="$HOME/signal-forge-v2/main.py"
LOG_DIR="$HOME/signal-forge-v2/logs"

mkdir -p "$LOG_DIR"

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.signalforge.v2</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PYTHON</string>
        <string>$MAIN_PY</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HOME/signal-forge-v2</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>$HOME/signal-forge-v2</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/daemon-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/daemon-stderr.log</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF

echo "Plist created at $PLIST_PATH"
echo ""
echo "To start the daemon:"
echo "  launchctl load $PLIST_PATH"
echo ""
echo "To stop:"
echo "  launchctl unload $PLIST_PATH"
echo ""
echo "To check status:"
echo "  launchctl list | grep signalforge"
echo ""
echo "Logs:"
echo "  tail -f $LOG_DIR/daemon-stdout.log"
echo "  tail -f $LOG_DIR/daemon-stderr.log"
