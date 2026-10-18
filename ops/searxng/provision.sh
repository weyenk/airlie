#!/usr/bin/env bash
# Runs INSIDE the Tart VM (as its admin user, which has passwordless sudo): installs SearXNG at the commit
# given as $1 (ops/searxng/version pins it), as a systemd service on 0.0.0.0:8080 with the JSON format on.
# Safe to run again: it only changes what differs. `task searxng:up` pipes it in with `tart exec -i`.
set -euo pipefail

COMMIT="${1:?usage: provision.sh <searxng commit>}"
DIR=/opt/searxng
export DEBIAN_FRONTEND=noninteractive

export NEEDRESTART_MODE=a   # no "which services to restart" prompt
quietly() {  # run a command, show nothing unless it fails
  local log
  log="$(mktemp)"
  "$@" >"$log" 2>&1 || { tail -n 30 "$log" >&2; rm -f "$log"; return 1; }
  rm -f "$log"
}

echo "[provision] packages"
quietly sudo apt-get update -qq
quietly sudo apt-get install -y -qq python3-dev python3-venv git build-essential libxslt-dev zlib1g-dev libffi-dev libssl-dev curl

echo "[provision] searxng $COMMIT"
sudo mkdir -p "$DIR" /etc/searxng /var/lib/airlie
sudo chown -R "$USER":"$USER" "$DIR"
cd "$DIR"
[ -d .git ] || git init -q
if [ "$(git rev-parse HEAD 2>/dev/null || true)" != "$COMMIT" ]; then
  git fetch -q --depth 1 https://github.com/searxng/searxng "$COMMIT"
  git checkout -q --force FETCH_HEAD
fi

echo "[provision] python environment"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -U pip setuptools wheel pyyaml msgspec typing_extensions
venv/bin/pip install -q --use-pep517 --no-build-isolation -e .

echo "[provision] settings and service"
if [ ! -f /etc/searxng/settings.yml ]; then
  SECRET="$(openssl rand -hex 32)"
  sudo tee /etc/searxng/settings.yml >/dev/null <<EOF
use_default_settings: true
server:
  bind_address: "0.0.0.0"   # the VM's network is private to this Mac; airlie reaches it through a loopback forward
  port: 8080
  secret_key: "$SECRET"
  limiter: false            # no bot detection for a single-user instance
  image_proxy: false
search:
  formats: [html, json]     # web_search reads the JSON format, which SearXNG ships switched off
EOF
fi
sudo tee /etc/systemd/system/searxng.service >/dev/null <<EOF
[Unit]
Description=SearXNG (airlie's web_search backend)
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
Environment=SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
ExecStart=$DIR/venv/bin/python -m searx.webapp
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable searxng >/dev/null 2>&1
echo "$COMMIT" | sudo tee /var/lib/airlie/searxng-commit >/dev/null
echo "[provision] done"
