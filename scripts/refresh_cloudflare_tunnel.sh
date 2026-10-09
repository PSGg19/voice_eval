#!/usr/bin/env bash
# =============================================================================
# refresh_cloudflare_tunnel.sh
# -----------------------------------------------------------------------------
# Kills any running cloudflared quick tunnel, starts a fresh one against the
# local judge_it server port, waits for the new https://*.trycloudflare.com URL,
# verifies it actually reaches the server, and rewrites the active
# JUDGE_IT_PUBLIC_URL line in .env to the new URL.
#
# Why this exists: trycloudflare.com quick-tunnel hostnames are EPHEMERAL — a
# new random hostname is minted every time cloudflared (re)starts, and the
# tunnel can silently die (process alive, edge connection dead). When the URL
# in .env goes stale, Twilio can't reach judge_it's media stream and plays
# "an application error has occurred. Goodbye." into every call.
#
# Usage:
#   ./scripts/refresh_cloudflare_tunnel.sh           # uses port from .env (or 8765)
#   ./scripts/refresh_cloudflare_tunnel.sh 8765      # explicit port
#
# After running, RESTART the campaign so it picks up the new .env value.
# =============================================================================
set -euo pipefail

# --- locate repo root + .env (script lives in scripts/) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
LOG_FILE="/tmp/cloudflared-judge_it.log"

# --- resolve the local port: arg > .env JUDGE_IT_SERVER_PORT > 8765 ---
PORT="${1:-}"
if [[ -z "$PORT" && -f "$ENV_FILE" ]]; then
  PORT="$(grep -E '^[[:space:]]*JUDGE_IT_SERVER_PORT=' "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -dc '0-9' || true)"
fi
PORT="${PORT:-8765}"

echo "▶ repo:   $ROOT_DIR"
echo "▶ .env:   $ENV_FILE"
echo "▶ port:   $PORT"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "✗ cloudflared not found on PATH. Install it first (brew install cloudflared)." >&2
  exit 1
fi

# --- 1. kill any existing quick tunnel ---
if pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
  echo "▶ killing existing cloudflared tunnel(s)…"
  pkill -f "cloudflared tunnel" || true
fi

# --- 2. start a fresh quick tunnel, FULLY detached, logging to LOG_FILE ---
# Detach so the tunnel outlives this script AND the terminal: no controlling
# terminal (stdin from /dev/null), immune to SIGHUP (nohup / setsid), and
# removed from the shell job table (disown). A Ctrl+C on a later campaign — or
# closing this terminal — will NOT stop the tunnel.
: > "$LOG_FILE"
echo "▶ starting new cloudflared quick tunnel → http://localhost:$PORT"
if command -v setsid >/dev/null 2>&1; then
  setsid cloudflared tunnel --url "http://localhost:$PORT" </dev/null > "$LOG_FILE" 2>&1 &
else
  nohup cloudflared tunnel --url "http://localhost:$PORT" </dev/null > "$LOG_FILE" 2>&1 &
fi
TUNNEL_PID=$!
disown || true

# --- 3. poll the log for the new URL (up to ~40s, no foreground sleep) ---
URL=""
for _ in $(seq 1 40); do
  # -a (text mode): cloudflared's log contains control bytes, so a plain grep
  # reports "Binary file ... matches" instead of the URL — which then gets
  # written into .env. Forcing text mode returns the real URL.
  URL="$(grep -aoE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" | head -1 || true)"
  [[ -n "$URL" ]] && break
  curl -s --max-time 1 http://10.255.255.1 >/dev/null 2>&1 || true   # ~1s delay
done

if [[ -z "$URL" ]]; then
  echo "✗ Timed out waiting for the tunnel URL. Last log lines:" >&2
  tail -n 20 "$LOG_FILE" >&2
  exit 1
fi
echo "▶ new tunnel URL: $URL  (cloudflared pid $TUNNEL_PID)"

# --- 4. WAIT for the tunnel edge to actually be reachable before returning ---
# A fresh trycloudflare hostname takes a few seconds to propagate. If the
# campaign starts before then, Twilio's TwiML fetch to the new URL fails and
# every call is created but gets no audio (calls with no "TwiML served").
#
# The catch on macOS: the SYSTEM resolver negatively-caches the NXDOMAIN from
# before the tunnel existed, so a plain `curl "$URL/"` keeps failing with
# "couldn't resolve host" (exit 6) even after the edge is live. To probe the
# EDGE regardless of that local cache, we resolve the hostname out-of-band
# (dig/host, which query DNS directly) and pin the IP with curl --resolve.
# A non-000 reply (502 = up, no origin yet; 404/200 = up) means the edge is
# live and Twilio WILL reach it. 502 with no server running is success here.
HOST="${URL#https://}"; HOST="${HOST%%/*}"
echo "▶ waiting for tunnel edge to propagate (up to ~60s)…"
CODE="000"
set +e   # the probe loop is all best-effort; never let it abort the script
for _ in $(seq 1 30); do
  IP="$( (dig +short "$HOST" A 2>/dev/null || true) | grep -E '^[0-9.]+$' | head -1)"
  if [[ -z "$IP" ]]; then
    IP="$( (host "$HOST" 2>/dev/null || true) | awk '/has address/{print $NF; exit}')"
  fi
  if [[ -n "$IP" ]]; then
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
              --resolve "$HOST:443:$IP" "https://$HOST/" 2>/dev/null || true)"
    CODE="${CODE:-000}"
    [[ "$CODE" != "000" ]] && break
  fi
  curl -s --max-time 2 http://10.255.255.1 >/dev/null 2>&1 || true   # ~2s delay
done
set -e   # restore errexit for the rest of the script
if [[ "$CODE" == "000" ]]; then
  echo "⚠ tunnel edge not confirmed reachable after ~60s. Updating .env anyway,"
  echo "  but WAIT a bit before starting the campaign, or re-run this script."
else
  echo "▶ tunnel edge is LIVE: HTTP $CODE (502 = up, origin not started yet; 404 = up)"
  echo "  Safe to start the campaign — Twilio can now reach $HOST."
fi

# --- 5. rewrite the active JUDGE_IT_PUBLIC_URL line in .env ---
if [[ ! -f "$ENV_FILE" ]]; then
  echo "⚠ .env not found at $ENV_FILE — skipping auto-update. Set manually:"
  echo "    JUDGE_IT_PUBLIC_URL=$URL"
  exit 0
fi

URL="$URL" python3 - "$ENV_FILE" <<'PY'
import os, sys
new = os.environ["URL"]
path = sys.argv[1]
lines = open(path).read().splitlines()
out, replaced = [], False
for ln in lines:
    s = ln.lstrip()
    if not s.startswith("#") and s.startswith("JUDGE_IT_PUBLIC_URL=") and not replaced:
        out.append(f"JUDGE_IT_PUBLIC_URL={new}   # cloudflare quick tunnel (refreshed)")
        replaced = True
    else:
        out.append(ln)
if not replaced:                       # no active line — append one
    out.append(f"JUDGE_IT_PUBLIC_URL={new}   # cloudflare quick tunnel (refreshed)")
open(path, "w").write("\n".join(out) + "\n")
print("updated" if replaced else "appended")
PY

echo
echo "✅ Done. .env now points to:"
grep -nE '^[[:space:]]*JUDGE_IT_PUBLIC_URL=' "$ENV_FILE"
echo
echo "⚠ Restart your campaign/server so it picks up the new .env value:"
echo "    judge_it campaign clients/veros/campaigns/ib2-fixes.yaml"
