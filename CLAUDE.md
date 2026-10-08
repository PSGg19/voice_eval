# Opkura — working notes for Claude

**Working ON opkura?** Read on.
**Just want to USE the job API?** → **[`docs/job-api.md`](docs/job-api.md)** is the
doc to read (and the one to hand to other people). It assumes no repo access, no
local install and a hosted URL. Everything below is host-side detail that a
caller neither needs nor can act on.

---

## Setting up and running the stack (Claude: this is your runbook)

When a human asks you to set opkura up or run tests through it, do this —
don't reinvent it from the README:

**1. One-time setup** (idempotent — also the "after a git pull" command; it
runs DB migrations):

```bash
./scripts/setup.sh
```

It checks the toolchain, creates the venv + installs deps, starts brew
`postgresql@14` + `redis` (the docker copies in the compose file are unused —
native services shadow them), creates the `opkura` DB, runs
`alembic upgrade head`, brings up the observability containers
(`docker compose --profile logs --profile tracing`), provisions Grafana's
Loki datasource, builds `../opkura-web`, and runs the test suite. If the
suite fails, read `.dev/setup-pytest.log` — the script prints the two usual
causes (containers still warming → re-run; ambient env → it prints the
`env | grep` to check).

**Secrets are human-only — ask, never guess:**
- `.env` (git-ignored): Twilio SID/token/number, Deepgram key, Gemini key,
  `AUTOCALLS_OSV_ACCESS_TOKEN` (a ~8h SkitAdmin JWT — the human copies it
  from a logged-in llm-studio DevTools session; a 401 anywhere means it
  expired), SIP trunk vars (`AUTOCALLS_CALL_TRANSPORT=sip`,
  `AUTOCALLS_SIP_DESTINATION_HOST`, `AUTOCALLS_SIP_TRUNK_NUMBER`), and
  `AUTOCALLS_QUEUE_ENABLED=true`. A colleague's fastest start is copying a
  working `.env` from a teammate and refreshing the OSV token.
- `~/.opkura-obs.env` (setup.sh writes a template): observability env.
  Langfuse needs a one-time project created at http://localhost:3200 and its
  keys filled in (plus `NEXT_PUBLIC_LANGFUSE_PROJECT_ID` in
  `../opkura-web/.env.local`).

**2. Daily loop:**

```bash
./scripts/start.sh   # cloudflared tunnel (auto-rewrites AUTOCALLS_PUBLIC_URL
                     # in .env) → opkura serve :8765 → one queue worker → web
                     # console :3400. Logs in .dev/*.log.
./scripts/stop.sh
```

**3. Run a test call.** Two transports, both working end-to-end:

- **Voice** (a REAL phone call, ~2 min, costs money): POST to the API —

  ```bash
  curl -sX POST localhost:8765/v1/runs -H 'content-type: application/json' -d '{
    "scenario_id": "<GET /v1/scenarios>", "region": "us", "transport": "sip",
    "variable_set_id": "<an is_synthetic-attested set>",
    "acknowledge_customer_data": true }'
  ```

  It flows `queued → running → evaluating → completed` through the queue;
  the run page at http://localhost:3400/runs/<uuid> shows the transcript,
  verdict, the stereo call recording, and deep links to that call's logs
  (Grafana/Loki), trace (Phoenix + Langfuse — per-turn tree with LLM
  prompts/completions and the judge), and the bot-side view in llm-studio.

- **Text** (no phone call — the same flow over OSV's text runtime): same
  POST with `"transport": "text"`. Needs a fresh OSV token. Text also
  supports deterministic `assert:` blocks (see below).

  Clearly-synthetic `vars_overrides` in the POST work without a variable
  set; real-looking values are refused (422 `pii_value_detected`) unless an
  attested `is_synthetic` variable set covers them — that attestation is a
  human decision, never yours.

  CLI alternative (`opkura run <scenario.yaml>`) is self-contained but binds
  :8765 — stop `serve` first.

**Scaling:** `serve` holds many concurrent voice calls on one event loop
(load-tested at 100); extra `opkura worker --name w2 [--slots N --reserve M]`
processes share text/evaluate/settle. Admission budgets + kill switch:
`GET/PUT /v1/admission`, live on the console dashboard.

**Postman:** a full collection (29 requests, sample payloads) lives in the
sibling repo `../postman/collections/opkura/`.

**When something is off** — the symptom → fix table is in
[`docs/local-dev.md`](docs/local-dev.md). The classic: a run stuck ~6 min
then failing with an empty transcript = the tunnel died → `./scripts/start.sh`.

**Known-pending (as of 2026-09-03):** India voice dialing awaits LiveKit
trunk coordinates (`AUTOCALLS_LIVEKIT_SIP_*`) — India works for job-mode
seeding/generation and text, not voice dialing. The Veros IB 2.0 bot itself
is flaky on QA-10 (sometimes omits the day in the due date) — that's a bot
bug the harness correctly catches, not a harness failure.

Opkura places a **real phone call to a voice bot**, plays the caller, and judges
the result. Three ways to run it:

| Mode | Input | Who writes the scenario |
|---|---|---|
| `opkura run <scenario.yaml>` | one scenario | a human |
| `opkura campaign <campaign.yaml>` | a list of scenarios | a human |
| **job mode** (HTTP, `/v1/jobs`) | **a list of faulty production calls** | **opkura, from the real call data** |

Everything below is about **job mode**, because that is the one you drive.

---

## What job mode does

You hand it broken calls. It reads each one from OSV, writes a scenario that
reproduces the issue using that call's **real account data**, dials the bot, and
tells you what happened.

```
you: "these 3 calls are broken, the bot's number is +1844…"
      │
      ▼
POST /v1/jobs ──────────► job_id            (immediate)
      │  background: OSV → metadata + transcript + prompt
      │              LLM → one scenario per call, lint-checked
      │              → campaign.yaml
      ▼
GET /v1/jobs/{id}/wait ──► status: ready     (blocks until done)
      │
POST /v1/jobs/{id}/run ─► accepted           (immediate)
      │  background: dial each scenario, correlate back to OSV
      ▼
GET /v1/jobs/{id}/wait ──► status: completed + results[]
```

**You do not write scenarios by hand, and you do not supply test credentials.**
The account number, DOB, SSN last-4, name and registered phone come from the
faulty call's own `interaction_metadata` — that is what makes the replication
faithful and why no test data is needed.

---

## Before you start

Job mode **requires SIP transport** and refuses to run without it (HTTP 400).
Correlation keys on the `X-Opkura-Session-Id` SIP header, which a PSTN call
cannot carry.

`.env` needs:

```
AUTOCALLS_CALL_TRANSPORT=sip
AUTOCALLS_SIP_DESTINATION_HOST=581mqcm8zvu.sip.livekit.cloud:5060
AUTOCALLS_SIP_TRUNK_NUMBER=1205

AUTOCALLS_OSV_BASE_URL=https://llm-studio.skit.ai/us/api/interface
AUTOCALLS_OSV_ACCESS_TOKEN=<SkitAdmin access JWT>
AUTOCALLS_OPENAI_API_KEY=sk-...

AUTOCALLS_PUBLIC_URL=https://<tunnel>.trycloudflare.com
```

Start the server, and a tunnel so Twilio can reach the media stream:

```bash
cloudflared tunnel --url http://localhost:8765   # copy the https URL into AUTOCALLS_PUBLIC_URL
opkura serve                                        # or: python -m opkura serve
```

### Region

The region is a **path segment in the OSV base URL**. Change the URL, nothing
else:

| Region | `AUTOCALLS_OSV_BASE_URL` |
|---|---|
| US | `https://llm-studio.skit.ai/us/api/interface` ← verified |
| India | `https://llm-studio.skit.ai/in/api/interface` ← by analogy, unverified |
| Canada | `https://llm-studio.skit.ai/ca/api/interface` ← by analogy, unverified |

**Do not trust a `region=` query param in an llm-studio URL** — it is frequently
wrong. The authoritative region is whichever base URL actually returns the
interaction. If a call link 404s, try another region before assuming the id is
bad.

### The OSV token expires

It is a short-lived **access** token (~8 hours). Get a fresh one from a logged-in
llm-studio browser session: DevTools → Network → any `/api/interface/` request →
the `authorization: Bearer …` header. Ask the human for it; do not try to mint
one.

**Before assuming a `401` means the token is dead, check how it was read.** The
token is stored **quoted** in `.env`. pydantic-settings strips the quotes, so the
service is fine — but any helper script that parses `.env` by hand will send
`Bearer "eyJ..."` with the quotes attached and get `401 Invalid token` from a
perfectly good token. That cost a wrong diagnosis once. Strip `"` and `'` when
reading `.env` directly, or just let `Settings` do it.

---

## Creating a job

```bash
curl -sX POST localhost:8765/v1/jobs \
  -H 'content-type: application/json' \
  -d '{
    "calls": [
      "https://llm-studio.skit.ai/clients/1e31f185-16ac-4211-827e-46ba19d81a29/conversations/1354?interaction_uuid=c26d1903-…",
      "acb65a70-…"
    ],
    "config": {
      "target": "+18447331982",
      "languages": ["en-US", "es-US"],
      "max_concurrency": 2,
      "notes": "focus on the Spanish disclosure wording"
    },
    "callback_url": "http://localhost:8765/v1/hooks/my-sink"
  }'
```

Response is immediate:

```json
{ "job_id": "job_b22e05bdaa24", "status": "created", "scenario_count": 0 }
```

### The payload, field by field

| Field | Required | Notes |
|---|---|---|
| `calls[]` | **yes** | Full llm-studio links **or bare interaction uuids** — both work. The parser pulls `interaction_uuid` from the query string, and falls back to the first uuid it finds. A `client_uuid` earlier in the path will not confuse it. |
| `config.target` | **yes** | E.164, the number opkura dials. This is the **bot's** number — normally an **imported clone** of the live bot, since tests must not phone production. It will differ from the source call's `virtual_id`, and that is correct; correlation matches on client + caller id, never on assistant, precisely because the clone is a different assistant. |
| `config.region` | no | `us` (default), `in` or `ca`. Selects the OSV gateway per job, so one instance can serve all three. Validated at admission — a bad value is a 400, not a confusing "call not found". |
| `config.languages` | no | default `["en-US"]`. Drives the persona's language rules — set `["en-US","es-US"]` for a bilingual bot. |
| `config.max_concurrency` | no | default 2. How many calls run at once. |
| `config.notes` | no | free text passed to the generator. Use it to steer: *"the bug is in the closing, not the auth"*. |
| `job_id` | no | omit and one is minted for you. Supplying one that already exists is a **409**. |
| `callback_url` | no | where progress is POSTed. See below if you have no server. |

**Cost note:** every scenario is a real phone call. Send 3 broken calls, get 3
calls dialled. Start with one.

---

## Getting the response

You have no web server, so **use `/wait`** — one blocking request, no listener,
no poll loop:

```bash
curl -s "localhost:8765/v1/jobs/$JOB/wait?timeout=300"
```

It returns as soon as the job settles (`ready`, `completed` or `failed`). On
timeout it returns the job as-is with `"settled": false` — just call it again.

Other options:

- `GET /v1/jobs/{job_id}` — one snapshot, no waiting.
- `GET /v1/hooks/{sink_id}` — if you set `callback_url` to
  `http://localhost:8765/v1/hooks/<anything>`, opkura posts to its own sink and
  you read the whole history back, oldest first. Useful for seeing the
  `seeding → generating → ready` progression.

### Statuses

`created → seeding → generating → ready` — then, after you run it:
`running → correlating → completed`. `failed` at any point, with `error` set.

**`ready` means scenarios exist but nothing has been dialled.** Read them before
running — that is the whole reason create and run are separate.

---

## Running a job

```bash
curl -sX POST "localhost:8765/v1/jobs/$JOB/run" \
  -H 'content-type: application/json' \
  -d '{"eval": false, "callback_url": "http://localhost:8765/v1/hooks/my-sink"}'
```

### `eval: false` vs `eval: true`

| | Meaning |
|---|---|
| `"eval": false` | **opkura dials, you judge.** You get session ids, titles, descriptions, what was tested, and a call link per result. Read the transcript and decide. |
| `"eval": true` (default) | opkura's own LLM evaluator judges, and each result also carries `passed` and `overall_reasoning`. |

Prefer `eval: false` when you intend to inspect the calls yourself — it skips an
LLM judge whose accuracy is unmeasured (see `docs/platform-evolution.md` §10.2).

### The result

```json
{
  "job_id": "job_b22e05bdaa24",
  "status": "completed",
  "eval": false,
  "results": [
    {
      "scenario_id": "S01-c26d1903",
      "opkura_session_id": "403ade8595a94409ab3e7c8a8ce14791",
      "source_interaction_uuid": "c26d1903-…",
      "title": "EN closing must not flip to Spanish",
      "description": "Bot switched to Spanish on a short closing phrase.",
      "what_we_tested": "Auth by registered phone + SSN last-4, declined payment, then the short closing.",
      "call_link": "https://llm-studio.skit.ai/clients/1e31f185-…/conversations?interaction_uuid=799a39eb-…",
      "interaction_uuid": "799a39eb-…",
      "call_status": "COMPLETED",
      "disposition": "faq_due"
    }
  ]
}
```

`call_link` is the thing to hand a human. `opkura_session_id` is the exact join
key — it is the SIP header opkura injected, and it is stored on the bot's
interaction, so the mapping is not a guess.

If `interaction_uuid` and `call_link` are **null**, correlation failed: the call
probably never reached the bot. Check `call_status` first.

---

## When it goes wrong

| Symptom | Cause |
|---|---|
| `400 requires AUTOCALLS_CALL_TRANSPORT=sip` | transport is `pstn`. Job mode cannot correlate over PSTN. |
| `409 job … already exists` | you reused a `job_id`. Omit it. |
| `409 … has no generated campaign to run` | generation produced nothing lint-clean; read `scenarios[].lint_errors`. |
| error mentions `no OSV token` | `AUTOCALLS_OSV_ACCESS_TOKEN` is unset or blank. Fails immediately, by design — an empty token would otherwise render the header `Bearer ` and be reported as a network problem. |
| error mentions `OSV 401` | the token expired. Get a fresh one (above). |
| status `failed`, error mentions `no call could be seeded` | wrong region, expired token, or bad interaction uuid. The error lists the reason per call. |
| `scenarios[].lint_ok: false` | the model wrote invalid YAML. It gets one automatic repair attempt; a second failure means it is excluded from the campaign. |
| calls connect but `interaction_uuid` is null | correlation window missed, or the bot side did not record the header. |
| calls never connect | the LiveKit inbound trunk needs a **dispatch rule**, not just a trunk. Twilio reports a bare `failed`/0s with no error code. See `scripts/probe_sip_invite.py`. |

---

## Things not to get wrong

**`jobs/` contains real customer PII** — names, account numbers, DOB, SSN
last-4 — lifted from production interactions on purpose. It is gitignored.
Never commit it, never paste its contents into a ticket, never move it outside
the repo.

**Never pass `version` when fetching an assistant config.**
`/assistants/1734/` returns 200; `/assistants/1734/?version=<flow_config_id>`
returns `404 Assistant not found`. A `flow_config_id` is not a version.

**Test calls are still recorded as live production interactions**, and marking
them as tests barely helps. Opkura sends `X-Is-Test: 1` (voice) / `is_test: true`
(text). Verified in Loki against the live US runtime: the only thing that flag
suppresses is the **compliance event**. Post-call processing runs **after every
turn** — not at call end — and POSTs the caller's name, account number, SSN
last-4, address and due amount to the client's real CRM via the integration
proxy. Dispositions and call summaries are written normally.

So test traffic writes to production data from the first authenticated turn, on
both transports. Keep job sizes small, and only use accounts it is safe to
touch. (`docs/platform-evolution.md` §11.1)

**`is_test` hides the run from llm-studio, which is why it now defaults to
false.** The conversations view filters on `is_test=false`, so a run marked as a
test never appears there. Given the flag suppresses only the compliance event,
that was a bad trade — visibility is worth more. Configure it per scenario:

```yaml
transport: "text"
is_test: false      # visible in llm-studio (default)
```

Deployment fallback is `AUTOCALLS_TEXT_IS_TEST` (default `false`). Text transport
only — on a voice call the intent travels as the `X-Is-Test` SIP header, which
opkura sets itself, so lint rejects `is_test` on a `sip`/`pstn` scenario.

Either way the data is complete and API-readable:
`OSVClient.get_conversations(client_uuid, interaction_uuid)` returns every turn,
including tool calls.

**Never settle a payment.** Generated personas hang up when a flow reaches card
or bank collection. If you edit a scenario, preserve that rule. On text you can
now enforce it rather than trusting the persona:

```yaml
assert:
  must_not_call_tools: ["pay_with_card", "schedule_autopay"]
```

**Never point a test at an assistant that holds real customer accounts.** Lint
refuses it (`assistant_has_customer_data`), reading
`clients/live-assistants.yaml`. The rule is the **account count**, not the name:
`Imported - RSI_V3` carries 957,069 real accounts, because importing a bot copies
its account table. "It's an imported clone, so it's a sandbox" is how a test run
leaked into Veros's production workflow API on 2026-08-24.

Refresh the list with `python scripts/refresh_live_assistants.py --print-sql`.
To use a listed assistant deliberately, set `allow_customer_data: true` and say
in a comment why those accounts are not real customers.

## Two things text mode gives you that voice cannot

**`assert:` — deterministic checks that veto the LLM judge.**

```yaml
assert:
  must_visit_modules:     ["auth_dob", "mini_miranda"]
  must_not_visit_modules: ["card_collection"]
  must_call_tools:        ["get_accounts_by_additional_info"]
  must_not_call_tools:    ["pay_with_card"]
  tool_order:             ["get_account_scheduled_payments", "get_account_payment_options"]
  max_tag_only_turns:     0        # 0 = the bot must never go silent
```

`tool_order` is a subsequence (gaps allowed). A failure fails the run whatever
the evaluator says; unobserved data is INCONCLUSIVE and does **not** fail it.
Tool assertions need `AUTOCALLS_OSV_ACCESS_TOKEN` — the text runtime does not
return tool calls, so opkura fetches them from the conversations API after the
run. Without it they report INCONCLUSIVE, which is not a pass.

**`metrics:` — declared metrics with types, replacing the JSON blob in the
prompt.**

```yaml
metrics:
  - key: required_gates_passed
    description: required gates passed, as "n/m"
    type: string                    # integer | number | string | boolean | array
    example: "4/4"
  - key: caller_had_to_repeat
    description: did the caller repeat because the bot missed it
    type: boolean
    required: false
```

Do **not** declare `bot_turns`, `caller_turns`, `modules_visited`,
`duplicated_text_seen` or `empty_or_tag_only_turns` — opkura measures those and
overrides whatever the evaluator returns. Lint warns
(`metric_already_measured`). Where the model's number disagrees with the
measurement, the measurement wins and its claim is kept as
`<key>_reported_by_llm`.

**A tag-only bot turn is a bot defect, not a harness one.** OSV strips
`<LANG:…>`/`<HANGUP>` before filling `output_message`, so a reply of nothing but
a marker tag arrives as `""` — indistinguishable from silence over HTTP. Opkura
now recovers those from the conversations API and reports them as
`Silent bot turns: N tag-only`. The same reply is dead air on a voice call.

---

## Repo orientation

```
opkura/jobs/          job mode: api, pipeline, generator, store, workspace
opkura/services/osv/  OSV client (metadata, transcript, prompt, interactions list)
opkura/transport/     Twilio + SIP dialing, media streams, DTMF
opkura/orchestrator/  TestRunner, campaign runner
opkura/core/          config, models, scenario/campaign loaders, lint
clients/<name>/       hand-written scenarios and campaigns per client
plan/v3/              design docs (gitignored)
```

Tests: `python -m pytest tests/ -q`. Two `tests/unit/core/test_lint.py` CLI tests
fail on a stock checkout because of ANSI colour capture — pre-existing, unrelated
to anything you change.

Scenario schema is enforced by `opkura/core/lint.py` — that file is the spec.
Required keys: `name`, `target`, `vars`, `persona`, `evaluation_prompt`.
