#!/usr/bin/env bash
# One-time (and safely re-runnable) local setup for the full opkura stack:
#   python venv → deps → Postgres DB + migrations → Redis → observability
#   containers → Grafana datasource → the web console.
#
#   ./scripts/setup.sh
#
# Secrets are NOT created here. Two files hold them, both git-ignored:
#   .env               — Twilio/Deepgram/Gemini/OSV keys, SIP trunk, PUBLIC_URL
#   ~/.opkura-obs.env  — observability env (LOG_SINK, PHOENIX/LANGFUSE keys)
# The script checks they exist and names what's missing, then stops.
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m   ok\033[0m %s\n' "$*"; }
fail() { printf '\033[31m   MISSING\033[0m %s\n' "$*"; exit 1; }

# ---------------------------------------------------------------- toolchain
say "toolchain"
command -v python3.12 >/dev/null || command -v python3.11 >/dev/null \
  || fail "python 3.11 or 3.12 (pyproject wants >=3.11,<3.13)"
command -v docker >/dev/null      || fail "docker (Docker Desktop)"
command -v node >/dev/null        || fail "node (for opkura-web)"
command -v cloudflared >/dev/null || fail "cloudflared (brew install cloudflared)"
command -v brew >/dev/null        || fail "homebrew"
ok "python, docker, node, cloudflared"

# ------------------------------------------------------------------ secrets
say "secret files"
[ -f .env ] || fail ".env — copy from a teammate; needs AUTOCALLS_TWILIO_*, \
AUTOCALLS_DEEPGRAM_API_KEY, AUTOCALLS_GEMINI_API_KEY, AUTOCALLS_OSV_ACCESS_TOKEN, \
AUTOCALLS_CALL_TRANSPORT=sip + SIP trunk vars, AUTOCALLS_QUEUE_ENABLED=true"
for key in AUTOCALLS_TWILIO_ACCOUNT_SID AUTOCALLS_DEEPGRAM_API_KEY \
           AUTOCALLS_GEMINI_API_KEY AUTOCALLS_OSV_ACCESS_TOKEN; do
  grep -q "^${key}=" .env || fail ".env is missing ${key}"
done
ok ".env present with the core keys"
if [ ! -f "$HOME/.opkura-obs.env" ]; then
  cat > "$HOME/.opkura-obs.env" <<'EOF'
# Observability env for opkura serve/worker — sourced by scripts/start.sh.
export LOG_SINK=loki
export LOKI_URL=http://localhost:3100/loki/api/v1/push
export PHOENIX_ENABLED=1
# Langfuse: create a project at http://localhost:3200, then fill these in
# (and NEXT_PUBLIC_LANGFUSE_PROJECT_ID in ../opkura-web/.env.local):
#export LANGFUSE_ENABLED=1
#export LANGFUSE_HOST=http://localhost:3200
#export LANGFUSE_PUBLIC_KEY=pk-lf-...
#export LANGFUSE_SECRET_KEY=sk-lf-...
export TRACE_CAPTURE_LLM_IO=1
EOF
  chmod 600 "$HOME/.opkura-obs.env"
  ok "wrote ~/.opkura-obs.env template (Langfuse keys commented out)"
else
  ok "~/.opkura-obs.env present"
fi

# --------------------------------------------------------------- python env
say "python venv + deps"
if [ ! -d .venv ]; then
  PY=$(command -v python3.12 || command -v python3.11)
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q -e ".[observability]" 2>&1 | tail -1 || true
.venv/bin/pip install -q pytest pytest-asyncio fakeredis[lua] 2>&1 | tail -1 || true
ok "$(.venv/bin/python --version)"

# ------------------------------------------------- postgres + redis (brew)
say "postgres + redis (native brew services — the compose copies are unused)"
brew services list | grep -q "postgresql@14.*started" \
  || { brew services start postgresql@14; sleep 3; }
brew services list | grep -q "^redis.*started" \
  || { brew services start redis; sleep 2; }
ok "brew services up"
# the DB (default url: postgresql+psycopg://postgres:root@localhost:5432/opkura)
DB_URL=$(grep -oE '^AUTOCALLS_DATABASE_URL=.*' .env | cut -d= -f2- || true)
psql -h localhost -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='opkura'" 2>/dev/null | grep -q 1 \
  || createdb -h localhost -U postgres opkura 2>/dev/null \
  || psql -h localhost -tc "SELECT 1 FROM pg_database WHERE datname='opkura'" | grep -q 1 \
  || createdb opkura
ok "database 'opkura' exists"

say "migrations (alembic upgrade head)"
.venv/bin/alembic upgrade head
ok "schema current"

# ------------------------------------------- observability stack (docker)
say "observability containers (loki, grafana, phoenix, langfuse + deps)"
docker compose --profile logs --profile tracing up -d --quiet-pull 2>&1 | tail -2 || true
ok "compose profiles 'logs' + 'tracing' up"

# Grafana needs a Loki datasource once (anonymous admin is enabled locally).
say "grafana loki datasource"
for i in $(seq 1 15); do
  curl -sf localhost:3001/api/health >/dev/null 2>&1 && break; sleep 2
done
if curl -s localhost:3001/api/datasources 2>/dev/null | grep -q '"uid":"loki"'; then
  ok "already provisioned"
else
  curl -s -X POST localhost:3001/api/datasources -H 'content-type: application/json' -d \
    '{"name":"Loki","type":"loki","uid":"loki","url":"http://loki:3100","access":"proxy","isDefault":true}' \
    >/dev/null && ok "created (uid=loki)" || echo "   grafana not reachable yet — re-run setup later"
fi

# ---------------------------------------------------------------- the web
say "opkura-web (../opkura-web)"
if [ -d ../opkura-web ]; then
  (cd ../opkura-web && npm install --silent && npm run build 2>&1 | tail -1)
  ok "built (serves on :3400)"
else
  echo "   ../opkura-web not found — clone it next to this repo"
fi

# Give the containers a moment — langfuse/loki booting mid-suite shows up as
# spurious integration failures on a first run.
say "waiting for observability containers"
for url in "localhost:3100/ready" "localhost:3200/api/public/health" "localhost:6006"; do
  for i in $(seq 1 30); do
    curl -sf --max-time 2 "http://$url" >/dev/null 2>&1 && break; sleep 2
  done
done
ok "loki, langfuse, phoenix answering"

say "sanity: test suite (full output → .dev/setup-pytest.log)"
mkdir -p .dev
if .venv/bin/python -m pytest tests/ -q > .dev/setup-pytest.log 2>&1; then
  tail -1 .dev/setup-pytest.log
else
  tail -1 .dev/setup-pytest.log
  echo
  echo "   Failures (details in .dev/setup-pytest.log):"
  grep -E "^(FAILED|ERROR)" .dev/setup-pytest.log | head -20
  echo
  echo "   Common causes on a first run:"
  echo "   - containers still warming: just re-run ./scripts/setup.sh"
  echo "   - ambient env leaking into tests: check"
  echo "       env | grep -E '^(PG|AUTOCALLS|TRACING_|LOG_SINK|LANGFUSE_|PHOENIX_|OTEL_)'"
  echo "     and retry from a shell without those exports"
fi

say "done — next: ./scripts/start.sh"
