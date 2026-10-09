#!/usr/bin/env bash
#
# Run judge_it tests at any granularity.
#
# Usage:
#   ./scripts/test.sh                    # run everything (live tests skip without RUN_LIVE)
#   ./scripts/test.sh unit               # run only unit tier
#   ./scripts/test.sh integration        # only integration tier
#   ./scripts/test.sh e2e                # e2e tier (auto-sets RUN_LIVE=1 — uses Twilio credits)
#   ./scripts/test.sh engine             # only judge_it/engine tests (shortcut for unit/engine)
#   ./scripts/test.sh core               # only judge_it/core tests
#   ./scripts/test.sh llm                # only judge_it/llm tests
#   ./scripts/test.sh persistence        # only judge_it/persistence tests
#   ./scripts/test.sh bcg                # only judge_it/bcg tests
#   ./scripts/test.sh tests/unit/core/test_lint.py    # any explicit path
#   ./scripts/test.sh -v                 # verbose (pytest -v) — runs everything
#   ./scripts/test.sh engine -v          # verbose, engine only
#
# Anything after the first arg is forwarded to pytest verbatim.

set -euo pipefail

cd "$(dirname "$0")/.."

# Pick a Python interpreter. Prefer the repo's virtualenv if present —
# that's where judge_it's deps are installed via `pip install -e .`. Fall
# back to `python` then `python3` on PATH for systems where the venv isn't
# in the canonical .venv/ spot.
if [[ -x ".venv/bin/python" ]]; then
    PY=".venv/bin/python"
elif command -v python >/dev/null 2>&1; then
    PY="python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "error: no python interpreter found (looked for .venv/bin/python, python, python3)" >&2
    exit 1
fi

# Map convenience aliases to test paths.
target_for() {
    case "$1" in
        all|"")             echo "tests/" ;;
        unit)               echo "tests/unit/" ;;
        integration)        echo "tests/integration/" ;;
        e2e)                echo "tests/e2e/" ;;
        core|engine|llm|persistence|bcg)
                            echo "tests/unit/$1/" ;;
        *)                  echo "$1" ;;  # treat as an explicit path or pytest arg
    esac
}

# First positional arg is the target (or empty). Remaining args pass through.
if [[ $# -ge 1 && ! "$1" =~ ^- ]]; then
    target="$(target_for "$1")"
    shift
else
    target="tests/"
fi

# e2e tier requires the live flag; auto-set so users don't have to remember.
extra_env=()
if [[ "$target" == "tests/e2e/" || "$target" == tests/e2e/* ]]; then
    if [[ -z "${RUN_LIVE:-}" ]]; then
        echo "→ e2e tier: auto-setting RUN_LIVE=1 (will use Twilio + Gemini credits)"
        extra_env=(RUN_LIVE=1)
    fi
fi

echo "→ $PY -m pytest $target $*"
exec env "${extra_env[@]+"${extra_env[@]}"}" "$PY" -m pytest "$target" "$@"
