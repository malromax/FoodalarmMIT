#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="free-food-alarm.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"

cd "${APP_DIR}"

if [ ! -f ".env" ]; then
  echo "Missing .env in ${APP_DIR}" >&2
  echo "Create it from .env.example before running setup." >&2
  exit 1
fi

python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install requests gpiozero

TMP_SERVICE="$(mktemp)"
cat > "${TMP_SERVICE}" <<EOF
[Unit]
Description=MIT Free Food Alarm
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/free_food_alarm.py --interval 30
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 0644 "${TMP_SERVICE}" "${SERVICE_FILE}"
rm -f "${TMP_SERVICE}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo "Installed and started ${SERVICE_NAME}"
echo "Check status with: systemctl status ${SERVICE_NAME}"
echo "Follow logs with: journalctl -u ${SERVICE_NAME} -f"
