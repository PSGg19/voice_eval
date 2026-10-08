# `scripts/`

Convenience runners for the common dev / ops tasks.

## `setup.sh` — first-time install

Run once after cloning the repo. Idempotent — safe to re-run when you
pull new changes.

```bash
./scripts/setup.sh
```

What it does:

1. Verifies Python 3.11+ is installed (errors out cleanly if not).
2. Creates `.venv/` if missing (rebuilds it if the existing one is broken
   — stale shebangs from a moved repo, etc).
3. Installs **pinned** dependencies from `requirements.txt` for
   byte-identical environments across the team (equivalent to `npm ci`).
4. Installs `vjudge` in editable mode so the `vjudge` console-script
   wires up (equivalent to `npm link`).
5. Copies `.env.example` → `.env` if `.env` doesn't exist (does NOT
   overwrite an existing one).
6. Verifies `vjudge --help` works.
7. Prints next-step guidance.

After running setup, you have two ways to invoke `vjudge`:

```bash
# Option A: activate the venv (then plain `vjudge` works for the session)
source .venv/bin/activate
vjudge --help
# (`deactivate` to leave)

# Option B: use the venv binary directly (no activation needed)
.venv/bin/vjudge --help
```

## `test.sh` — run tests at any granularity

```bash
./scripts/test.sh                    # everything (live tests skip without RUN_LIVE)
./scripts/test.sh unit               # only tests/unit/
./scripts/test.sh integration        # only tests/integration/
./scripts/test.sh e2e                # tests/e2e/ — auto-sets RUN_LIVE=1 (real Twilio)
./scripts/test.sh engine             # shortcut for tests/unit/engine/
./scripts/test.sh core               # tests/unit/core/
./scripts/test.sh llm                # tests/unit/llm/
./scripts/test.sh persistence        # tests/unit/persistence/
./scripts/test.sh bcg                # tests/unit/bcg/
./scripts/test.sh tests/unit/core/test_lint.py    # any explicit path

./scripts/test.sh engine -v          # pytest -v on engine tests
./scripts/test.sh -q                 # all tests, quiet
./scripts/test.sh unit -k barge      # all unit tests whose name contains "barge"
```

**Note on `e2e`:** the e2e tier places real Twilio calls and burns LLM
tokens. The runner auto-sets `RUN_LIVE=1` when you ask for the e2e tier
explicitly. Plain `./scripts/test.sh` (run-everything) does NOT set it —
live tests still skip safely.

## Regenerating `requirements.txt`

`requirements.txt` is the lockfile-equivalent (like `package-lock.json`)
— it pins every dependency to an exact version for reproducibility.

**When to regenerate:**
- After you change `pyproject.toml`'s `dependencies` list
- After `pip install <new-package>` (and you want to add it permanently)
- After a `pip install --upgrade` you want to lock in for the team

**How:**

```bash
# 1. Make sure your venv has the deps you want pinned
.venv/bin/python -m pip install -e .          # (or upgrade specific packages)

# 2. Regenerate requirements.txt
.venv/bin/python -m pip freeze \
    | grep -v "^-e " \
    | grep -v "^vjudge" \
    > requirements.txt

# 3. Commit the change
git add requirements.txt
git commit -m "deps: bump <package> to <version>"
```

The `grep -v` filters drop vjudge itself (installed editable, not a
distributable package) so requirements.txt stays clean.
