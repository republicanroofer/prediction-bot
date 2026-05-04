#!/usr/bin/env bash
# deploy.sh — deploy or update the bot on the VPS
#
# Usage:
#   VPS_HOST=your.server.ip ./scripts/deploy.sh
#
# Assumes:
#   - setup_vps.sh has already been run
#   - SSH key auth is configured for the deploy user
#   - .env file exists locally (it will be rsync'd but not committed)

set -euo pipefail

VPS_HOST="${VPS_HOST:?Set VPS_HOST env var}"
VPS_USER="${VPS_USER:-bot}"
APP_DIR="${APP_DIR:-/opt/prediction-bot}"
BRANCH="${BRANCH:-main}"

echo "==> Building frontend"
cd frontend
npm ci --silent
npm run build
cd ..

echo "==> Syncing code to $VPS_HOST"
rsync -az --delete \
  --exclude='.git' \
  --exclude='frontend/node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  ./ "$VPS_USER@$VPS_HOST:$APP_DIR/"

echo "==> Syncing .env (secrets)"
rsync -az .env "$VPS_USER@$VPS_HOST:$APP_DIR/.env"

echo "==> Remote: install Python deps + run migrations + restart service"
# shellcheck disable=SC2087
ssh "$VPS_USER@$VPS_HOST" bash <<'REMOTE'
set -euo pipefail
APP_DIR="/opt/prediction-bot"
cd "$APP_DIR"

# Python venv
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Apply DB migrations
source .env
psql "$DATABASE_URL" -f backend/db/migrations/001_initial.sql

# Restart systemd service (or create it on first deploy)
if systemctl is-active --quiet predbot; then
  systemctl restart predbot
else
  cat > /etc/systemd/system/predbot.service <<SERVICE
[Unit]
Description=Prediction Bot
After=network.target postgresql.service

[Service]
User=bot
WorkingDirectory=/opt/prediction-bot
EnvironmentFile=/opt/prediction-bot/.env
ExecStart=/opt/prediction-bot/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable predbot
  systemctl start predbot
fi

echo "Service status:"
systemctl status predbot --no-pager -l
REMOTE

echo ""
echo "==> Deploy complete.  Bot running at http://$VPS_HOST:8000"
