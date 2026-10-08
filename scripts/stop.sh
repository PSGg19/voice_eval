#!/usr/bin/env bash
# Stop everything scripts/start.sh started (tunnel, serve, worker, web).
# Postgres/Redis (brew) and the docker observability stack stay up —
# they're shared infrastructure; stop those with `brew services stop …`
# and `docker compose --profile logs --profile tracing down` if you must.
set -uo pipefail
cd "$(dirname "$0")/.."

for name in tunnel serve worker web; do
  if [ -f ".dev/${name}.pid" ]; then
    kill "$(cat ".dev/${name}.pid")" 2>/dev/null && echo "stopped ${name}"
    rm -f ".dev/${name}.pid"
  fi
done
# belt and braces: anything holding our ports, whoever started it
kill $(lsof -tnP -iTCP:8765 -sTCP:LISTEN) 2>/dev/null && echo "stopped :8765"
kill $(lsof -tnP -iTCP:3400 -sTCP:LISTEN) 2>/dev/null && echo "stopped :3400"
pkill -f "opkura worker" 2>/dev/null && echo "stopped stray workers"
pkill -f "cloudflared tunnel --url http://localhost:8765" 2>/dev/null && echo "stopped stray tunnels"
exit 0
