# Running the full stack locally

Two scripts do everything. Read this once; after that it's two commands.

## The pieces (what runs where)

| Piece | Where | Started by |
|---|---|---|
| Postgres (`judge_it` DB — runs, scenarios, targets) | native brew `postgresql@14`, :5432 | brew service (always on) |
| Redis (queue streams, job docs, worker registry) | native brew `redis`, :6379 | brew service (always on) |
| Loki + Grafana (logs) | docker compose, :3100 / :3001 | `setup.sh` (stays up) |
| Phoenix + Langfuse (+clickhouse/minio) (traces, LLM calls) | docker compose, :6006 / :3200 | `setup.sh` (stays up) |
| cloudflared quick tunnel (Twilio → your laptop) | new URL every start | `start.sh` |
| `judge_it serve` (API + embedded voice worker) | :8765 | `start.sh` |
| `judge_it worker` (queue: converse-text/evaluate/settle) | — | `start.sh` |
| judge_it-web console | :3400 | `start.sh` |

The docker compose file also contains postgres/redis, but the native brew
services shadow them — the containers are unused.

## One-time setup

```bash
./scripts/setup.sh
```

Idempotent — re-run it any time (after a pull, to run migrations, etc.). It:
checks the toolchain, checks `.env` has the core keys, writes a
`~/.judge_it-obs.env` template if missing, creates the venv + installs deps,
starts brew postgres/redis, creates the `judge_it` DB, runs
`alembic upgrade head`, brings up the observability containers
(`--profile logs --profile tracing`), provisions Grafana's Loki datasource,
builds judge_it-web, and runs the test suite.

Secrets you must supply yourself (never committed):
- `.env` — Twilio, Deepgram, Gemini, OSV token (expires ~8h — grab a fresh
  one from a logged-in llm-studio DevTools session), SIP trunk vars,
  `JUDGE_IT_QUEUE_ENABLED=true`.
- `~/.judge_it-obs.env` — observability env; for Langfuse create a project at
  http://localhost:3200 once, then uncomment and fill the LANGFUSE_* lines,
  and put `NEXT_PUBLIC_LANGFUSE_PROJECT_ID=<id>` in `../judge_it-web/.env.local`.

## Daily loop

```bash
./scripts/start.sh    # tunnel (rewrites JUDGE_IT_PUBLIC_URL) → serve → worker → web
./scripts/stop.sh     # stops those four; brew + docker stay up
```

Logs land in `.dev/*.log`. The console is http://localhost:3400.

## Making a call

From the console: **Runs** shows everything; dispatch via the API:

```bash
curl -sX POST localhost:8765/v1/runs -H 'content-type: application/json' -d '{
  "scenario_id": "<from /v1/scenarios or the Scenarios page>",
  "region": "us", "transport": "sip",
  "variable_set_id": "<attested synthetic vars>",
  "acknowledge_customer_data": true
}'
```

or CLI (needs :8765 free — stop serve first, it binds the same port):

```bash
source ~/.judge_it-obs.env
.venv/bin/python -m judge_it run clients/veros/scenarios/ib2-qa-replication/QA-10-date-format-getdate-hangup.yaml
```

Every run page carries: the transcript, verdict, the stereo call recording,
and one-click deep links to that call's logs (Grafana), trace (Phoenix,
Langfuse — turn tree with LLM prompts/completions and the judge), and the
bot-side view in llm-studio.

## When something is off

| Symptom | Cause / fix |
|---|---|
| run stuck 6 min → empty transcript | tunnel died → `./scripts/start.sh` (new URL, serve restarted) |
| voice runs stuck `queued` forever | serve not running, or its embedded worker failed — check `.dev/serve.log` |
| Grafana button shows nothing | run predates `LOG_SINK=loki`, or serve started without `~/.judge_it-obs.env` |
| Langfuse/Phoenix empty | same — the obs env is sourced by start.sh; don't start serve by hand without it |
| `OSV 401` anywhere | the OSV token expired — fresh one into `.env`, restart serve |
| llm-studio button 404/empty | you're logged out of llm-studio, or its region switcher ≠ the run's region |
