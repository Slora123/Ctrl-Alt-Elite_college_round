#!/usr/bin/env bash
# Run LOCALLY:  deploy/ec2/deploy.sh ubuntu@<host> [path/to/key.pem]
# Copies the code, installs everything, and sends your LLM key over ssh stdin (never on a command line, never into git).
set -euo pipefail
HOST="${1:?usage: deploy.sh user@host [keyfile]}"; KEY="${2:-}"
SSH=(ssh -o StrictHostKeyChecking=accept-new ${KEY:+-i "$KEY"}); ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
"${SSH[@]}" "$HOST" 'echo connected'
rsync -az --delete -e "${SSH[*]}" --exclude .venv --exclude node_modules --exclude .cg --exclude '.env*' --exclude dist --exclude .git \
  "$ROOT/" "$HOST:~/crisisgrid-src/"
KEYLINE="$(grep -hE '^(NVIDIA_NIM_API_KEY)=' "$ROOT/.cg/env" "$ROOT/.env" 2>/dev/null | head -1 || true)"
if [ -n "$KEYLINE" ]; then
  printf '%s\n' "$KEYLINE" | "${SSH[@]}" "$HOST" 'sudo grep -q "^NVIDIA_NIM_API_KEY=" /etc/crisisgrid/env 2>/dev/null && echo "key already on the server" || { sudo mkdir -p /etc/crisisgrid; sudo tee -a /etc/crisisgrid/env >/dev/null; echo "key stored in /etc/crisisgrid/env"; }'
fi
"${SSH[@]}" "$HOST" 'bash ~/crisisgrid-src/deploy/ec2/setup.sh'
# the setup creates the env file only when it is missing, so a key sent above may precede it: re-run once to be safe
"${SSH[@]}" "$HOST" 'sudo systemctl restart crisisgrid-api'
