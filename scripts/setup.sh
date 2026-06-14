#!/usr/bin/env bash
#
# Opkura — one-time setup for a fresh checkout.
#
# What this does:
#   1. Verifies Python 3.11+ is available
#   2. Creates a virtualenv under ./.venv if missing
#   3. Installs opkura — pinned via requirements.txt for reproducibility,
#      then `pip install -e .` so the `opkura` console-script is wired
#      (equivalent to `npm ci && npm link` in node-speak)
#   4. Copies .env.example → .env if .env doesn't exist
#   5. Verifies the `opkura` console-script is reachable
#   6. Prints next-step guidance
#
# Idempotent — safe to re-run. Won't overwrite an existing .env.
#
# Usage:
#   ./scripts/setup.sh

set -euo pipefail

cd "$(dirname "$0")/.."

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
green() { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
red() { printf "\033[31m%s\033[0m\n" "$1" >&2; }

# --- 1. Pick a python interpreter ---------------------------------------

bold "→ Step 1/5: Locating Python 3.11+"
PY_BIN=""
for candidate in python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
        major=${version%%.*}
        minor=${version##*.}
        if [[ "$major" -eq 3 && "$minor" -ge 11 ]]; then
            PY_BIN="$candidate"
            echo "  Found: $candidate (Python $version)"
            break
        fi
    fi
done

if [[ -z "$PY_BIN" ]]; then
    red "  No Python 3.11+ found. Install via:"
    red "    macOS:  brew install python@3.12"
    red "    Linux:  sudo apt install python3.12 python3.12-venv"
    exit 1
fi

# --- 2. Create venv ------------------------------------------------------

bold "→ Step 2/5: Setting up virtualenv at .venv/"
if [[ -x ".venv/bin/python" ]] && ./.venv/bin/python -c "import sys" 2>/dev/null; then
    yellow "  .venv/ already exists and is healthy — skipping creation."
else
    if [[ -d ".venv" ]]; then
        yellow "  Existing .venv/ is broken (stale shebang or moved repo) — recreating..."
        rm -rf .venv
    fi
    "$PY_BIN" -m venv .venv
    green "  Created .venv/"
fi

VENV_PY="./.venv/bin/python"

# --- 3. Install deps -----------------------------------------------------

bold "→ Step 3/5: Installing opkura + dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip

if [[ -f "requirements.txt" ]]; then
    echo "  Installing pinned dependencies from requirements.txt (reproducible)..."
    "$VENV_PY" -m pip install --quiet -r requirements.txt
    green "  requirements.txt installed"
else
    yellow "  No requirements.txt found — falling back to unpinned install from pyproject.toml"
fi

echo "  Installing opkura in editable mode (wires the opkura CLI)..."
"$VENV_PY" -m pip install --quiet -e . --no-deps
green "  pip install -e . succeeded"

# --- 4. Bootstrap .env if missing ---------------------------------------

bold "→ Step 4/5: Bootstrapping .env"
if [[ -f ".env" ]]; then
    yellow "  .env already exists — leaving it alone."
elif [[ -f ".env.example" ]]; then
    cp .env.example .env
    green "  Copied .env.example → .env"
    yellow "  ⚠ You MUST edit .env to add your real Twilio / Deepgram / Gemini keys."
else
    yellow "  No .env.example found — skipping. Create .env manually per README.md."
fi

# --- 5. Smoke check ------------------------------------------------------

bold "→ Step 5/5: Verifying opkura entry-point"
if ./.venv/bin/opkura --help >/dev/null 2>&1; then
    green "  opkura --help works"
else
    red "  opkura entry-point failed. Run with:  .venv/bin/opkura --help  for the error."
    exit 1
fi

echo
green "✓ Setup complete."
echo
bold "Next steps:"
echo
echo "  1. Activate the venv so plain 'opkura' works in your shell:"
echo "       $ source .venv/bin/activate"
echo "     (run 'deactivate' to leave when done; or just prefix .venv/bin/ each time)"
echo
echo "  2. Make sure .env has all required keys:"
echo "       $ cat .env  # TWILIO_*, DEEPGRAM_*, AUTOCALLS_GEMINI_API_KEY, PUBLIC_URL"
echo
echo "  3. Run the test suite (offline, free):"
echo "       $ ./scripts/test.sh"
echo
echo "  4. Lint a scenario (offline, free):"
echo "       $ opkura lint \"clients/resurgent/scenarios/auth/*.yaml\""
echo
echo "  5. Place a live test call (real Twilio + LLM cost):"
echo "       $ opkura run clients/resurgent/scenarios/auth/A-001-happy-path.yaml"
echo
yellow "Tip: if requirements.txt changes (git pull brings new pinned deps), re-run"
yellow "     ./scripts/setup.sh to sync the venv. Idempotent."
