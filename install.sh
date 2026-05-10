#!/usr/bin/env bash
# Windrose Bot installer.
# Usage: sudo ./install.sh

set -euo pipefail

INSTALL_DIR="/opt/windrose-bot"
SERVICE_NAME="windrose-bot"
LOG_FILE="/var/log/windrose-bot.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root (sudo ./install.sh)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Verifying prerequisites"
command -v "$PYTHON_BIN" >/dev/null || { echo "ERROR: $PYTHON_BIN not found" >&2; exit 1; }
command -v docker >/dev/null || { echo "ERROR: docker not found" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: 'docker compose' not available" >&2; exit 1; }

echo "==> Creating $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

echo "==> Copying files"
cp "$SCRIPT_DIR/bot.py"          "$INSTALL_DIR/bot.py"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
chmod 644 "$INSTALL_DIR/bot.py" "$INSTALL_DIR/requirements.txt"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
        cp "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env"
    else
        touch "$INSTALL_DIR/.env"
    fi
    chmod 600 "$INSTALL_DIR/.env"
    echo "    Created $INSTALL_DIR/.env — fill it in before starting the service."
else
    echo "    $INSTALL_DIR/.env already exists, leaving it alone."
fi

echo "==> Setting up Python virtualenv"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip >/dev/null
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "==> Preparing log file"
touch "$LOG_FILE"
chmod 644 "$LOG_FILE"

echo "==> Installing systemd unit"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Windrose Server Telegram Bot
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/bot.py
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal
KillSignal=SIGINT
TimeoutStopSec=15s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "==> Done."
echo ""
echo "Next steps:"
echo "  1. Edit configuration:  sudo nano $INSTALL_DIR/.env"
echo "  2. Enable and start:    sudo systemctl enable --now $SERVICE_NAME"
echo "  3. Watch logs:          sudo journalctl -u $SERVICE_NAME -f"
echo "  4. Status:              sudo systemctl status $SERVICE_NAME"
