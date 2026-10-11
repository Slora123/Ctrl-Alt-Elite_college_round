#!/usr/bin/env bash
# Runs ON the EC2 server (Ubuntu), as the ubuntu user: ./setup.sh   (deploy.sh copies the code and calls this)
# Installs uv + Caddy, the API and worker as systemd services, and HTTPS in front of them. Safe to re-run.
set -euo pipefail
APP=/opt/crisisgrid
ENVF=/etc/crisisgrid/env
DOMAIN="${CG_DOMAIN:-$(curl -s http://checkip.amazonaws.com | tr . -).sslip.io}"   # free hostname for the public IP: Caddy gets a real certificate for it

sudo apt-get update -qq
sudo apt-get install -y -qq git curl rsync debian-keyring debian-archive-keyring apt-transport-https gnupg build-essential >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | sudo gpg --dearmor -o /usr/share/keyrings/caddy.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt | sudo tee /etc/apt/sources.list.d/caddy.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy >/dev/null
fi

# two unprivileged users: the API (holds the secrets) and the worker (runs paper code and must not be able to read them)
id crisisgrid >/dev/null 2>&1 || sudo useradd --system --create-home --shell /usr/sbin/nologin crisisgrid
id cgworker  >/dev/null 2>&1 || sudo useradd --system --create-home --shell /usr/sbin/nologin cgworker
sudo mkdir -p "$APP" /etc/crisisgrid /var/lib/crisisgrid /var/lib/cgworker
sudo rsync -a --delete --exclude .venv --exclude node_modules --exclude .cg --exclude '.env*' --exclude dist --exclude .git \
  "$(dirname "$(readlink -f "$0")")/../../" "$APP/"
sudo chown -R root:root "$APP" && sudo chmod -R a+rX "$APP"
sudo chown crisisgrid:crisisgrid /var/lib/crisisgrid && sudo chown cgworker:cgworker /var/lib/cgworker

# uv for both users (installed system-wide), then one shared virtualenv in /opt (read-only for the service users)
if ! command -v uv >/dev/null; then curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh >/dev/null; fi
(cd "$APP" && sudo UV_PROJECT_ENVIRONMENT="$APP/.venv" uv sync --frozen --no-dev 2>&1 | tail -2)
sudo chmod -R a+rX "$APP/.venv"

# secrets file: each setting is added only if missing, so re-runs keep existing secrets and a key sent earlier by deploy.sh survives
sudo install -d -m 755 /etc/crisisgrid
[ -f "$ENVF" ] || sudo install -m 600 -o root -g root /dev/null "$ENVF"
ensure() { sudo grep -q "^$1=" "$ENVF" || echo "$1=$2" | sudo tee -a "$ENVF" >/dev/null; }
ensure CG_ENV prod
ensure CG_AUTH_ENABLED 1
ensure CG_AUTH_SECRET "$(openssl rand -base64 48 | tr -d '\n=/+')"
ensure CG_WORKER_REGISTRATION_TOKEN "$(openssl rand -base64 32 | tr -d '\n=/+')"
ensure CG_DATABASE_URL sqlite:////var/lib/crisisgrid/crisisgrid.db
ensure CG_DATA_DIR /var/lib/crisisgrid
ensure CG_PAPER_ENV_DIR /var/lib/crisisgrid/paper_env
ensure CG_GENERATE_NOTEBOOKS 1
ensure CG_LLM_CHAIN nvidia_nim/nvidia/nemotron-3-super-120b-a12b
ensure CG_LLM_MAX_TOKENS 4096
sudo chmod 600 "$ENVF"

sudo tee /etc/systemd/system/crisisgrid-api.service >/dev/null <<UNIT
[Unit]
Description=CrisisGrid API
After=network.target
[Service]
User=crisisgrid
WorkingDirectory=$APP/api
EnvironmentFile=$ENVF
ExecStart=$APP/.venv/bin/python -m uvicorn crisisgrid_api.main:app --host 127.0.0.1 --port 8100 --proxy-headers
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT

# the worker gets ONLY the registration token (never the auth secret or API keys), runs as another user, and has no write access to the app
sudo tee /etc/systemd/system/crisisgrid-worker.service >/dev/null <<UNIT
[Unit]
Description=CrisisGrid worker (runs experiment code)
After=crisisgrid-api.service
Requires=crisisgrid-api.service
[Service]
User=cgworker
WorkingDirectory=/var/lib/cgworker
ExecStart=/bin/bash -c 'exec $APP/.venv/bin/python -m crisisgrid_api.worker --api http://127.0.0.1:8100 --name ec2-worker --executor paper_env --paper-env-dir /var/lib/cgworker/paper_env --registration-token "\$(cat /etc/crisisgrid/worker-token)"'
Restart=always
RestartSec=5
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
UNIT
sudo bash -c "grep '^CG_WORKER_REGISTRATION_TOKEN=' $ENVF | cut -d= -f2- > /etc/crisisgrid/worker-token"
sudo chown root:cgworker /etc/crisisgrid/worker-token && sudo chmod 640 /etc/crisisgrid/worker-token

sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$DOMAIN {
  encode gzip
  request_body { max_size 60MB }
  reverse_proxy 127.0.0.1:8100 { flush_interval -1 }   # -1: do not buffer, the run events are a live stream (SSE)
}
CADDY

sudo systemctl daemon-reload
sudo systemctl enable --now crisisgrid-api crisisgrid-worker >/dev/null 2>&1
sudo systemctl restart crisisgrid-api crisisgrid-worker caddy
sleep 4
curl -fsS http://127.0.0.1:8100/health && echo
echo "API:    https://$DOMAIN/health   (needs inbound ports 80 and 443 open in the EC2 security group)"
echo "Vercel: website/vercel.json must rewrite /cg/:path* to https://$DOMAIN/:path*"
