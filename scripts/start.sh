#!/usr/bin/env bash
# Start the whole opkura dev stack, in order, with health checks:
#
#   1. cloudflared quick tunnel  → rewrites AUTOCALLS_PUBLIC_URL in .env
#      (Twilio must reach the media websocket; the URL changes every start)
#   2. opkura serve   (:8765 — API + embedded voice worker)
#   3. opkura worker  (queue worker: converse-text / evaluate / settle)
#   4. opkura-web     (:3400 — the console)
#
#   ./scripts/start.sh          start everything
#   ./scripts/stop.sh           stop everything
#
# Logs land in .dev/*.log (git-ignored). Re-running is safe: each part is
# stopped first.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .dev

say() { printf '\033[1;36m== %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- stop old
./scripts/stop.sh >/dev/null 2>&1 || true

# ------------------------------------------------------------------ tunnel
say "tunnel"
nohup cloudflared tunnel --url http://localhost:8765 > .dev/tunnel.log 2>&1 < /dev/null &
echo $! > .dev/tunnel.pid
URL=""
for i in $(seq 1 20); do
  URL=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" .dev/tunnel.log | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || { echo "tunnel never printed a URL — see .dev/tunnel.log"; exit 1; }
sed -i '' "s|^AUTOCALLS_PUBLIC_URL=.*|AUTOCALLS_PUBLIC_URL=$URL|" .env
echo "   $URL  (written to .env)"

# ----------------------------------------------------------------- backend
say "opkura serve (:8765) + worker"
# shellcheck disable=SC1090
source "$HOME/.opkura-obs.env"   # LOG_SINK / PHOENIX / LANGFUSE / TRACE_CAPTURE
nohup .venv/bin/python -m opkura serve  > .dev/serve.log  2>&1 < /dev/null & echo $! > .dev/serve.pid
nohup .venv/bin/python -m opkura worker > .dev/worker.log 2>&1 < /dev/null & echo $! > .dev/worker.pid
for i in $(seq 1 20); do
  curl -sf --max-time 3 localhost:8765/health >/dev/null 2>&1 && break; sleep 1
done
curl -sf --max-time 3 localhost:8765/health >/dev/null || { echo "API never came up — see .dev/serve.log"; exit 1; }
echo "   api healthy; worker registering"

# --------------------------------------------------------------------- web
say "opkura-web (:3400)"
if [ -d ../opkura-web ]; then
  (cd ../opkura-web && nohup npm run start > ../opkura/.dev/web.log 2>&1 < /dev/null & echo $! > ../opkura/.dev/web.pid)
  for i in $(seq 1 15); do
    curl -sf --max-time 3 -o /dev/null localhost:3400/ 2>/dev/null && break; sleep 1
  done
  echo "   console: http://localhost:3400"
else
  echo "   ../opkura-web not found — skipped"
fi

say "up"
echo "   console   http://localhost:3400"
echo "   api       http://localhost:8765/health"
echo "   grafana   http://localhost:3001   phoenix http://localhost:6006   langfuse http://localhost:3200"
echo "   tunnel    $URL"
echo "   logs      .dev/*.log     stop: ./scripts/stop.sh"
