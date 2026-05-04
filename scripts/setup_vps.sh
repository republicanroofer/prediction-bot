#!/usr/bin/env bash
# setup_vps.sh — one-shot VPS provisioning for Ubuntu 24.04
# Run as root on a fresh Hetzner VPS:
#   curl -fsSL https://raw.githubusercontent.com/.../setup_vps.sh | bash

set -euo pipefail

DEPLOY_USER="bot"
APP_DIR="/opt/prediction-bot"
DB_NAME="predbot"
DB_USER="predbot"
DB_PASS="${DB_PASS:-$(openssl rand -hex 16)}"

echo "==> Updating system packages"
apt-get update -qq && apt-get upgrade -y -qq

echo "==> Installing dependencies"
apt-get install -y -qq \
  git curl wget unzip \
  python3.12 python3.12-venv python3-pip \
  postgresql postgresql-contrib \
  nginx certbot python3-certbot-nginx \
  ufw fail2ban

echo "==> Creating deploy user: $DEPLOY_USER"
id "$DEPLOY_USER" &>/dev/null || useradd -m -s /bin/bash "$DEPLOY_USER"

echo "==> Configuring PostgreSQL"
systemctl enable postgresql && systemctl start postgresql
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'\" | grep -q 1 || psql -c \"CREATE USER $DB_USER WITH PASSWORD '$DB_PASS'\""
su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='$DB_NAME'\" | grep -q 1 || psql -c \"CREATE DATABASE $DB_NAME OWNER $DB_USER\""
su - postgres -c "psql -c \"GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER\""

echo "==> Firewall (UFW)"
ufw --force enable
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw allow 5432/tcp  # postgres (restrict to localhost in production)

echo "==> Installing Node.js 20 (for frontend build)"
if ! command -v node &>/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

echo "==> Setting up app directory: $APP_DIR"
mkdir -p "$APP_DIR"
chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

echo ""
echo "===================================================================="
echo "  VPS setup complete."
echo ""
echo "  DB credentials (save these):"
echo "    DB_USER=$DB_USER"
echo "    DB_PASS=$DB_PASS"
echo "    DB_NAME=$DB_NAME"
echo "    DATABASE_URL=postgresql://$DB_USER:$DB_PASS@localhost/$DB_NAME"
echo ""
echo "  Next: run scripts/deploy.sh from your local machine"
echo "===================================================================="
