# JudgeIt Platform Evolution — OSV Parity, Service Mode, Observability

Status: **proposal / for review**. Nothing here is implemented.

Scope: turn judge_it from a single-operator CLI tool into a service that other
systems call, that runs for weeks without babysitting, and that can be
debugged from the same Grafana/Loki/Tempo panes as `orchestrator-service`.

Nine decisions, each with the OSV precedent, a recommendation, and what it
costs. Then hazards, then a phased plan.

---

## In one page

Read this. The rest of the document is reference — go to a section when you need
the detail behind a line here.

### What judge_it becomes

A service that runs one job well: **hold a conversation with a bot and judge it.**
Two transports — **text** (OSV's text runtime over HTTP: cheap, fast, hundreds
concurrent, the default) and **twilio** (real PSTN: for audio, barge, DTMF, and
latency only). Everything else moves out.

```
llm-studio UI ──► OSV interface ──POST /v1/runs──► judge_it ──► call ──► verdict
   (dropdowns)      (owns catalog,                (pipeline
                     submits runs)                 only)
                                                      │
                          logs → Loki    metrics → Prometheus
                          traces + LLM payloads → Phoenix
                          results → Postgres    audio → S3
```

### The ten decisions

1. **JudgeIt is a pipeline.** Not a catalog, not a CRUD app, not an admin UI.
2. **No YAML.** A run is a JSON payload, validated by Pydantic.
3. **OSV owns the provider catalog and the UI**, and OSV calls judge_it.
4. **Payload sends `provider_key`;** judge_it looks up `base_url` in a ~20-row table.
5. **LLM speaks the OpenAI schema.** New vendor = one row + one secret.
6. **All keys in AWS Secrets Manager**, `judge_it/{vendor}-{type}`, one shared set.
7. **Logs go to stdout → Loki**, tagged `app="judge_it"`. No log files anywhere.
8. **Every LLM call goes to Phoenix**, in its own project, tagged with the
   interaction uuid.
9. **Postgres.** SQLite is dropped, not migrated.
10. **If OSV is down, judge_it doesn't run.** No fallback machinery.

### What we build, in four steps

Each step is useful on its own and doesn't need the next one.

| Step | Build | Done when |
|---|---|---|
| **1. See what's happening** | logfmt to stdout → Loki · Prometheus metrics · Phoenix spans · redaction before any of it ships | you can paste an `interaction_uuid` into Grafana and see judge_it's and OSV's view of the same call |
| **2. Stop losing state** | Postgres + Alembic · payload schema replaces YAML · `_sessions` → Redis · S3 for audio | a restart doesn't lose in-flight runs, and a 6-week-old run is still readable |
| **3. Be callable** | `/v1/runs` + scenario registry · `providers` table + Secrets Manager · idempotency · admission control | OSV's UI can submit a run and show the result |
| **4. Survive unattended** | run state machine · reaper · graceful drain · per-vendor backpressure · cost caps | it runs for a month without anyone watching it |

Then, once those hold: the CI gate (`POST /v1/gates/{suite}`) that blocks a bad
bot deploy. That's the payoff — everything above just makes judge_it trustworthy
enough to gate on.

**Deliberately deferred:** decomposing `transport/stream.py` into processors
(large, risky, low payoff once step 1 gives you the observability); out-of-tree
vendor plugins (nobody has asked for one); flake detection and version-diff
(after the CI gate exists).

### Three things that need someone else

Everything above is judge_it-side work. These aren't:

1. **Are judge_it's vendor keys production's keys under a different path?** If yes, a
   load test eats production's concurrency and can degrade real calls. Check Twilio
   first. *Diff the key values.* (§8)
2. **`is_test` for *voice* calls.** Every judge_it PSTN call is recorded as a live
   production interaction — post-call processing, compliance, analytics, webhooks
   all fire. Needs an OSV change or a caller-ID whitelist. Text mode already takes
   `is_test` as a request field. (§11.1)
2b. **In-call side effects, both transports.** The bot's tools run for real —
   payments, SMS, CRM writes through PIS. `is_test` does not gate these. Needs test
   accounts or a PIS sandbox before any suite runs at volume. (§11.4)
3. ~~**`interaction_uuid` for *voice* runs.**~~ **SOLVED 2026-08-11 — no longer
   needs anyone else.** judge_it now dials the LiveKit SIP host directly
   (`JUDGE_IT_CALL_TRANSPORT=sip`, `transport/sip.py`) and its `X-` headers
   reach the bot, so a voice run is joinable without any OSV change. The trick
   was inverting the direction: instead of making OSV adopt an
   `interaction_uuid` we inject, we put **our** id on **their** row —
   `x-judge_it-session-id` lands in `interaction_metadata` and is queryable. See
   §5a for the evidence. Text mode still returns the uuid on turn 1. (§5a, §5d)

---

## What changes in the codebase

**Deleted:** the YAML corpus and its loaders · `detect_schema` · most of `lint.py`
· `debug.log` and its `FileHandler` · SQLite · `llm/gemini.py` +
`detect_provider` + `_api_key_for` · vendor keys in `.env` · `_sessions` as a
process dict · `resolved_tts_transport`.

**Never built:** provider CRUD, admin screens, catalog cache, sync jobs — OSV
owns all of it.

**Added:** run API + scenario registry · `providers` table + Secrets Manager ·
frames and observers · Loki/Prometheus/Phoenix · state machine + reaper +
admission control · Postgres schema with prompt dedupe · Indic vendors ·
redaction.

## Why it scales

**Throughput.** Each current ceiling has one fix: SQLite's single write lock →
Postgres. `_sessions` being process-local → Redis. A file handle per run → none.
Opaque vendor timeouts → admission control returning 429. One key = one
concurrency pool → multi-resource secrets. Load tests sharing production quota →
isolated accounts. Leaked Twilio calls → the reaper. An 80 KB persona stored per
turn → content-addressed prompts.

**Change-rate.** New vendor = one row + one secret. New scenario = a payload. New
vendor tunable = OSV renders it from data. New caller = a token. None of it is a
release — which is what stops test coverage from quietly plateauing.

---


## 0. Where judge_it is today

Honest read of the current shape, because the proposals only make sense
against it.

**What's good.** The V1→V2 dispatch is clean. `core/` genuinely has no I/O
deps. The per-call artifact set (`persistence/logwriter.py`) is more complete
than OSV's — transcript, audio, LLM calls, rendered prompts, metadata, all
written from a `finally` that survives cancellation. The *contents* are the
good part and they all survive the move to Postgres/Loki/S3 (§6, §9); only the
folder-of-files delivery mechanism goes away. The comments in
`orchestrator/runner.py` record *why* decisions were made, including rejected
alternatives. Keep that habit.

**What blocks the next step.**

| Problem | Evidence |
|---|---|
| `MediaStreamSession` is a god object | `transport/stream.py` — 1572 lines: WS loop, STT callbacks, VAD, turn detection, barge scheduling, 4 TTS transports (`_speak_blocking/_speak_streaming/_speak_ws/_speak_ws_inner`), 2 DTMF transports, audio recording, transcript building, mulaw codec |
| Two event loops in two threads | `runner.py:312-316` — FastAPI runs in a thread, so completion needs `call_soon_threadsafe`, and `call_id_var` must be set in *two* places (`core/log_context.py:8-20`) |
| Session registry is process-local | `transport/server.py:22` — `_sessions: dict`. Restart loses every in-flight call; can't scale to >1 pod |
| No metrics at all | no `prometheus_client` anywhere |
| Per-call `FileHandler` on a shared logger | `logwriter.py` attaches to the `judge_it` logger and filters by contextvar. Comments show this has already caused cross-call log pollution twice |
| SQLite + one global write lock | `persistence/store.py` — `judge_it.db` is already 12 MB and is the only store |
| Config has ad-hoc legacy aliasing | `core/config.py:47-57` `resolved_tts_transport` — a property that reconciles two env vars. One is fine; the third one will hurt |
| No notion of the bot's identity | zero hits for `interaction`, `assistant_uuid`, `client_uuid` across the whole package |
| CLI-only entry | `cli/main.py` starts the FastAPI server in a thread purely to serve Twilio webhooks |
| Everything is coupled to YAML files on local disk | `core/scenario_loader.py` + `core/campaign_loader.py` + 554 lines of `core/lint.py`; `runner.py:49-66` derives log paths from `scenario.source_path`; `runner.py:69-90` sniffs schema version from file shape |

That last one is the crux of your ask: judge_it currently has no idea *what* it
called, only *which number*. Everything about correlation follows from fixing
that.

---

## 1. Decision: adopt Pipecat's *philosophy*, not the *dependency*

You said "we can pipecat so we can have same philosophy." Agreed on
philosophy. I'd argue against the dependency.

### What the philosophy actually is

Four ideas, and they're separable:

1. **Typed frames, one direction.** Nothing calls back into session state.
   `_on_stt_final()` mutating `self` becomes "emit `TranscriptionFrame`".
2. **Small processors, composed.** Each does one thing with a
   `process_frame(frame, direction)` signature. The pipeline is a *list*,
   which means it's config.
3. **Observers, off the data path.** Metrics, debug tracing, and timing
   subscribe to frames without being able to break the call.
   `app/runtime/voice/observers/metrics_observer.py:46` is the model.
4. **Services behind a registry.** `ServiceFactory._class_registry[type][name]`
   (`app/runtime/voice/services/factory.py:52`) — 20+ vendors, uniform
   construction from a config dict.

Idea 3 is the highest-leverage thing on this list for judge_it. It's what turns
"we need better logging and metrics" from a refactor into an additive change.

### Why not take the dependency

- **Independence of the test harness.** judge_it is the adversary. If it shares
  a framework with the system under test, framework bugs cancel out and the
  test goes green for the wrong reason. A latency regression inside Pipecat's
  frame scheduling would be invisible to a Pipecat-based prober.
- **judge_it needs to misbehave on purpose.** Barge mid-utterance, DTMF during
  bot speech, silence injection, deliberately-late turn ends. Pipecat's
  interruption strategies are built to make a bot behave *well*. You'd spend
  your time fighting `min_words_interruption_strategy` rather than using it.
  (You can see OSV already needed `processors/_vendor/interruptions/` to patch
  around this from the *bot* side.)
- **Weight.** Pipecat pulls a large tree; judge_it's value is that it's a small
  thing you can run 50 of.

### Recommendation

Build a thin frame bus of your own — `judge_it/pipeline/` — and split
`stream.py` into processors. Target shape:

```
judge_it/pipeline/
├── frames.py         # AudioFrame, TranscriptionFrame, InterimFrame,
│                     # BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
│                     # TurnEndFrame, BargeFrame, DtmfFrame,
│                     # CallLifecycleFrame, MetricsFrame, EndFrame
├── bus.py            # ~150 lines: ordered processor list + observer fanout
├── processor.py      # Processor protocol
└── observers.py      # Observer protocol

judge_it/pipeline/processors/
├── stt_source.py         # WS audio in  → InterimFrame/TranscriptionFrame
├── turn_detector.py      # → TurnEndFrame   (silero | deepgram-utterance-end)
├── caller_llm.py         # TurnEndFrame → caller utterance
├── barge_scheduler.py    # scheduled | llm  (only added when barge enabled)
├── dtmf_sender.py        # rest | audio
├── tts_sink.py           # utterance → AudioFrame  (ws | http | blocking)
├── recorder.py           # AudioFrame → wav        (omit for load tests)
└── transcript_writer.py  # → CallResult.transcript

judge_it/pipeline/observers/
├── metrics_observer.py   # frames → prometheus
├── trace_observer.py     # frames → OTel spans
├── event_observer.py     # frames → structured stdout log events → Loki (§6)
└── result_observer.py    # frames → transcript/turn rows → Postgres (§9)
```

The payoff is that **the processor list becomes the run config** (§3), and
**"better logging/metrics/tracing" becomes three observers** rather than edits
scattered through 1572 lines (§6).

**Cost:** this is the largest item in the proposal. `stream.py` holds a lot of
hard-won behaviour — the smart-grace turn detection, the DTMF reconnect
dance, the mulaw handling. Do it incrementally: extract the frame types and
the observer fanout *first*, emit frames from the existing code alongside the
current callbacks, build the metrics observer on those frames, and only then
start moving logic out of `MediaStreamSession`. Don't start with a rewrite.

**Take the dependency for one thing only:** if you want vendor breadth (20+
STT/TTS providers) cheaply, wrap Pipecat's *service classes* behind your own
`STTService`/`TTSService` protocol. Vendor adapters are commodity and
independently maintained — that's where the dependency pays. Keep the
pipeline yours.

---

## 2. Decision: the payload is the interface — YAML goes away

Scenarios stop being files. A run is defined by a JSON payload, submitted over
the API, validated by Pydantic, and stored in Postgres. There is no
`.yaml` anywhere in the execution path.

### What this deletes

| Goes away | Today |
|---|---|
| `clients/*/scenarios/*.yaml`, `flows/*.yaml`, `clients/_examples/*.yaml` | the scenario corpus |
| YAML loading in `core/scenario_loader.py` (351 lines) | `yaml.safe_load` → `Scenario` |
| `core/campaign_loader.py` (156 lines) | campaign YAML |
| Most of `core/lint.py` (554 lines) | hand-rolled schema validation |
| `detect_schema()` V1/V2 sniffing (`runner.py:69-90`) | "does the file have `steps:` or `persona:`" |
| `_scenario_log_root()` (`runner.py:49-66`) | log paths derived from `scenario.source_path` |

`detect_schema` is worth calling out: it exists purely because two YAML shapes
share one file extension. With a typed payload carrying an explicit
`schema_version`, the whole guessing game disappears — and so does the
"hybrid YAMLs are rejected at load time" special case.

### Pydantic is the schema now

Define the payload once as Pydantic models in `core/schema.py`. You then get,
for free, things `lint.py` currently hand-rolls:

- **Validation** with field-level error paths, returned as HTTP 422.
- **Published JSON Schema / OpenAPI** at `/openapi.json` — callers generate
  typed clients instead of reading a README about YAML keys.
- **Editor support** — the same JSON Schema gives autocomplete and inline
  validation in any editor, which is most of what YAML-with-a-linter bought.

What Pydantic *can't* express stays as an explicit semantic-check pass, and
this is the part worth keeping from `lint.py`:

- vendor capability vs requested language (§8) — "sarvam doesn't do es-MX"
- `barge_in.triggers` referencing turns beyond `max_turns`
- Jinja2 placeholders in `persona` with no matching key in `vars`
- `llm.provider` disagreeing with the model prefix (already in
  `runner.py:504-534` — move it to validation time, where it belongs, instead
  of failing after the log folder is created)

Expose that as `POST /v1/scenarios/lint` so callers validate before spending a
phone call.

### The payload

```json
{
  "schema_version": 2,
  "name": "resurgent-auth-happy-path",
  "suite": "regression",
  "description": "DOB→SSN→ZIP verification, no barge. Replaces A-001.yaml.",

  "target": "+1...",
  "client_uuid": "...",
  "assistant_uuid": "...",
  "assistant_version": 12,

  "language": "en-US",
  "secondary_languages": [],

  "vendors": {
    "stt": { "provider_key": "deepgram-asr-en-US", "model": "nova-2",
             "config": { "utterance_end_ms": 1000 } },
    "tts": { "provider_key": "deepgram-tts-en-US", "voice": "aura-asteria-en",
             "config": { "transport": "ws" } },
    "llm": {
      "caller":    { "provider_key": "google-llm", "model": "gemini-2.5-flash",
                     "temperature": 0.3, "stream": true },
      "evaluator": { "provider_key": "openai-llm", "model": "gpt-4o",
                     "temperature": 0.0,
                     "response_format": { "type": "json_schema" } }
    }
  },

  "transport":     { "name": "twilio", "dtmf": "rest" },
  "turn_detector": { "name": "silero", "silence_threshold_s": 0.8 },

  "vars": { "account_last4": "4821", "dob": "12-04", "zip": "80202" },
  "persona": "You are calling about... {{ vars.account_last4 }}",
  "evaluation_prompt": "Judge whether the bot...",

  "max_turns": 30,
  "barge_in": { "enabled": true, "mode": "scheduled", "triggers": [] },
  "features": { "recording": true }
}
```

Note what the vendor block does *not* contain: no `base_url`, no credential, no
capabilities. Those come from judge_it's `providers` table and Secrets Manager,
resolved at submit time (§8). `transport` and `turn_detector` sit outside
`vendors` because they aren't providers in OSV's catalog — they're payload enums
validated by judge_it's own schema.

Every knob the scenario YAML had, plus the vendor block from §8 — which is the
whole point of your ask: **STT, TTS, LLM, transport, and turn detection are all
just payload fields, and a new vendor is a new `name` value, not a code
change.**

### Two ways to supply it

**Inline** — the whole payload in `POST /v1/runs`. Right for CI-generated
scenarios, one-off probes, and callers who own their own storage.

**By reference** — a registry in Postgres (`scenarios` table, §9):

```
POST /v1/scenarios                          → {scenario_id, name, version: 3}
GET  /v1/scenarios?suite=regression
GET  /v1/scenarios/{name}/versions
GET  /v1/scenarios/{name}/versions/2/diff/3
POST /v1/runs  { "scenario_ref": "resurgent-auth-happy-path@3", "vars": {...} }
```

Registry entries are **immutable per version** — a `POST` to an existing name
creates version N+1, never mutates N. `@3` pins; omitting the version means
latest (fine for exploration, never for a CI gate).

Support both. The registry stores exactly the same payload shape, so there's
one schema, one validator, one set of docs.

### The reproducibility invariant

`runs.scenario_payload` (JSONB, §9) always stores the **fully-resolved payload
actually executed** — inline body, or the registry version with request-level
`vars`/`overrides` merged in. Never a reference.

This is the invariant that makes the whole thing trustworthy: a run stays
interpretable after the registry entry is superseded, after the caller's
template changes, after someone edits the profile. Store
`scenario_hash = sha256(canonical_json(payload))` alongside it — canonical
meaning sorted keys and no insignificant whitespace, so the hash is stable
across serialisers. That hash does triple duty: dedupe, "did this scenario
change between Tuesday and Friday", and a cheap log/span field (§6).

### New risk: Jinja2 over an API is template injection

`persona` and `evaluation_prompt` are Jinja2-rendered against `vars`
(`core/scenario_loader.py`). Today the templates come from files on your own
disk, so there's no threat model. **The moment the template arrives in an HTTP
payload from another service, it's remote template execution** — the standard
SSTI path (`{{ ''.__class__.__mro__ }}` → module access → RCE).

Non-negotiable once this ships:

- `jinja2.sandbox.SandboxedEnvironment`, not `Environment`.
- Restrict the render context to `vars` only; no builtins, no globals, no
  filters that can reach the filesystem.
- Cap template length and render time; reject on timeout.
- `undefined=StrictUndefined` so a typo'd placeholder is a 422 rather than a
  silently empty persona.

Cheaper alternative worth considering: drop Jinja2 for `str.format`-style
`{account_last4}` substitution. You lose loops and conditionals in personas —
check whether any real scenario uses them. If not, this removes an entire
vulnerability class for free.

### What YAML was actually good at — replace it deliberately

Three real losses. Don't discover them after the migration:

1. **Git-reviewable diffs.** A scenario change was a PR. Mitigate with
   immutable versions + `GET /versions/2/diff/3`, and — if the team wants PR
   review — an `judge_it push ./scenarios/` command that syncs a directory of
   **JSON** files into the registry. Git stays the human source of truth; the
   API stays the execution source of truth. Cheap to build, keeps the
   workflow.
2. **Comments.** JSON has none, and the YAML corpus uses them heavily
   (`clients/_examples/` exists to explain the format). Add first-class
   `description` and a `notes` map for per-field rationale. Otherwise the
   institutional knowledge in those comments evaporates.
3. **Bulk edits.** "Change `max_turns` across 40 scenarios" was `sed`. Now it's
   a script against the API — provide `PATCH /v1/scenarios/bulk` with a filter,
   or accept that it's a for-loop.

### Migration and the CLI

- **One-shot converter**: `judge_it migrate-scenarios clients/ --push`. YAML →
  payload → registry, one version per file, `description` seeded from the
  YAML's leading comments. Don't hand-port; the corpus is large and the
  converter is ~100 lines.
- **The CLI becomes an API client**, it doesn't die. `judge_it run
  scenario.json` POSTs and streams status; `judge_it run --local` runs in-process
  for development. Interactive iteration stays as fast as it is today.
- **V1 flows (`flows/*.yaml`, `legacy/flow.py`)**: convert or drop. Given
  `legacy/` is documented as frozen, dropping is probably right — but confirm
  nobody's running the load tester off those files first.

### Compatibility, because callers are now external

The payload is a published contract with other teams' code on the other end.

- `schema_version` in the payload; **additive-only changes within v1**, new
  fields optional with defaults.
- Breaking changes get `/v2`, with `/v1` maintained until callers migrate.
- Accepting an unknown field should be a **422, not a silent ignore** — a typo
  in `max_turms` must not quietly run 30 turns. (`extra="forbid"`.)
- Version the *stored* payloads too: a `schema_version` on the `scenarios` row
  so an old registry entry can be upgraded on read rather than breaking.

---

## 3. Decision: layered config, frozen onto every run

Today: `pydantic-settings` with `JUDGE_IT_` prefix, plus per-scenario `llm:`,
`stt:`, `tts:`, `barge_in:` blocks. In API mode you also need per-request
overrides, and you need to be able to answer "why did *this* run behave
differently."

### Precedence, explicit and documented

```
code defaults
  < env / Settings              (deployment-level: endpoints, caps, DB/Redis URLs)
    < profile                   (named preset, see below)
      < campaign defaults       (campaign payload)
        < scenario payload      (§2 — inline or registry version)
          < per-request overrides
```

Note the split this implies: **env/Settings holds only deployment concerns**
(Loki/Phoenix endpoints, concurrency caps, DB and Redis URLs). Credentials are
not in this list at all — they come from AWS Secrets Manager, resolved by
`(client_uuid, vendor, service_type)` and never present in env or payload (§8).
Everything that describes *a test* lives in the payload. Today `Settings`
mixes both — `caller_model`, `evaluator_model`, `temperature`, and
`skip_evaluation` (`core/config.py:80-88`) are test semantics wearing env-var
clothing, which is why "set four env vars and hope" is the current workflow.
Move them to the payload and profiles; leave credentials in env.

Implement as a real resolver — `core/config_resolver.py` — that returns a
frozen `RunConfig` and, alongside it, a per-key provenance map. Kill
`resolved_tts_transport` (`core/config.py:47`); legacy aliasing belongs in one
normalisation step in the resolver, not as a property on `Settings`.

### Profiles

Named presets, because most runs are one of four things:

```json
{
  "load-test":     { "skip_evaluation": true,  "recording": false, "trace_sample_rate": 0.02 },
  "regression":    { "skip_evaluation": false, "recording": true,  "repeats": 3 },
  "latency-probe": { "max_turns": 4, "skip_evaluation": true, "recording": false },
  "debug":         { "log_level": "DEBUG", "recording": true, "save_raw_frames": true }
}
```

Profiles are rows in Postgres, not a file — same reasoning as §2. `"profile":
"load-test"` in the payload, and the resolved values land in
`runs.resolved_config` so the run records what the profile *was* at execution
time, not what it is now.

### Features → processors

The pipeline list from §1 is *derived* from config:

```json
"features": {
  "barge":      { "enabled": true, "mode": "llm" },
  "dtmf":       { "transport": "rest" },
  "recording":  false,
  "turn_detection": "silero"
}
```

Disabled feature ⇒ processor not constructed. Not a runtime `if` inside a
1572-line file.

### Persist the resolved config with the run

`runs.resolved_config` JSONB (§9), secrets redacted, with the provenance map
and the scenario hash. You already record the scenario and the rendered
prompts — this closes the loop, and being a column rather than a file makes it
queryable: *"show me every run in the last month where `tts.transport` resolved
to `blocking`"* is one `WHERE resolved_config->'tts'->>'transport' = 'blocking'`.

### Vendor config as blocks, not flat keys

Follow `ServiceFactory.create_from_config`: each vendor gets a dict, so adding
ElevenLabs doesn't mean adding five top-level env vars.

```json
"vendors": {
  "tts": { "provider_key": "deepgram-tts-en-US", "voice": "aura-asteria-en",
           "config": { "transport": "ws" } },
  "stt": { "provider_key": "deepgram-asr-en-US", "model": "nova-2",
           "config": { "utterance_end_ms": 1000 } },
  "llm": {
    "caller":    { "provider_key": "google-llm", "model": "gemini-2.5-flash" },
    "evaluator": { "provider_key": "openai-llm", "model": "gpt-4o" }
  }
}
```

Adding a vendor is one row in judge_it's `providers` table plus one secret in
Secrets Manager (§8) — then it's selectable from a payload. No env var, no code
change, no judge_it release. For an OpenAI-compatible LLM the row is
`{provider_key, vendor, service_type, base_url}` and nothing else (§8).

---

## 4. Decision: real service mode + API

Mirror OSV's `main.py --mode` dispatch, and note the TODO already in that file
about the elif-chain sprawl — don't reproduce that. Use subcommands.

```bash
judge_it serve api      --port 8000    # control plane: submit, query, cancel
judge_it serve worker   --concurrency 10   # executes runs
judge_it serve media    --port 8765    # Twilio TwiML + media-stream WS (public)
judge_it reap                          # reconciler tick (§7)
```

`api` and `worker` split because the API must stay responsive while calls run
for minutes, and because they scale on different axes (API on RPS, workers on
Twilio channels). `media` is separate because it's the only component that
needs a public URL, and it's the one you don't want to redeploy casually.

### Endpoints

Resource shape follows `app/interface/views/imposter.py` — that's the
in-house precedent for "submit a long test job, poll it, download results,"
and matching it means the frontend patterns transfer.

```
POST   /v1/runs                        202 → {run_uuid, status: queued}
GET    /v1/runs/{run_uuid}             status + summary + verdict
GET    /v1/runs/{run_uuid}/artifacts   presigned S3 URLs (audio) + Loki/Tempo deeplinks
POST   /v1/runs/{run_uuid}/cancel      must actually hang up the Twilio call
GET    /v1/runs?interaction_uuid=…     ← the lookup your callers need
GET    /v1/runs?client_uuid=&assistant_uuid=&assistant_version=&suite=&since=

POST   /v1/campaigns                   202 → {campaign_uuid}
GET    /v1/campaigns/{uuid}            aggregate progress
GET    /v1/campaigns/{uuid}/report     pass rate, per-scenario breakdown

POST   /v1/scenarios                   store a payload → {name, version}   (§2)
GET    /v1/scenarios?suite=&client_uuid=
GET    /v1/scenarios/{name}/versions
GET    /v1/scenarios/{name}/versions/{a}/diff/{b}
POST   /v1/scenarios/lint              validate a payload without dialing

POST   /v1/gates/{suite}               CI gate: run suite, return pass/fail (§10)

GET    /openapi.json                   the payload schema, published (§2)

GET    /healthz  /readyz  /metrics
```

### Request body

The run envelope wraps the §2 scenario payload — one of `scenario` (inline) or
`scenario_ref` (registry), never both:

```json
{
  "scenario_ref": "resurgent-auth-happy-path@3",
  "scenario":     { "...": "inline alternative — the full §2 payload" },

  "profile":   "regression",
  "vars":      { "account_last4": "4821" },
  "overrides": { "max_turns": 20, "vendors": { "tts": { "name": "elevenlabs" } } },

  "interaction_uuid":  "550e8400-e29b-41d4-a716-446655440000",
  "correlation_uuid":  "nightly-2026-08-04",

  "callback_url":    "https://caller.internal/judge_it-webhook",
  "idempotency_key": "nightly-2026-08-04-A-001",
  "priority":        "normal",
  "labels": { "suite": "regression", "git_sha": "abc123", "region": "us" }
}
```

`client_uuid`, `assistant_uuid`, `assistant_version`, `target`, and the vendor
block live in the scenario payload, not the envelope — the envelope is *how to
run it*, the payload is *what to run*. `vars` and `overrides` are the only
things that legitimately vary per invocation of the same scenario, and both are
merged into `runs.scenario_payload` before execution (§2).

Response echoes `scenario_hash` so the caller can assert it ran what it
thought it ran.

### Async, callback-driven. Never synchronous.

A call runs for minutes. Holding an HTTP connection open for it fails on
infrastructure before it fails on logic — proxy and ALB idle timeouts are
typically 60s, and a tied-up worker per in-flight call caps you at the worker
count. So:

```
OSV                                          judge_it
 │  POST /v1/runs  {payload, callback_url}
 │ ───────────────────────────────────────►  validate + admit
 │  ◄─── 202 {run_uuid, status:"queued"}     enqueue
 │                                            │
 │  ◄─── POST callback  seq=1 dialing         worker picks up
 │  ◄─── POST callback  seq=2 in_call         bot answered
 │  ◄─── POST callback  seq=3 evaluating      call ended
 │  ◄─── POST callback  seq=4 completed       + verdict + debugging_urls
 │                                            
 │  GET /v1/runs/{uuid}  ─────────────────►   (safety net — callbacks get lost)
```

**Why callbacks and not a WebSocket** — the reasoning you gave is right, and
there's a second reason: a WS needs sticky routing, reconnect handling, and a live
connection per run, which at 50 concurrent runs is 50 connections to keep alive
across deploys. A callback is a stateless POST that OSV already knows how to
receive (`app/interface/views/webhooks.py`). JudgeIt → OSV over webhook; OSV →
browser however OSV likes (its own polling or SSE). Clean seam, and the browser
never talks to judge_it.

#### The callback contract

Fire on state transitions, not continuously:

| Event | When |
|---|---|
| `dialing` | Twilio call placed |
| `in_call` | bot answered, conversation started |
| `evaluating` | call ended, evaluator running |
| terminal | `completed` \| `failed` \| `timed_out` \| `cancelled` \| `abandoned` |

Body: `run_uuid`, `correlation_uuid`, `interaction_uuid` (when known),
`seq`, `state`, `outcome`, timestamps, and on terminal the verdict summary plus
`debugging_urls`. Per-turn live-transcript events are opt-in via
`callback_events: [...]` — chatty, and most callers don't want them.

**Include a monotonic `seq` per run, and have OSV discard anything older than
what it has stored.** This is the bug that bites otherwise: a retried `in_call`
delivery can arrive *after* `completed`, and without `seq` the UI flips backwards
from "passed" to "in progress". Signed with HMAC-SHA256 + timestamp + nonce;
retry with exponential backoff a few times, then stop and let OSV's polling catch
up.

### Batches: OSV submits many, judge_it decides the concurrency

```
POST /v1/campaigns
{ "scenarios": [ {...}, {"scenario_ref": "auth@3"}, … ],
  "max_concurrency": 25, "fail_fast": false, "callback_url": "…" }

202 { "campaign_uuid": "…", "run_uuids": [...], "total": 50,
      "effective_concurrency": 8,
      "concurrency_capped_by": "twilio_channels" }
```

`max_concurrency` is a **request, not a setting.** JudgeIt clamps it against the
global cap and the per-vendor caps, then **echoes back what it actually chose and
why** — so OSV's UI can honestly show "running 8 of 50, concurrency capped from 25
by Twilio channels" instead of silently under-delivering. Callbacks fire per run,
plus one campaign-level aggregate on completion.

The existing `orchestrator/campaign_runner.py` worker-pool-over-asyncio-queue
logic survives this; it just moves behind the broker instead of being the
top-level scheduler.

### Running 50 at once: what actually constrains it

Not CPU. The ceilings are external, so concurrency is admission-controlled per
vendor rather than by a single number:

| Constraint | Control |
|---|---|
| Twilio channel concurrency | global admission cap (`probe_twilio_concurrency.py` already measures it) |
| Deepgram concurrent connections | per-vendor token bucket |
| Gemini/OpenAI RPM + TPM | per-vendor token bucket + circuit breaker |
| Silero VAD + mulaw conversion (the only real CPU) | bounds tasks-per-process; scale out with pods |

Saturation returns `429` with `Retry-After`, or queues with a visible
`judge_it_queue_depth` — never an opaque 20-minute timeout.

### Data isolation at concurrency

The reason the file-based design had to go, restated as a concurrency property:

- **Every write carries `run_uuid`** as a key. `run_turns`, `run_llm_calls`,
  `run_events` — no shared mutable state between runs, so isolation is structural
  rather than something the code has to remember.
- **One pipeline object graph per run.** The frame bus and its observers are
  per-run instances, not globals.
- **`_sessions` → Redis keyed by `run_uuid`**, so any media pod can serve any
  call's webhook and a restart doesn't strand in-flight runs.
- **ContextVars now only *label* log records**, they don't route them to files.
  The old per-run `FileHandler` + `_CallIdFilter` was cross-run leakage waiting to
  happen — it already leaked twice — and 50 concurrent runs would be its worst
  case. With no file handles, that failure mode cannot occur.
- **Shared vendor connections are the one thing to watch.** TTS/STT sockets must
  be per-run or safely multiplexed; `DeepgramTTS` is already instantiated
  per-scenario for voice overrides, so extend that rather than pooling globally.

Log filtering then works the same at 1 run or 50:

```logql
{app="judge_it"} | logfmt | run_uuid="…"                  # one run out of 50
{app="judge_it"} | logfmt | correlation_uuid="nightly-…"   # one campaign
{app="judge_it", level="ERROR"} | logfmt | worker_id="3"   # one bad worker
```

`seq` gives ordering within a run (§6), because Loki won't guarantee it across
streams.

#### The OSV side of this does NOT work today — measured

You cannot pull one conversation's log out of the text runtime, because the
`interaction_uuid` is not bound into the logging context. Counted over the
10 minutes covering the two Veros runs:

```logql
sum(count_over_time({app="orchestrator-service-text"} |= "<interaction_uuid>" [10m]))  →     19
sum(count_over_time({app="orchestrator-service-text"}                          [10m]))  → 10,415
```

**0.18%.** And those 19 are not spread through the turn — they cluster at a
handful of sites that happen to interpolate the uuid into a message string:
`context_manager.py:1639`, `redis.py:323`, `views.py:947/972`,
`utils.py:85`, `integration.py:89`, and the `post_call_event.py` block. The
actual per-turn work carries no correlation id at all — `text_bots.py`,
`frame_processor.py`, `tags.py`, `db_writer.py`, `worker.py`.

Two consequences, both felt while debugging the turn-4 stall:

1. **A uuid filter shows request entry and post-call, and nothing in between.**
   It looks like turns are missing from the logs. They are not — the lines exist,
   they are just unfilterable.
2. **The lines that matter most are the ones without the uuid.**
   `worker.py:1261 idle timeout detected` — the line that explains the whole
   failure — has no interaction on it. It had to be tied to the run by
   timestamp, by hand. Turns also land on different pods (no session affinity:
   `n7cv4`, `gskg5`, `q547q`, `m7bpt`, `bkjj5` all served one conversation), so
   a pod filter does not substitute.

**Proposed OSV change** (needs approval before anyone writes it): bind
`interaction_uuid` — and judge_it's run id when supplied — into the request's
logging context for its whole lifetime, so every line inherits it. With loguru
that is a `logger.contextualize(...)` around the request handler plus a `{extra}`
field in the sink format; no call-site edits. Against the three-part bar:

- **Real problem?** Yes — one conversation is currently unreconstructable from
  logs, and the diagnostic line is the one that lacks the id.
- **No judge_it-side workaround?** Correct. JudgeIt cannot add fields to OSV's log
  lines. JudgeIt's own logs already carry the uuid; that is the half that works.
- **Small?** Yes — context binding plus a format string, no behaviour change.

Until it exists, the working recipe is: get the turns from
`OSVClient.get_conversations(client_uuid, interaction_uuid)` (complete, ordered,
with tool calls), and use Loki only for the server-side *why*, correlating on
timestamp.

### What OSV has to build for this

Small, and mostly exists:

1. A webhook receiver — `app/interface/views/webhooks.py` is the pattern.
2. A table holding per-test-case run status, keyed by `run_uuid`, with the `seq`
   guard.
3. Frontend polls **OSV**, not judge_it.

### Semantics to get right

- **Idempotency is mandatory, not nice-to-have.** Every retry that isn't
  deduped places a *real billed phone call*. Same `idempotency_key` ⇒ same
  `run_uuid`, HTTP 200, no second dial. Default the key to
  `interaction_uuid` when supplied. OSV's `app/background/post_call/markers.py`
  is the in-house idempotency primitive to copy.
- **Admission control.** judge_it's binding constraint is Twilio channel
  concurrency, not CPU — you already have
  `scripts/probe_twilio_concurrency.py`, so this is a known limit. Enforce a
  global cap and a per-client cap; return `429` with `Retry-After` when
  saturated rather than queueing unboundedly and timing out. Export
  `judge_it_queue_depth` and `judge_it_admission_rejections_total`.
- **Auth.** Service-to-service. The primary caller is **OSV's interface**
  (§8) — it owns the UI, resolves the provider config, and submits runs. Reuse
  OSV's JWT scheme (`app/interface/middleware/auth.py`) so there's one kind of
  credential. Note the trust boundary this creates: the token identifies OSV, not
  the end user, so `client_uuid` in the payload is OSV *vouching* for whoever
  clicked — credential resolution is only as strong as OSV's own tenant isolation.
  Additional callers (CI, cron, scripts) get their own tokens scoped per
  `client_uuid`.
- **Signed callbacks.** A test result that says "PASS" needs to be unforgeable —
  see the callback contract above.

---

## 5. Decision: correlation IDs — the `interaction_uuid` problem

This is the part of your ask that needs the most care, because **the obvious
version of it doesn't work over PSTN**, and it's better to know that now.

> **STATUS 2026-08-11 — largely solved; read this before acting on §5a-d.**
> Everything below about PSTN not carrying headers is still true, and now
> mostly moot: judge_it no longer dials PSTN for these runs. Mechanism **(a) is
> shipped and verified** — and needed **no OSV change at all**, contrary to
> what (a) assumed. Mechanism **(b), the reconciler, is no longer needed** for
> SIP runs. The `is_test` half of this problem is NOT solved: the flag now
> reaches the bot but nothing consumes it (§11.1).

### Three IDs, not one

Do **not** overwrite judge_it's run id with the caller's uuid. Store three:

| Field | Owner | Nullable | Purpose |
|---|---|---|---|
| `run_uuid` | judge_it | never | primary key, log field, TwiML/WS path, S3 key prefix |
| `interaction_uuid` | OSV | yes → backfilled | joins to the bot's `Interaction` row, its logs, its metrics |
| `correlation_uuid` | the caller | yes | the caller's own batch/job id (nightly suite, CI build) |

Why not reuse the caller's uuid as `run_uuid`: one interaction may be probed
by several runs (repeats for flake detection, §10), and a caller retry would
collide on your primary key. Keep them separate and index
`interaction_uuid`.

### The hard part

For an outbound PSTN call, **OSV mints the uuid itself and there is currently
no injection point**:

```python
# app/llm/context_manager.py:1604
interaction_uuid = str(uuid.uuid4())
self.variables["interaction_uuid"] = interaction_uuid
```

So the caller can't hand judge_it a uuid that the bot will also use — not
without a change on the OSV side *and* a transport that can carry it. Three
mechanisms, in order of preference:

**(a) SIP header injection — SHIPPED 2026-08-11. Needed no OSV work after all.**

Dial a **SIP URI** rather than a PSTN number. Twilio's `<Sip>` verb forwards
`X-`-prefixed custom headers; LiveKit SIP surfaces inbound SIP headers to the
agent as participant attributes (`sip.h.x-*`). Then:

```
judge_it (mints or receives interaction_uuid)
  → TwiML <Sip>sip:bot@trunk?X-Interaction-Uuid=…&X-JudgeIt-Run=…&X-Is-Test=1</Sip>
  → LiveKit SIP participant attribute sip.h.x-interaction-uuid
  → livekit_session.py reads it
  → LLMConfigManager(interaction_uuid=…, is_test=True)
  → Interaction(uuid=<supplied>, is_test=True)
```

**What actually shipped, and how it differs from the sketch above.**

The sketch routes through `<Sip>` TwiML and asks OSV to adopt the uuid we
inject. Neither turned out to be necessary:

- **No TwiML `<Sip>` verb.** Twilio's REST `POST /Calls` accepts a SIP URI
  directly in `To`, and fetches the TwiML at `url` *after* the SIP call
  connects — so judge_it's existing `<Connect><Stream>` and the whole
  media/DTMF-reattach design work unchanged. One kwarg, not a new dial flow.
- **No OSV change.** The sketch's last two lines
  (`LLMConfigManager(interaction_uuid=…)` → `Interaction(uuid=<supplied>)`)
  are what made this cross-team. Invert the direction instead: don't make OSV
  adopt *our* id, put *our* id on *their* row. `x-judge_it-session-id` is stored
  in `interaction_metadata` and is queryable, which is all a join needs.
  `LLMConfigManager` never has to learn a new parameter.

Addressing is header-driven, not URI-driven — the DID does **not** go in the
user part:

```
From: +12202206097                                   (a Twilio number)
To:   sip:1205@581mqcm8zvu.sip.livekit.cloud:5060    (1205 = LiveKit trunk id)
        ?X-VirtualNumber=%2B18447331982              (the bot DID — selects the BOT)
        &X-JudgeIt-Session-Id=<run session>
        &X-Is-Test=1
```

`1205` selects the *trunk*; `X-VirtualNumber` selects the *bot*, via the
inbound trunk's `headersToAttributes: {"X-VirtualNumber": "virtualNumber"}`.

**Verified** on interaction `799a39eb-d01e-4fb0-acb1-4211cf8457df` — a real
115.6s conversation, disposition `faq_due` — whose `interaction_metadata`
carried:

```
x-virtualnumber      = +18447331982    # %2B percent-decoded back to '+'
virtual_number       = +18447331982    # the headersToAttributes mapping
x-judge_it-session-id  = <run session>   # the join key
x-twilio-callsid     = CA419b539b…     # stamped by Twilio itself
x-is-test            = 1
ani / caller_number  = +12202206097
```

Four things worth carrying forward:

1. **Header names arrive lowercased** — read `x-virtualnumber`, not
   `X-VirtualNumber`.
2. **Twilio stamps its own `X-Twilio-CallSid`** (and `X-Twilio-AccountSid`) on
   the INVITE. Undocumented, so don't depend on it — but it means the bot gets
   Twilio's SID for free. judge_it *cannot* inject the Call SID itself: it is
   minted by the same `calls.create` request that sends the INVITE, which is
   why the injected key is judge_it's `session_id` (mapped to `call_sid`
   judge_it-side) rather than the SID.
3. **LiveKit needs a dispatch rule, not just a trunk.** With the trunk alone it
   answers `404 Does not match Trunks or Dispatch Rules` — and Twilio reports
   only a bare `failed`/0s with **no error code, no Debugger alert and no Voice
   Insights record**. That combination cost most of the debugging time.
4. **Reading the real SIP response** is the diagnostic that unblocked it, since
   Twilio surfaces nothing: open a TCP socket to the SIP host, send a
   hand-built INVITE with SDP, and read *past* the provisional `100 Processing`
   / `180 Ringing`. Time-to-terminal also discriminates — ~0.5s is an explicit
   reject, ~9s is a timeout. `scripts/probe_sip_invite.py` documents both.

Twilio's SIP-interface egress turned out to be the **same** `54.172.60.0/30`
range Elastic SIP Trunking uses (`x-lk-real-ip = 54.172.60.1`), so the feared
IP-allow-list divergence was never real. UDP works; no `;transport=` needed.

**(b) Post-hoc reconciliation — SUPERSEDED for SIP runs; keep only if you still
dial true PSTN.**

Now that (a) works, this is a heuristic where an exact key exists — don't build
it for SIP runs. It remains the only option for `call_transport=pstn`, which is
still judge_it's default, so the mechanism is documented rather than deleted.

True PSTN (`<Dial>` to an E.164 number) cannot carry custom headers. So:
judge_it finishes the run with `interaction_uuid = null`, and a reconciler
(§7) resolves it afterwards by querying OSV:

```
GET /api/client/{client_uuid}/interactions?virtual_id={bot_did}
    &recipient_id={judge_it_caller_id}&started_between={t0-30s},{t1+30s}
```

Exactly one match ⇒ backfill. Zero or many ⇒ leave unresolved and alert.
Using judge_it's caller ID as `recipient_id` makes this nearly always unique,
because only judge_it calls from that number.

**(c) Direct transport — bypass telephony entirely.**

Add a non-telephony transport where judge_it talks to OSV's voice gRPC server
or text runtime directly. There, `interaction_uuid` and `is_test` are just
request parameters — no SIP, no reconciliation, no Twilio cost, seconds
instead of minutes. This is the mode your CI gate should use for breadth;
keep PSTN for the runs where the carrier path is part of what you're
testing.

### (d) Text transport — already solved, and it should be the default

OSV's **text runtime** (`main.py --mode text`) exposes
`POST /text_runtime/post_message_web`: one HTTP request per user turn, returning the
bot's reply. Not a call — the same assistant, driven over HTTP.

It is the "direct transport" this document has been proposing, and **it already
exists with the two fields that were blocking correlation**
(`app/runtime/text/views.py:52-85`):

```python
class PostInteractionMessageRequestWeb(BaseModel):
    recipient_id: str
    user_message: str
    channel: Channel                       # SMS | EMAIL | WEB_CHAT | VOICE
    assistant_uuid: Optional[str]
    is_test: Optional[bool] = False        # ← analytics pollution: solved
    interaction_uuid: Optional[str] = None # ← continues an existing conversation
    fc_version: Optional[int] = None       # pin the assistant version
```

**How correlation works here — no injection needed.** `interaction_uuid` on the
request *looks up* an existing interaction (`views.py:531-536`); it does not create
one with a chosen id. So:

- **Turn 1:** judge_it POSTs with no `interaction_uuid`. OSV creates the interaction
  and **returns `interaction_uuid` in the response**
  (`PostInteractionMessageResponseWeb`).
- **Turns 2..N:** judge_it passes it back to continue the conversation.

JudgeIt learns the real interaction uuid within a second of starting, from the
response, with **zero OSV changes and no SIP-trunk probe.** That's
`CorrelationMethod.RESPONSE`, and it is simpler than anything proposed above.

#### Same brain, different mouth

Same `LLMConfigManager`, same flow config, same modules, same tools/MCP. The only
channel deltas in the system prompt are small — an opt-out block for SMS, a
low-signal block for EMAIL (`context_manager.py:254-265`). `DTMF_PROMPT` is added
regardless of channel.

The response is also **richer than voice**:

```python
output_message, current_module, tool_calls, function_times,
total_response_time, interaction_metadata, interaction_uuid
```

Over PSTN judge_it only hears audio. Here it can assert on **module transitions and
tool calls** — assertions that are impossible over voice.

#### What it costs, and what it cannot test

Dropping Twilio (~$0.045/3min), STT (~$0.02) and TTS (~$0.03) saves ~$0.10 a run.
The evaluator LLM dominates and is channel-independent, so this is roughly
**2-3x cheaper, not 10x.** The real unlock is elsewhere: no 3-minute wall clock (30
turns of HTTP is under a minute) and **no Twilio channel ceiling** — concurrency is
bounded by LLM RPM instead, so hundreds rather than ~25. That is what makes a CI
gate viable.

**Not testable in text mode**, and it is a real list:

| Voice-only | Why it matters |
|---|---|
| STT mishearing | a large class of production bugs — the bot mis-hears a DOB |
| TTS / pronunciation | numbers, names, currency |
| Barge-in | judge_it's entire `barge_in` feature |
| DTMF **collection mechanics** | digit timeouts, partial entry, RFC2833 — but see below: the *logic* is testable |
| Turn-taking, VAD, `user_idle` | the bot hanging up on a slow caller |
| `bot_turn_latency` | the outside-in latency metric (§6) is voice-only |

#### DTMF is mostly testable in text — one ~10-line OSV change away

Worth correcting an earlier assumption in this doc. Trace how DTMF actually reaches
the model:

```
bot emits "<DTMF:16>" in its response text
   → TagsFrameProcessor._handle_dtmf_tag  (app/runtime/voice/processors/tags.py:424)
     strips the tag, pushes StartDTMFFrame(max_digits=16)
   → [voice]  DTMFFrameProcessor collects keypresses, then
              _finalize_dtmf_input pushes them as a **TranscriptionFrame** (dtmf.py:232)
   → the LLM sees the digits as ordinary user text
```

**From the model's point of view, DTMF input is already just text.** So posting
`"1234567890123456"` as `user_message` is identical to what the LLM receives on a
real keypad entry. And `TagsFrameProcessor` is shared — `text_bots.py:155`
instantiates the same class — so the bot's tag emission works in text too.

The only thing missing is the **signal back to the caller**. In text mode
`StartDTMFFrame` is pushed and nothing consumes it, so the tag is stripped and
`output_message` gives no hint that DTMF was requested or how many digits.

The fix follows a precedent that already exists: **`<RECORD>` is already surfaced
to text callers as `message_type="RECORD"`** (`views.py:183, 272, 591`). Add the
same for DTMF — `message_type="DTMF"` plus `max_digits` — and text-mode DTMF becomes
first-class. That is a handful of lines, and a far smaller ask than the SIP-header
work.

**What it unlocks:** the bot's DTMF *request* logic (does it ask at the right moment,
with the right digit count?) and its *validation* of the digit string — C-010 card
14 digits, C-011 18 digits, C-012 press-2-to-reenter, C-008 switch debit→ACH. That
moves the 20 `pif/` scenarios from voice-only to text-testable: **~90% → ~98% of the
hand-written corpus.**

**What stays voice-only:** the collection mechanics — `_digit_timeout_handler`,
partial entry, the no-digit-when-voice-detected fallback that forwards the
transcription instead (`dtmf.py:225-236`), RFC2833 signalling, and judge_it's own
`dtmf_transport: rest` reconnect dance.

**And it does not fix the in-call side-effect hazard.** Text mode executes the same
tools through PIS, and `is_test` protects far less than its name suggests.

Measured against the live US text runtime on 2026-08-17 (Loki,
`app="orchestrator-service-text"`, interaction `a10b2719-…`):

- `is_test=true` suppresses exactly one thing — the compliance event:
  `post_call_event.py:326 not sending compliance event since it is a test`.
- **Post-call events run after EVERY turn, not at call end**
  (`post_call_event.py:77 processing post-call events … duration=0 status=none`),
  and they POST the caller's real data — name, account number, SSN last-4, due
  amount, address — to the live CRM:
  `integration-proxy.crm.svc.cluster.local/debt_collections/post_call_outcomes/`,
  returning `_post_data_success_`. That fired on a test interaction, per turn.
- Dispositions are written normally (`rpc` → `rpc_authenticated`), and a call
  summary is generated per turn.

So a text run is **not** side-effect-free, and it is not even side-effect-free
*before* the flow reaches payment: the post-call integration writes to the
client's CRM from the first authenticated turn onward. Treat `is_test` as an
analytics/compliance hint only.

#### Two transports, and scenarios declare which they need

- **`transport: text`** — the default. Flow logic, module transitions, tool calls,
  hallucination, compliance wording. Cheap, fast, highly concurrent, correlation
  and `is_test` for free. Use it for the CI gate and the bulk of regression.
- **`transport: twilio`** — a deliberate, smaller set. Audio, barge, DTMF,
  turn-taking, latency, carrier path. Expensive, slow, channel-limited.

A barge scenario is **not runnable** in text mode — that is a capability check at
lint time (§8), not a runtime surprise.

#### Testing a voice bot in text mode: only with `channel=VOICE`

This is load-bearing, not a detail. An assistant has **four separate flow configs**
(`app/models/assistant.py:187-190`):

```python
voice_flow_config      sms_flow_config
email_flow_config      web_chat_flow_config
```

and `get_channel_flow` (`app/models/utils.py:29`) filters
`FlowConfig.channel == channel` with **no fallback — it returns `None`**.

So:

| Request | Flow actually exercised |
|---|---|
| `channel=WEB_CHAT` | `web_chat_flow_config` — **a different flow**, or `None` |
| `channel=VOICE` | `voice_flow_config` — the real production voice flow |

**Driving a voice bot over `WEB_CHAT` tests nothing about that bot.** Different
modules, different edges, different `general_instruction`. JudgeIt must always send
`channel=VOICE` when the target is a voice assistant, and treat a `None` flow as a
hard failure rather than a fallback.

The good news: this is what OSV's own test framework already does — imposter passes
`channel=Channel.VOICE` straight into `LLMConfigManager` (`simulator.py:79`) with
`is_test=True`. **OSV already tests voice bots without audio.** And
`post_message_web` has no channel allowlist — every channel branch in it is
`if SMS` / `if EMAIL`, so VOICE falls through to the default path.

**So the one experiment worth running before anything else in this plan:** POST to
`/text_runtime/post_message_web` with `channel=VOICE, is_test=true` against a real
voice assistant and check that the reply comes from the voice flow (`current_module`
should be a voice-flow module name). Ten minutes, and it decides whether judge_it's
cheap path exists today or needs an OSV endpoint.

If the endpoint doesn't handle VOICE cleanly, the fallback ask is small — a thin OSV
endpoint wrapping `LLMConfigManager(channel=VOICE, is_test=True)`, i.e. "imposter as
an API". That is a far smaller cross-team request than the SIP-header work.

#### What text + `channel=VOICE` actually catches

Worth being concrete, because it's more than it sounds. The production failure
documented in your own `resurgent-prompt.md` (call I483617082) was: bot skipped a
pre-flight check → called `get_account_payment_options` → backend errored → bot
**fabricated success-path wording** → called a second tool that also failed → leaked
raw error strings to TTS.

Every step of that is reproducible in text mode. Same for module-transition bugs,
tool-call sequencing, hallucination, compliance wording, variable rendering, and
out-of-scope handling. **The failures in that file are overwhelmingly reasoning
failures, not audio failures** — which is the real argument for making text the
default transport.

### Building both transports in judge_it

> **Implementation spec: [dual-transport.md](./dual-transport.md).** That file has
> the protocol, the exact request/response mapping, the capability matrix, the two
> OSV asks, and the build order. The rest of this subsection is the reasoning.


The good news: **`AICallerEngine` and `Evaluator` are already transport-agnostic.**
The engine emits `{speech, dtmf, hangup}` per turn; the evaluator consumes a
transcript. Neither knows about audio. What's coupled is `orchestrator/runner.py`,
which hardcodes `MediaStreamSession`.

#### The seam

```python
# judge_it/transport/base.py
class Transport(Protocol):
    async def start(self) -> BotTurn: ...          # place call / open interaction; returns greeting
    async def send(self, turn: CallerTurn) -> BotTurn: ...   # speech+dtmf → bot reply
    async def close(self) -> None: ...
    capabilities: TransportCapabilities

@dataclass(frozen=True)
class TransportCapabilities:
    barge: bool           # voice only
    dtmf_timing: bool     # voice only — collection timeouts, partial entry
    audio: bool
    turn_latency: bool    # bot_turn_latency measurable
    module_visibility: bool   # text only — current_module / tool_calls in the reply
```

`send()` fits text natively (HTTP request/response *is* a turn). For voice it wraps
what `stream.py` already does — TTS out, wait for turn end, return the transcript —
so **the audio machinery stays where it is**, behind the method.

Barge is the one thing that doesn't fit a turn-level protocol, because it fires
*during* bot speech. Handle it as an optional hook the engine registers and only the
voice transport ever calls. `capabilities.barge = False` then makes a barge scenario
a **lint-time rejection**, not a runtime surprise.

#### Sequencing: don't unify first

The tempting move is to extract the shared turn loop out of
`stream.py:_conversation_loop` up front. **Don't.** That loop holds the smart-grace
turn detection and the barge scheduling — the two most delicate behaviours in the
codebase — and breaking the working voice path to enable the text path is a bad
trade.

Instead:

1. **Build `transport/text.py` as a standalone path.** Its own ~40-line turn loop,
   reusing `AICallerEngine` and `Evaluator` unchanged. Touch nothing in `stream.py`.
   Total maybe 150 lines. This is where ~98% of the corpus coverage comes from.
2. **Add `transport: text | twilio` to the payload** (§2) and dispatch in
   `runner.py`.
3. **Only then**, if the duplication actually hurts, extract the shared loop and put
   `MediaStreamSession` behind `Transport`.

Temporary duplication of a 40-line loop is much cheaper than risking the audio path.

#### What text mode adds to the result model

`CallResult` gains optional fields the voice path can't populate:

```python
modules_visited: list[str]        # from current_module per turn
tool_calls:      list[dict]       # from the response
transport:       str              # "text" | "twilio"
reasoning_latency_ms: list[float] # total_response_time per turn
```

That enables something worth more than the cost saving: **deterministic assertions
alongside the LLM verdict.**

```yaml
assert:                              # text transport only; lint rejects it on sip/pstn
  must_visit_modules:     ["auth_dob", "auth_ssn", "mini_miranda"]
  must_not_visit_modules: ["card_collection"]
  must_call_tools:        ["get_accounts_by_additional_info"]
  must_not_call_tools:    ["get_design_your_own_payment_plan_options"]
  tool_order:             ["get_account_scheduled_payments", "get_account_payment_options"]
  max_tag_only_turns:     0
```

**Status: implemented** (2026-08-25) — `judge_it/engine/assertions.py`, wired
through `CallResult.passed` so a failure vetoes the LLM verdict everywhere.

> The key is `assert:`, **not** `assertions:`. An earlier draft of this section
> said `assertions:`, which is a V1 legacy key that `lint.py` rejects — a
> scenario using it fails before it runs.

Those are exact checks, not a judge's opinion — and they directly target the
evaluator-trust risk (§10.2). The I483617082 failure (pre-flight skipped, wrong tool
called after an error) is caught by `tool_order` deterministically, with no LLM in
the loop. Unobserved data yields INCONCLUSIVE rather than a failure, so a missing
OSV token cannot turn a good run red — see `docs/dual-transport.md` for the full
semantics.

#### Running one scenario on both

```json
"transports": ["text", "twilio"]
```

Base scenarios run on text for every assistant version change and on Twilio
nightly. Same payload, two runs, two `run_uuid`s, comparable verdicts — and a
disagreement between them is itself a finding: it means the bug is in the audio
layer, not the reasoning.

### Record the resolution method

```python
class CorrelationMethod(StrEnum):
    RESPONSE    = "response"      # OSV returned it on turn 1 — text transport (d)
    SIP_HEADER  = "sip_header"    # we minted it, injected via SIP (a) — LIVE
    RECONCILED  = "reconciled"    # matched post-call (b) — pstn transport only
    UNRESOLVED  = "unresolved"    # give up, alert
```

`SIP_HEADER` is live as of 2026-08-11 (§5a). Note it resolves in the *opposite*
direction to what the name suggests: the join key stored on the interaction is
judge_it's `session_id`, not an `interaction_uuid` we minted — so the run's
`interaction_uuid` is still backfilled, just by an exact lookup on
`interaction_metadata->>'x-judge_it-session-id'` instead of a time-window guess.

Store it on the run. Without it, `interaction_uuid` is a field of unknown
trustworthiness, and six months from now nobody will remember which runs had
a real join and which had a heuristic one. Export
`judge_it_correlation_total{method}` — a rising `unresolved` rate is a real
alert.

### Store the report URL

OSV's imposter already does this (`app/lib/imposter/core/runner.py:246`):

```
https://llm-studio.judge_it.dev/clients/{client_uuid}/conversations?interaction_uuid={interaction_uuid}
```

Put it on the run record and in the callback payload. It's the single most
useful field for whoever reads a failure.

---

## 6. Decision: observability that joins across both services

The goal isn't "more logs." It's: given a failed run, one query returns
judge_it's view *and* the bot's view of the same conversation.

### Logging — stdout → Loki, zero files

**Decision: judge_it writes no log files. Ever.** Everything goes to stdout in
logfmt; a collector ships it to Loki. This deletes, rather than fixes:

- `debug.log` per run,
- `CallLogWriter`'s `FileHandler` attached to the shared `judge_it` logger,
- the `_CallIdFilter` contextvar-matching filter,
- `ENABLE_LOCAL_LOGFILE`-style local sinks.

That whole mechanism has already caused cross-call log pollution twice (the
comments in `logwriter.py` and `core/log_context.py:1-21` record both
incidents). It exists only because logs had to be split into files. Once they
don't, the bug class disappears — and the contextvars stay, because Loki needs
them as *fields*.

```
judge_it process ──stdout(logfmt)──► Grafana Alloy ──► Loki ──► Grafana Explore
    (no disk)                      (DaemonSet /                    │
                                    sidecar)                       ▼
                                                            Tempo (via trace_id
                                                             derived field)
```

**Format: logfmt, not JSON.** Not because logfmt is better, but because OSV's
`app/lib/slog/slog.py` already emits it. Matching means one set of LogQL
queries works against both services, which is the entire point of the
exercise.

#### The label-cardinality rule (the thing to get right)

Loki indexes **labels**, not line content. Every distinct label-value
combination is a separate stream. Putting `run_uuid` or `interaction_uuid` in
a label creates one stream per call — that will fall over, and the fix
afterwards is a re-ingest.

```
LABELS (bounded, ~hundreds of streams total):
  app=judge_it  component=api|worker|media|reaper  env=dev|stage|prod
  region=us|india|canada  level=INFO|ERROR
      ↑ that's it. Nothing per-run, per-scenario, or per-client here.

LINE FIELDS (unbounded, free — this is where identity lives):
  run_uuid  interaction_uuid  correlation_uuid  client_uuid
  assistant_uuid  assistant_version  scenario_name  scenario_hash
  profile  worker_id  turn  seq  trace_id  span_id
```

**The payload itself is not a log field.** It's multi-KB, it holds the persona
and the PII-bearing `vars`, and it's already in `runs.scenario_payload` (§9).
Log `scenario_hash` instead — it's 64 chars, it pins the exact payload, and
`| logfmt | scenario_hash="abc…"` finds every run of that exact definition
across the fleet. If you need the payload while reading logs, the run-detail
endpoint is one click away via `debugging_urls`.

**Same Loki, separated by label.** `app="judge_it"` is the boundary. Two rules
follow, and the second one is easy to miss:

1. **Production dashboards must pin `app="osv-voice"`.** A query with no `app`
   filter silently picks up judge_it's test traffic. Audit the existing panels once.
2. **Tagging judge_it does not exclude test calls from production views.** The
   bot-side logs of an judge_it-driven call are emitted by OSV and tagged
   `app="osv-voice"` — they look exactly like a real customer call. Separating
   judge_it's own logs is easy; separating *test traffic* needs `is_test` to
   propagate into OSV's log fields, which is the same blocker as §11.1. Worth
   knowing before assuming a label solves it.

Joining still works when you want it: `{app=~"judge_it|osv-voice"} | logfmt |
interaction_uuid="…"`. Separated by default, joinable on request.

`client_uuid` is a judgement call — if you have <50 clients it *could* be a
label, but it will grow and you can't easily undo it. Keep it a field.

Extend the existing contextvars (`core/log_context.py` already has
`call_id_var` and `worker_id_var`) to that field set. Rename
`call_id_var` → `run_uuid_var`; it identifies a run, not a call. Match OSV's
names exactly where they overlap (`interaction_uuid_var`, `client_uuid_var` —
`slog.py:11-13`) so the same `| logfmt |` parse works on both streams.

#### Queries this buys you

```logql
# one conversation, both sides of the phone call
{app=~"judge_it|osv-voice", env="prod"} | logfmt | interaction_uuid="550e8400-…"

# every error in a campaign
{app="judge_it", component="worker", level="ERROR"} | logfmt
  | correlation_uuid="nightly-2026-08-04"

# one run, in order
{app="judge_it"} | logfmt | run_uuid="…" | line_format "{{.seq}} {{.msg}}"

# barge failures for one assistant across the fleet
{app="judge_it"} | logfmt | assistant_uuid="…" | msg=~"barge.*(missed|late)"
```

#### Three things files gave you for free — replace them deliberately

1. **Ordering.** A single file is totally ordered. Loki orders by timestamp
   per stream, and lines from the same run will arrive via several streams
   (api, worker, media pods) with sub-millisecond ties. **Emit a monotonic
   `seq` counter per run** and sort on it during triage. Cheap; without it,
   turn-by-turn reconstruction is guesswork.
2. **Crash tails.** `loguru`'s `enqueue=True` (which OSV uses) buffers — on a
   hard crash the last lines are lost, exactly the lines you want. Log
   terminal errors synchronously and flush before exit; register an
   `atexit`/signal flush.
3. **"Everything in one place."** This was judge_it's best triage feature and
   losing it silently would be a real regression. Replace it explicitly with
   a **run detail endpoint plus deeplinks** — see below.

#### What replaces the run folder

`GET /v1/runs/{run_uuid}` returns the assembled view from Postgres — turns,
verdict, resolved config, vendor usage, timings — plus a `debugging_urls`
block, mirroring OSV's `construct_debugging_urls`
(`app/llm/context_manager.py:1610-1625`):

```json
"debugging_urls": {
  "logs":        "https://grafana…/explore?…run_uuid%3D%22<uuid>%22",
  "trace":       "https://grafana…/explore?…traceID=<trace_id>",
  "dashboard":   "https://grafana…/d/judge_it-run/run?var_run_uuid=<uuid>",
  "interaction": "https://llm-studio.judge_it.dev/clients/<client>/conversations?interaction_uuid=<iuuid>",
  "audio":       "<presigned S3 URL>"
}
```

Put that block in the API response, in the webhook callback, and in the Slack
failure message. One click from "test failed" to the logs is the actual
requirement; a folder was just one way to meet it.

#### Log→trace pivot

Emit `trace_id` and `span_id` as fields on every line, then configure the Loki
datasource's **derived field** to link `trace_id` → Tempo. Grafana renders a
"View trace" button on every log line.

OSV doesn't do this yet — `slog.py` has no `trace_id`, though the OTel
plumbing exists (`metrics_observer.py` already imports `format_trace_id`).
Adding the field to both services is a few lines each and gives you
log→trace→log pivoting across the whole call path. Worth doing as a joint
change.

#### Volume control (this matters at 50 concurrent calls)

Loki ingest is metered and judge_it is chatty by nature.

- **Never log audio frames.** Not at DEBUG, not ever. Count them into a
  metric instead.
- **Per-component log levels** from config; `media` at INFO, `worker` at INFO,
  `reaper` at DEBUG is a reasonable default.
- **`profile: debug` raises the level for that run only** — carry it in the
  run config, not a global env var, so one bad scenario can be traced without
  a fleet-wide firehose.
- **Bulk debug data is not a log.** Raw frame dumps, full audio buffers, and
  complete LLM prompt bodies go to S3 as debug artifacts (§9), not into Loki.
  Log a reference.
- Set Loki per-stream rate limits and expect to tune
  `max_streams_per_user`.

### Metrics

None exist today. Mirror OSV's naming and **reuse
`app/instruments/metrics.py:5 LATENCY_BUCKETS` verbatim** so histograms are
directly comparable.

```python
# judge_it/instruments/metrics.py
judge_it_runs_total{client, assistant, assistant_version, scenario, outcome}
    # outcome: passed | failed | error | timed_out | cancelled | abandoned
judge_it_run_duration_seconds{client, assistant, scenario}
judge_it_active_runs{client, assistant}                    # Gauge, livesum
judge_it_queue_depth{priority}
judge_it_admission_rejections_total{reason}

# ── the money metric ──────────────────────────────────────────────────
judge_it_bot_turn_latency_seconds{client, assistant, timing_source}
# Deliberately mirrors voice_runtime_user_perceived_latency_seconds.
# Measured from the PSTN side, independently, by a different codebase.

judge_it_caller_vendor_latency_seconds{vendor_type, vendor_name}  # our STT/TTS/LLM
judge_it_vendor_errors_total{vendor_type, vendor_name, error_type, status_code}
judge_it_llm_tokens{token_type, model, role}               # role: caller | evaluator
judge_it_run_cost_usd{client, component}                   # component: telephony|stt|tts|llm

judge_it_barge_total{mode, outcome}                        # outcome: fired|missed|late
judge_it_dtmf_total{transport, outcome}
judge_it_evaluator_verdicts_total{passed, malformed}
judge_it_correlation_total{method}                         # §5
judge_it_twilio_call_status_total{status}
```

**`judge_it_bot_turn_latency_seconds` is a product feature, not just
instrumentation.** OSV measures user-perceived latency from *inside* its own
pipeline. judge_it measures it from the far end of the carrier. If OSV reports
p95 = 1.2 s and judge_it measures 2.1 s on the same interactions, you have found
carrier/transport/jitter-buffer overhead that OSV structurally cannot see. Put
both series on one Grafana panel joined on `assistant`; the gap between them
is a number nobody currently has.

Note the multiprocess gauge trap OSV already hit — the comment at
`app/instruments/metrics.py:205-213` explains why worker-only metrics must not
live in the shared module. Same rule applies once judge_it has api/worker/media
processes: set `PROMETHEUS_MULTIPROC_DIR`, call `mark_process_dead` on child
exit.

### Tracing — Tempo for the call path, Phoenix for every LLM payload

Port OSV's dual-export architecture wholesale. `app/lib/phoenix_utils.py` is
the file to copy: one global `TracerProvider`, two span processors — Phoenix
gets LLM/STT/TTS spans via `ServiceTypeSpanProcessor`, Tempo gets everything.
Full reference in `docs/observability/tracing.md`.

```
                    ┌──────────────────────────────┐
                    │   TracerProvider (global)     │
run span            │  ServiceTypeSpanProcessor ───┼──► Phoenix  (LLM payloads)
├── turn 1          │    (llm | stt | tts)         │    project: "judge_it"
│   ├── stt.final   │                              │
│   ├── caller_llm ─┼─ LLM span w/ full messages   │
│   ├── tts.ttfb    │  BatchSpanProcessor ─────────┼──► Tempo    (call path)
│   └── bot_turn    │    (all spans)               │
├── turn 2 …        └──────────────────────────────┘
└── evaluator ─────── LLM span w/ verdict
```

#### Match OSV's span attributes exactly

This is what makes the two systems queryable as one. OSV's root span carries
`client.name`, `assistant.name`, `conversation.id` (= the interaction uuid), and
`PhoenixTracer.start_conversation_tracing()` sets them. Use **the same
attribute names** on judge_it's root span:

```python
conversation.id     = interaction_uuid    # ← the join key, same as OSV
client.name         = client_name
assistant.name      = assistant_name
assistant.version   = assistant_version
judge_it.run_uuid     = run_uuid
judge_it.scenario     = scenario_name
judge_it.scenario_hash= scenario_hash       # §2
judge_it.profile      = profile
```

Then `conversation.id = "<uuid>"` in Phoenix returns **both** the bot's LLM
calls and the caller's LLM calls for the same conversation. That is the single
most useful debugging view this whole document produces: the bot's reasoning
and the adversary's reasoning, interleaved, for one call.

**Separate Phoenix project, same instance.** `PHOENIX_PROJECT_NAME=judge_it`
alongside OSV's `voice-runtime`. Same attribute names so a filter transfers
between them, but a hard boundary so judge_it's volume never lands in the
production project — a 500-call load test is ~15,000 LLM spans, which would drown
real traffic.

The cost, stated plainly: with separate projects, looking at both sides of one
call is **two lookups** (switch project, same `conversation.id`) instead of one
view. That's the right trade — polluting production's LLM-observability project
with test traffic is worse than an extra click.

#### Instrumentation depends on §8

OSV gets automatic LLM spans from `OpenAIInstrumentor`. Whether judge_it can do
the same depends entirely on the LLM decision in §8:

- **If every LLM call goes through the `openai` SDK** (the §8 recommendation —
  OpenAI schema as the universal interface, `base_url` per vendor), then
  `OpenAIInstrumentor` works unchanged and Phoenix spans are free. Prefer this.
- **Until then** — the caller and evaluator run on the native Gemini client
  (`llm/gemini.py`) — auto-instrumentation gets you nothing, so emit spans by
  hand at the LLM client boundary using OpenInference conventions. judge_it
  already captures exactly the right data: `llm_calls.json` records input,
  output, and latency for every caller and evaluator call. That capture point
  becomes the span emitter, and it stays useful afterwards for native-SDK
  escape hatches:

```python
from openinference.semconv.trace import SpanAttributes as SA

with tracer.start_as_current_span("caller_llm", attributes={
    SA.OPENINFERENCE_SPAN_KIND:      "LLM",
    SA.LLM_PROVIDER:                 "gemini",
    SA.LLM_MODEL_NAME:               model,
    SA.LLM_INVOCATION_PARAMETERS:    json.dumps({"temperature": temp}),
    SA.LLM_INPUT_MESSAGES:           messages,      # ← the payload you asked for
    SA.LLM_OUTPUT_MESSAGES:          response,
    SA.LLM_TOKEN_COUNT_PROMPT:       prompt_tokens,
    SA.LLM_TOKEN_COUNT_COMPLETION:   completion_tokens,
    "judge_it.role":                   "caller",      # caller | evaluator
    "judge_it.turn":                   turn,
}):
    ...
```

`LLM_INPUT_MESSAGES` / `LLM_OUTPUT_MESSAGES` are what render as inspectable
prompt/response payloads in the Phoenix UI. **One observer (§1) writes the
`run_llm_calls` row and emits this span** — same data, two destinations, no
duplicate capture logic.

#### Why the evaluator in Phoenix matters more than the caller

Every pass/fail verdict judge_it produces comes from an LLM call whose prompt is
a 100-line rubric. When a verdict looks wrong, you need the exact rubric, the
exact transcript, and the exact response — which is precisely a Phoenix span.

Better: Phoenix has **datasets and experiments**. That's the off-the-shelf
implementation of the evaluator validation harness in §10.2 — curate a dataset
of hand-labelled transcripts, run the evaluator prompt against it as a Phoenix
experiment, and get precision/recall per prompt revision without building any
of it. If you adopt one Phoenix feature beyond span viewing, make it this one.

#### Trace context propagation

- **Accept `traceparent` from the API caller** so a run nests inside their CI
  build or campaign trace.
- **Inject `traceparent` toward the bot** where the transport allows — SIP
  header or direct mode (§5). Then OSV's pipeline spans become children of
  judge_it's turn span: **one trace spanning both sides of the phone call**.
  Unblocked as of 2026-08-11 for `call_transport=sip`: arbitrary `X-` headers
  demonstrably survive to the bot (§5a), so this is now just
  `JUDGE_IT_SIP_EXTRA_HEADERS`-shaped work plus an OSV read of the attribute.
  Still impossible over raw PSTN.
- **`trace_id` on every log line** (see Logging above) so Loki → Tempo →
  Phoenix is a two-click pivot.
- **Store all three URLs** in `debugging_urls`, following
  `context_manager.py:2121-2128`:
  `{PHOENIX_BASE_URL}/projects/{PHOENIX_PROJECT_ID}/spans/{trace_id}`.
- **Sentry** — OSV tags `interaction_uuid`, `client_uuid`, `assistant_id`
  (`context_manager.py:1610`). Match the tag names.

#### Two divergences from OSV's config

- **`BatchSpanProcessor` for Phoenix too.** OSV uses `SimpleSpanProcessor` on
  the Phoenix exporter, which exports synchronously per span. At load-test
  concurrency (25–50 calls, each with 30 LLM spans) that's a lot of blocking
  HTTP on the hot path. Make it a knob —
  `JUDGE_IT_PHOENIX_SPAN_PROCESSOR=simple|batch`, batch for load profiles, and
  flush on shutdown so nothing is lost during drain (§7.4).
- **Sampling.** Trace 100% of `profile: regression` runs; sample
  `profile: load-test` at 1–5%. A 500-call load test does not need 15,000 LLM
  spans in Phoenix, and the latency numbers you care about there are in
  Prometheus anyway.

**Redact before the span, not after.** Span attributes carrying
`LLM_INPUT_MESSAGES` contain the full persona — including the DOB/SSN/ZIP in
`vars`. Phoenix is a third destination for the same PII problem (§11.2), and
spans are as un-deletable as log lines.

### Alerts worth having on day one

```
judge_it_runs_total{outcome="error"} rate > 10%          → harness is broken
judge_it_correlation_total{method="unresolved"} rising   → §5 mechanism degraded
judge_it_active_runs > 0 for > 15min with no state change → stuck run, reaper failing
judge_it_run_cost_usd sum over 1h > budget               → runaway
judge_it_bot_turn_latency p95 − osv p95 > 500ms          → carrier-path regression
```

---

## 7. Decision: what "runs for a long time" actually requires

You flagged "run in long time so we have to handle things changing and normal
run." Here's the full list. Items 1–4 are load-bearing; the rest are
hardening.

### 1. Process model — the biggest change

Today: one process holding runner + FastAPI-in-a-thread + in-memory
`_sessions`. That design forced the `completion_loop` /
`call_soon_threadsafe` workaround and the duplicated contextvar set, and it
can't survive a restart or scale past one pod.

```
┌──────────┐   enqueue    ┌────────┐    ┌──────────────┐
│ api pod  │ ───────────► │ broker │ ─► │  worker pods │
│(stateless)│              │celery/ │    │ (run calls)  │
└──────────┘              │  SQS   │    └──────┬───────┘
      │                   └────────┘           │
      │  read state                            │ register session
      ▼                                        ▼
┌──────────────────────┐              ┌─────────────────┐
│ Postgres (runs,      │              │ Redis (session  │
│ campaigns, artifacts)│              │ registry, TTL)  │
└──────────────────────┘              └─────────────────┘
                                               ▲
┌──────────────┐   twiml/ws lookup             │
│  media pod   │ ──────────────────────────────┘
│ (public URL) │
└──────────────┘
```

`_sessions` moves to Redis keyed by `run_uuid` with a TTL, so the Twilio
webhook can land on any media pod. Use Celery + Redis to match OSV, or SQS if
you want OSV's post-call durability semantics.

*Cheaper interim:* keep one process but move `_sessions` to Redis with a TTL
anyway. That alone fixes the restart-loses-everything problem and the
unbounded-dict leak, and it's a small diff.

### 2. Explicit run state machine, persisted

```
queued → admitted → dialing → in_call → evaluating → completed
                       │         │          │
                       └─────────┴──────────┴──→ failed | timed_out
                                                 cancelled | abandoned
```

Persist every transition with a timestamp. This is what makes the reaper
possible and what makes "why is this run stuck" answerable.

### 3. Reaper / reconciler — non-negotiable

Direct precedent: `main.py --mode post-call-reconciler` →
`app/background/post_call/reconciler.py`.

A run stuck in `dialing`/`in_call` past its deadline means, in the worst case,
**a live Twilio call nobody is going to hang up**, billing by the minute,
possibly holding a production bot's concurrency slot. The reaper:

1. finds runs past deadline for their state,
2. asks Twilio for the real call status,
3. hangs up anything still live,
4. finalises the run as `abandoned` with whatever artifacts exist,
5. attempts `interaction_uuid` reconciliation (§5b),
6. emits `judge_it_reaped_total{state}`.

Run it every 60 s. This is the difference between "runs for a weekend" and
"runs for a quarter."

### 4. Graceful shutdown and drain

Calls last minutes; deploys happen. Without this, every deploy truncates
in-flight calls and produces false failures.

- SIGTERM ⇒ stop accepting, finish in-flight, exit when drained.
- k8s `terminationGracePeriodSeconds` **must exceed max call duration** (with
  `max_turns: 30` that's several minutes — set it explicitly, don't inherit
  the 30 s default).
- `preStop` hook to deregister from the LB before the grace period starts.
- Flush artifacts and mark state on the way out; if the deadline hits, mark
  `abandoned` rather than leaving `in_call`.
- `/readyz` fails during drain; `/healthz` stays green.

### 5. Leak inventory to close

| Leak | Where | Fix |
|---|---|---|
| Unbounded session dict | `transport/server.py:22` | Redis + TTL |
| Per-run `FileHandler` on shared logger | `logwriter.py` | delete file logging entirely (§6) |
| Orphaned Twilio calls | crash between `make_call` and `finally` | reaper (§4) |
| `logs/` and WAVs grow forever | `logwriter.py` | no pod-local files; S3 + lifecycle (§9) |
| Per-scenario `DeepgramTTS` instances | `runner.py:281-286` | pooled connections, closed on drain |
| Silero/PyTorch per process | `config.py:31` | load once at worker start, share |
| SQLite single write lock | `store.py` | Postgres (§9) |

### 6. Backpressure and vendor limits

Three independent ceilings: Twilio channel concurrency, Deepgram concurrent
connections, Gemini RPM/TPM. Today a 25-way campaign just starts failing
opaquely. Add per-vendor token buckets, retry with jitter, and a circuit
breaker that fails runs *fast and labelled* (`outcome="error"`,
`error_type="vendor_saturated"`) rather than letting them time out at 20
minutes each.

### 7. Cost controls

judge_it spends real money per run: telephony minutes, STT/TTS seconds, LLM
tokens. A scenario that loops (bot keeps re-asking, caller keeps answering)
burns until `max_turns` — and a *campaign* of those multiplies it.

- Per-run and per-campaign budget caps in the config (minutes, tokens, USD).
- Hard kill at cap, `outcome="budget_exceeded"`.
- `judge_it_run_cost_usd{component}` and a daily-spend alert.
- A global kill switch that drains and refuses new runs.

### 8. Config and code changing under a long-lived fleet

- **Pin the scenario.** `runs.scenario_payload` + `scenario_hash` (§2). A run
  must stay interpretable after the registry entry is superseded.
- **Pin the assistant version.** Record `assistant_uuid` + `version`. A pass
  rate without a version is not a measurement.
- **Version the API and the artifact schema.** `/v1`, and
  `schema_version` on the run row, so old runs stay readable after the shape
  changes.
- **Reload config without restarting** — or explicitly decide not to and
  document that config changes require a rolling restart. Either is fine;
  ambiguity isn't.
- **Alembic from the start** on Postgres. Retrofitting migrations onto a live
  results table is miserable.

### 9. Scheduling

OSV has `cronjobs/`. Nightly regression per client × assistant-version, plus a
continuous low-rate latency probe (`profile: latency-probe`, 4 turns, no
evaluator) as a canary. The canary is what tells you the *bot* broke at 3 a.m.
rather than the suite telling you at 9 a.m.

---

## 8. Decision: vendor plugin architecture (STT / TTS / LLM / language / transport)

Today judge_it is hardwired to **Deepgram + Twilio + Gemini**. `stream.py` calls
`DeepgramTTS` directly; STT is a Deepgram WS URL built inline; the only
abstraction that exists is `llm/base.py`'s `LLMClient` Protocol (44 lines —
and it's the right shape, so extend that pattern outward).

### The gap that matters most

OSV supports **sarvam**, **magpie**, and **parakeet** — those exist in
`app/runtime/voice/services/` specifically because Indic languages needed
vendors beyond the Western defaults. judge_it supports Deepgram only.

**So judge_it structurally cannot properly test the India-region bots.** A
Hindi or Hinglish scenario would be transcribed by a vendor that isn't what
production uses, which means a "failure" could be judge_it's STT mishearing the
bot rather than the bot misbehaving. That's a false-signal generator, and
it's the strongest single argument for doing this work.

### Five protocols

```python
# judge_it/services/protocols.py
class STTService(Protocol):
    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[TranscriptEvent]: ...
class TTSService(Protocol):
    async def synthesize(self, text: str, *, language: Language) -> AsyncIterator[bytes]: ...
class LLMService(Protocol):          # exists — llm/base.py, but see below:
    ...                              # replaced wholesale by the OpenAI schema
class Transport(Protocol):           # twilio | livekit-sip | direct-grpc | text
    async def place_call(self, target: str, run_uuid: str, headers: dict) -> CallHandle: ...
    async def hang_up(self, handle: CallHandle) -> None: ...
class TurnDetector(Protocol):        # silero | deepgram-utterance-end | vendor-vad
    def on_audio(self, frame: AudioFrame) -> TurnEndFrame | None: ...
```

Note `Transport.place_call(headers=…)` — that's the SIP-header hook from §5a,
and it's why transport belongs in this abstraction rather than staying
Twilio-shaped.

### LLM: the OpenAI schema *is* the protocol

Yes — do exactly what OSV does, and the evidence for it is stronger than a
convention. In `factory.py` the LLM registry has nine providers and
`create_from_config`'s return annotation is literally `OpenAILLMService`. The
OpenAI Chat Completions shape is the universal interface; native SDKs are the
exception.

Look at what onboarding a vendor costs there:

| File | Lines | What it does |
|---|---|---|
| `services/openai.py` → `ModOpenAILLMService` | 759 | the one normalisation layer |
| `services/cerebras.py` | **34** | `base_url` + key + default temperature |
| `services/openrouter.py` | **90** | same, plus header quirks |
| `services/groq.py` | 177 | same, plus reasoning-trace frames |
| `services/gemini.py` → `ModGoogleLLMOpenAIService(ModOpenAILLMService)` | — | **Gemini via the OpenAI path**, registered as `"GOOGLE"` |

That 759-vs-34 ratio is the whole argument: **pay once for normalisation, then
every OpenAI-compatible vendor is ~30 lines — or, for judge_it, zero lines.**

#### What judge_it should do

Replace `llm/base.py`'s bespoke `LLMClient` Protocol and `llm/gemini.py`'s
native client with a single `openai.AsyncOpenAI` client whose `base_url` comes
from the payload. A vendor then stops being code:

```sql
-- one providers row per endpoint (see "Where providers live" below)
INSERT INTO providers (provider_key, vendor, service_type, base_url) VALUES
  ('groq-llm',   'groq',   'llm', 'https://api.groq.com/openai/v1'),
  ('openai-llm', 'openai', 'llm', 'https://api.openai.com/v1');
```

```json
"llm": {
  "caller":    { "provider_key": "groq-llm", "model": "llama-3.3-70b-versatile",
                 "temperature": 0.3, "max_tokens": 512 },
  "evaluator": { "provider_key": "openai-llm", "model": "gpt-4o", "temperature": 0.0,
                 "response_format": { "type": "json_schema" } }
}
```

Adding Cerebras, Together, Fireworks, vLLM, or a self-hosted model is **one row
plus one secret** — no code, no release. Neither the `base_url` nor the credential
comes from the caller.

Note the inversion: **`base_url` is the dispatch key; `vendor` is just the
Secrets Manager lookup key plus a metric label.** Nothing dispatches on model
name.

#### This kills `detect_provider`

`runner.py:504-534` (`_api_key_for`) maps model prefix → provider → API key
slot, plus a mismatch check that raises when `provider="gemini"` meets an
Anthropic model name. `llm/__init__.py`'s `detect_provider` does the sniffing.

All of that exists **only because provider and credentials are inferred from
the model string**. With `base_url` on the `providers` row and the key resolved
from Secrets Manager by `(vendor, "llm")`, inference disappears and so does the
entire mismatch error path — a new model name never needs a code change to be
routable.

#### Gemini: use the compat endpoint, keep a native escape hatch

Google exposes an OpenAI-compatible endpoint —
`https://generativelanguage.googleapis.com/v1beta/openai/` — and OSV already
routes Google through it (`ModGoogleLLMOpenAIService`). So `llm/gemini.py` (90
lines) can go.

**Verify before deleting it**, because the compat layer is historically lossy
in exactly the places judge_it depends on:

- `response_format` with a strict JSON schema — the evaluator needs this.
- Thinking/reasoning budget for `gemini-2.5-pro`, which may only be reachable
  via `extra_body`.
- Token accounting on streamed responses.

OSV's answer is the pattern to copy: it registers **both** `GOOGLE` (compat)
and `GOOGLE_VERTEX` (native) and picks per assistant. Do the same —
OpenAI-compat as the default path, a native-SDK vendor registered alongside as
the escape hatch when compat can't express what a run needs.

#### Structured output belongs in the capability manifest

This matters more for judge_it than for OSV, because the evaluator verdict *is*
the product. Today malformed JSON is a known, handled failure —
`EvaluationResult.malformed`, a two-attempt retry, and a 90-second timeout
(`runner.py:465-502`).

`response_format: {"type": "json_schema", ..., "strict": true}` mostly deletes
that failure mode — **on vendors that support it.** Support varies widely, so
it's a declared capability:

```python
@dataclass(frozen=True)
class LLMCapabilities:
    structured_output: Literal["none", "json_object", "json_schema"]
    streaming:         bool
    tools:             bool
    usage_on_stream:   bool     # needs stream_options={"include_usage": True}
    max_context:       int
```

And a rule worth enforcing: **the evaluator refuses to run on a vendor
declaring `structured_output: "none"`** unless the payload explicitly opts in.
A judge that can't be forced into a schema is not one you should gate deploys
on. The caller role has no such constraint — it produces prose.

#### Stream the caller, or the bot will hang up on you

Stream the caller LLM (`stream=True`, SSE deltas — uniform across
OpenAI-compatible vendors) and pipe first tokens into TTS.

This isn't about judge_it's own latency looking good. OSV's pipeline runs
`processors/user_idle.py`, which **ends the call if the user is idle too long**.
A caller LLM taking 4 s to think reads to the bot as a silent human — so a slow
caller can trip the bot's idle handler and produce a *false failure* that looks
like a bot bug. Caller-side think time is a realism control, and streaming is
how you keep it under the bot's idle threshold.

#### Where OpenAI-compat leaks

Be realistic about why OSV's base class is 759 lines. The raw SDK is not
enough; you still need one normalisation layer for:

- `usage` missing on streamed responses unless
  `stream_options={"include_usage": true}` — and some vendors ignore it, which
  silently breaks `judge_it_llm_tokens` and cost accounting.
- `max_tokens` vs `max_completion_tokens`.
- Error envelope shapes and status codes (drives `judge_it_vendor_errors_total`
  labels and the circuit breaker).
- Rate-limit headers — different names per vendor, needed for backpressure
  (§7.6).
- `temperature` ranges, and whether `top_p` is honoured at all.
- Tool-call delta framing during streaming.

Write that once, in `judge_it/services/llm/openai_compat.py`, and keep
vendor-specific subclasses for genuine deviations only.

#### Knock-on: Phoenix auto-instrumentation comes back

This reverses the caveat in §6. If every LLM call goes through the `openai`
SDK, **`OpenAIInstrumentor` works for judge_it too** — the same
auto-instrumentation OSV uses, no hand-emitted spans, LLM payloads in Phoenix
for free across every vendor. The manual OpenInference span path stays only for
native-SDK escape hatches.

Sequencing note: that makes this LLM work a *prerequisite* for the cheap version
of §6's Phoenix work — but the OpenAI swap sits in step 3 and Phoenix in step 1.
Either pull the swap forward, or hand-emit OpenInference spans in step 1 and
simplify once step 3 lands. The second is fine; the spans are ~30 lines.

#### Onboarding a new LLM vendor, end to end

1. Store the key: `judge_it/<vendor>-llm` in AWS Secrets Manager.
2. Insert one `providers` row: `{provider_key, vendor, service_type:'llm', base_url}`.
3. `judge_it vendors check` — confirms the key resolves and that streaming and
   structured output actually work; writes `capabilities` and `verified_at`.
4. Run one scenario with `profile: latency-probe`.

No code for an OpenAI-compatible vendor. A subclass only when it deviates.

### Registry + out-of-tree plugins

Follow `ServiceFactory._class_registry[service_type][service_name]`
(`app/runtime/voice/services/factory.py:52`), but add entry-point discovery so
a vendor can be added **without forking judge_it**:

```python
# judge_it/services/registry.py
@register("tts", "deepgram", capabilities=DEEPGRAM_TTS_CAPS)
class DeepgramTTS: ...

# third-party package, no judge_it change required:
# pyproject.toml → [project.entry-points."judge_it.services"]
#                  acme_tts = "acme_judge_it:AcmeTTS"
```

That's what "custom vendor support" should mean concretely: a client-specific
or experimental vendor lives in its own package, and judge_it discovers it.

### Capability manifests — the part people skip

Every vendor declares what it can actually do, and config validation checks
the scenario against it **before dialing**:

```python
@dataclass(frozen=True)
class TTSCapabilities:
    languages:      frozenset[str]       # {"en-US", "hi-IN", "es-MX", …}
    voices:         Mapping[str, list[str]]   # language → voice ids
    sample_rates:   frozenset[int]
    streaming:      bool                 # can we get TTFB, or only full body?
    ssml:           bool
    max_chars:      int

@dataclass(frozen=True)
class STTCapabilities:
    languages:        frozenset[str]
    interim_results:  bool               # needed for barge timing
    word_timings:     bool               # needed for accurate latency attribution
    utterance_end:    bool               # needed for turn detection without Silero
    code_switching:   bool               # Hinglish
    sample_rates:     frozenset[int]
```

Then `judge_it lint` fails fast with a real message — *"scenario requests
`language: hi-IN` with `stt.name: deepgram`, which does not declare
`code_switching`; use `sarvam` or set `strict_language: false`"* — instead of
producing a garbage transcript and a mystifying evaluator verdict two minutes
and one phone call later. Validating vendor capability at lint time is worth
more here than in OSV, because judge_it's output is a *measurement*.

### Language as a first-class axis

OSV already has the resolution logic worth copying:
`filter_configs_by_language` (`factory.py:771`) and
`group_languages_by_vendor_and_model` (`factory.py:779`).

```json
"language": "hi-IN",
"secondary_languages": ["en-IN"],
"vendors": {
  "stt": { "provider_key": "sarvam-asr-hi-IN", "model": "saarika:v2" },
  "tts": { "provider_key": "magpie-tts-hi-IN", "voice": "female-1" },
  "llm": { "caller": { "provider_key": "google-llm", "model": "gemini-2.5-flash" } }
}
```

Plus the harder cases judge_it specifically needs:

- **Code-switching / Hinglish.** `language: hi-IN` with
  `secondary_languages: [en-IN]`. The caller persona should be able to switch
  mid-sentence, because real users do and that's a bug class worth probing.
- **Per-direction language.** The bot may speak Hindi while the caller's
  STT config differs. Allow `stt.language` and `tts.language` to diverge.
- **Mid-call language switch.** If the bot supports "press 2 for Hindi", the
  scenario needs to switch the caller's TTS/STT mid-run. That's a processor
  reacting to a `LanguageSwitchFrame` — clean under §1, near-impossible in
  today's `stream.py`.
- **Accent/voice variation as a test axis.** `voices: [female-1, male-2,
  elderly-1]` expands one scenario into N runs. Cheap coverage for ASR
  robustness on the bot side.

### Where providers live: OSV owns the catalog, judge_it resolves and runs

OSV already has the catalog and the UI — `RuntimeProvider`
(`app/models/assistant.py:280`) plus `app/interface/views/runtime_providers.py`
(CRUD, voices, language defaults, quota limits, `test-provider-api`, and
credential registration). **JudgeIt builds none of that.**

Worth noting why it's reusable: `RuntimeProvider.config` isn't a blob, it's a list
of field descriptors — `{name, type, default_value, description, range, options}`
(`runtime_providers.py:24`). `options` ⇒ a select, `range` ⇒ a slider, `type`
picks the control. The UI renders each vendor's knobs from data, so a new vendor
needs no frontend release.

```
user in llm-studio
      │ picks sarvam / saarika:v2 / hi-IN from OSV's dropdowns
      ▼
OSV interface ── POST /v1/runs { provider_key, model, config, … }
      ▼
judge_it API ── resolves at submit:
      │        providers table  → base_url, vendor, capabilities
      │        Secrets Manager  → api_key
      ├─► queue ─► worker ─► pipeline ─► places the call
      ▼
OSV interface ── GET /v1/runs/{uuid} → status, verdict, debugging_urls
```

JudgeIt is a pure server: accepts submissions, serves status, makes no outbound
call to OSV's control plane.

#### What judge_it keeps

One table, migration-seeded, ~20-40 rows:

```sql
CREATE TABLE providers (
    provider_key   TEXT PRIMARY KEY,        -- "deepgram-asr-en-US"
    vendor         TEXT NOT NULL,           -- Secrets Manager lookup key
    service_type   TEXT NOT NULL,           -- asr | tts | llm
    language       TEXT,
    base_url       TEXT NOT NULL,
    allowed_models TEXT[],
    capabilities   JSONB NOT NULL DEFAULT '{}',  -- manifests above; drives lint
    verified_at    TIMESTAMPTZ,             -- last empirical `vendors check`
    quota_limit    INT,   -- feeds admission control (§4), NOT vendor fallback
    quota_isolated BOOLEAN NOT NULL DEFAULT FALSE,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE
);
```

Config here, secrets in Secrets Manager, nothing else. No catalog copy, no
voices, no field descriptors, no CRUD, no admin screens — `provider_key` in,
`base_url` out.

Note `quota_limit`: OSV uses its equivalent to *fall back to another vendor* under
load. JudgeIt must not (see "Determinism" below) — for judge_it it caps admission.

**`transport` and `turn_detector` aren't providers** and get no rows. They're
payload enums validated by judge_it's Pydantic schema, and the UI renders those
controls from `GET /openapi.json` (§2) — which is part of why publishing the
schema matters.

#### Three things to get right

1. **Resolve at submit, not at dial.** A bad `provider_key`, a disabled provider,
   or a missing credential must fail at admission (§4), not three turns into a
   phone call. Freeze the resolved values into `runs.scenario_payload` so the run
   stays interpretable after the row changes (§2) — `provider_key` is an input,
   never the sole record of what ran, and the key is never written into the
   payload.
2. **Keep `provider_key` values aligned.** A key OSV's dropdown can offer but
   judge_it has no row for means the UI presents a choice that fails at admission. A
   CI check diffing `GET /providers/` against judge_it's rows covers it, provided
   OSV's naming is stable and derivable (`{vendor}-{type}-{language}`).
3. **A bot that won't answer is a test result, not a startup error.** If OSV's
   voice runtime is down, record `outcome="failed"` and report it. The
   `latency-probe` canary (§7.9) depends on this — refusing to run when the bot is
   unreachable gives you silence at 3 a.m. instead of an alert. Relatedly:
   scheduled runs need a submitter that isn't the UI, which is why the scenario
   registry (§2) still earns its place.

#### JudgeIt validates OSV's catalog

If OSV's catalog claims `deepgram nova-2` supports `hi-IN` and it doesn't, judge_it
receives that config and reports a **false bot failure**. It can't prevent that on
the run path, but `judge_it vendors check` can verify capabilities *empirically*
offline and flag disagreements. A mismatch is a finding, not noise — OSV's
production config asserts something untrue about a vendor. Emit
`judge_it_catalog_mismatch_total{vendor, capability}`.

### Credentials: Secrets Manager, resolved never supplied

Port OSV's `app/session/secrets_manager.py` + `app/runtime/voice/utils/service_keys.py`
— AWS Secrets Manager with a Redis read-through cache.

```
secret name:  judge_it/{vendor}-{service_type}
              judge_it/deepgram-stt   judge_it/groq-llm   judge_it/twilio-telephony

resolution:   Redis cache (1h TTL) → AWS Secrets Manager → env var (DEV only)
```

**No client dimension.** JudgeIt is internal tooling: one set of vendor accounts,
used for every run whatever client's bot is under test. That drops OSV's
`get_client_service_keys` override chain, `supports_client_keys`, and per-client
cost attribution.

**No credential in the payload, and no pointer to one either.** The path derives
from `(vendor, service_type)`, and `vendor` comes from judge_it's own `providers`
row — not the request. Nothing to spoof. Enforce it: reject any payload field
matching `api_key` / `token` / `secret` / `password` / `authorization` with a 422.
New keys get registered through an admin endpoint or IaC, never a run payload.

#### The inventory

| Secret | Shape | Contents |
|---|---|---|
| `judge_it/twilio-telephony` | dict | `account_sid`, `auth_token`, `phone_number` |
| `judge_it/deepgram-stt`, `-tts` | str | API key |
| `judge_it/sarvam-stt`, `judge_it/magpie-tts` | str | API key |
| `judge_it/google-llm`, `openai-llm`, `groq-llm` | str | API key |
| `judge_it/livekit-transport` | dict | `api_key`, `api_secret` |
| `judge_it/phoenix-obs` | str | `PHOENIX_API_KEY` (§6) |

Store values as JSON objects even for single-key secrets — OSV's `get_key` always
returns a dict and `service_keys` extracts `next(iter(secret.values()))`. Keeping
that uniform is what makes the multi-resource pick work.

#### Storing under `judge_it/*` is not the same as isolating

The one real hazard. JudgeIt's caller-side STT/TTS/LLM are *judge_it's* usage, not
the bot's — so if `judge_it/deepgram-stt` holds the same key **value** as
production's, a 50-call load test consumes production's concurrency and can
degrade real customer calls.

```
judge_it/deepgram-stt               ─┐
                                   ├─► same project ─► ONE concurrency pool
orchestrator/default/deepgram-stt ─┘
```

The check: **diff the key values.** Identical ⇒ shared quota, whatever the paths
say. The quota boundary differs per vendor (GCP project for Gemini, project for
Deepgram, account/subaccount for Twilio — verify that one first, it's the least
obvious and the most damaging to get wrong).

#### Keep these OSV behaviours; change one

- **Multi-resource secrets with random pick** (`_pick_resource`) — one secret
  holds several keys, each read picks one, spreading load within the cache TTL.
  Directly what load tests need.
- **Multi-key services return a dict** — Twilio and LiveKit above.
- **Log the source, never the value**: `"[service_keys] judge_it/groq-llm: resolved
  from Secrets Manager"`. Add key-shaped strings to the redaction processors
  (§11.2) as a backstop.
- **`invalidate_service_keys()`** for rotation — 1h TTL means rotation lands
  within the hour; invalidate for immediate, then `judge_it vendors check`.
- **Changed: no env fallback in production.** OSV's is convenient locally but
  means a missing secret silently resolves to whatever is in the environment.
  Gate on `ENVIRONMENT=DEV`; in prod a missing secret is a hard failure.

#### Fail closed at admission

Resolve every credential the payload needs during validation, alongside the
provider lookup — not three turns into a call when TTS 401s. Emit
`judge_it_secret_resolution_total{source, vendor}` with source in
`cache | secrets_manager | env_fallback | not_found`; a non-zero `env_fallback` or
`not_found` rate in prod is the alert that catches an expired key before the
nightly suite reports 40 failures.


### Determinism: no silent fallback

Important divergence from OSV. OSV wants vendor fallback for *resilience* — if
Cartesia fails, use ElevenLabs, keep the call alive. **judge_it is a measuring
instrument, and an instrument that silently changes its own calibration is
broken.** If the configured TTS fails and judge_it quietly swaps vendors, the
latency numbers and the transcript are no longer comparable to yesterday's,
and nobody will know why the graph moved.

So:

- Fallback is **off by default**, opt-in per run.
- The **vendor actually used** is recorded per run (and per turn if it
  changed) in a `run_vendor_usage` row (§9) and stamped on every metric via
  the `vendor_name` label.
- A run that fell back is marked `degraded=true` and **excluded from latency
  baselines** by default.
- If fallback is off and the vendor fails: fail the run fast with
  `error_type="vendor_unavailable"`. A failed run is honest; a silently
  re-calibrated one isn't.

### Operational bits

- **Per-vendor rate limits and circuit breakers** (§7.6) keyed by vendor, not
  global. Deepgram saturating shouldn't fail Sarvam runs.
- **`judge_it vendors check`** — a CLI/endpoint that validates every configured
  vendor's credentials, lists supported languages/voices, and runs a
  ~2-second synthesise-then-transcribe loopback. Run it in CI and after every
  credential rotation. This catches "the nightly suite has been failing for
  three days because a key expired" on day zero.
- **Generated parity matrix** — `judge_it vendors matrix --format md` emits the
  vendor × language × capability table from the manifests. Docs that can't
  drift.
- **Metrics already account for this**: `judge_it_caller_vendor_latency_seconds`
  and `judge_it_vendor_errors_total` (§6) carry `vendor_type` / `vendor_name`,
  matching OSV's `voice_runtime_vendor_latency_seconds` labels — so
  "is Deepgram slow today" is one query across both services.

---

## 9. Decision: storage — Postgres, and no local files at all

SQLite goes away entirely (`persistence/store.py`, `judge_it.db` — 12 MB and
already serialising all writes behind one asyncio lock). So does the per-run
folder. Three destinations, chosen by data shape:

| Data | Destination | Why |
|---|---|---|
| Runs, state transitions, verdicts, turns, resolved config, vendor usage, cost | **Postgres** | queryable, joinable, small |
| Log lines and per-turn events | **Loki** (§6) | time-series text, already there |
| Audio WAVs, raw frame dumps, oversized prompt bodies | **S3** | binary/bulk; nothing else can hold it |
| Traces | **Tempo** | already there |

**Nothing on pod disk** beyond an ephemeral scratch buffer during an active
call. Audio has to be buffered somewhere while the call runs — use
`/tmp`/`tmpfs`, upload on finalise, delete. `emptyDir` with a size limit, so a
stuck run can't fill a node.

### Schema sketch

Match OSV's stack — **async SQLAlchemy + Alembic** — so models, session
handling, and migration habits transfer from `app/models/` and
`app/session/db`.

```sql
-- identity, correlation, outcome
CREATE TABLE runs (
    run_uuid            UUID PRIMARY KEY,
    correlation_uuid    UUID,                         -- caller's batch/job id
    interaction_uuid    UUID,                         -- OSV side, backfillable (§5)
    correlation_method  TEXT,                         -- supplied|sip_header|direct|reconciled|unresolved
    idempotency_key     TEXT UNIQUE,                  -- §4

    client_uuid         UUID NOT NULL,
    assistant_uuid      UUID,
    assistant_version   INT,
    region              TEXT NOT NULL,

    schema_version      INT NOT NULL,                 -- payload contract version (§2)
    scenario_id         BIGINT REFERENCES scenarios(id),  -- NULL for inline payloads
    scenario_name       TEXT,
    scenario_payload    JSONB NOT NULL,               -- the resolved payload actually run (§2)
    scenario_hash       TEXT NOT NULL,                -- sha256(canonical_json(payload))
    profile             TEXT,
    resolved_config     JSONB NOT NULL,               -- was resolved_config.json
    language            TEXT,

    state               TEXT NOT NULL,                -- §7.2 state machine
    outcome             TEXT,                         -- passed|failed|error|timed_out|…
    degraded            BOOLEAN NOT NULL DEFAULT FALSE, -- vendor fallback fired (§8)
    error_type          TEXT,

    vendor_call_id      TEXT,                         -- Twilio CallSid — join key for §5b
    trace_id            TEXT,                         -- pivot to Tempo
    labels              JSONB NOT NULL DEFAULT '{}',  -- {suite, git_sha, …}

    queued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ NOT NULL                  -- the reaper reads this (§7.3)
);

CREATE INDEX ON runs (interaction_uuid);
CREATE INDEX ON runs (client_uuid, assistant_uuid, assistant_version, created_at DESC);
CREATE INDEX ON runs (state) WHERE state IN ('queued','dialing','in_call','evaluating');  -- reaper
CREATE INDEX ON runs USING GIN (labels);

CREATE TABLE run_events (          -- state machine audit trail (§7.2)
    run_uuid UUID REFERENCES runs, seq INT, at TIMESTAMPTZ,
    from_state TEXT, to_state TEXT, detail JSONB,
    PRIMARY KEY (run_uuid, seq)
);

CREATE TABLE run_turns (           -- was transcript.json
    run_uuid UUID REFERENCES runs, turn INT, speaker TEXT, text TEXT,
    started_ms REAL, ended_ms REAL,
    bot_turn_latency_ms REAL,      -- the outside-in latency measurement (§6)
    barge BOOLEAN, dtmf TEXT, language TEXT,
    PRIMARY KEY (run_uuid, turn)
);

CREATE TABLE run_evaluation (       -- was evaluation.json
    run_uuid UUID PRIMARY KEY REFERENCES runs,
    overall_passed BOOLEAN, overall_reasoning TEXT,
    criteria JSONB, malformed BOOLEAN, evaluator_model TEXT, latency_ms REAL
);

CREATE TABLE run_llm_calls (        -- was llm_calls.json
    run_uuid UUID REFERENCES runs, seq INT,
    role TEXT,                      -- caller | evaluator
    model TEXT, vendor TEXT,
    prompt_id BIGINT REFERENCES prompts(id),   -- deduped, see below
    response TEXT, prompt_tokens INT, completion_tokens INT, latency_ms REAL,
    PRIMARY KEY (run_uuid, seq)
);

CREATE TABLE prompts (              -- content-addressed prompt store
    id BIGSERIAL PRIMARY KEY,
    sha256 TEXT UNIQUE NOT NULL,
    body TEXT,                      -- NULL when offloaded to S3
    s3_key TEXT, byte_len INT
);

CREATE TABLE run_vendor_usage (     -- what was actually used, and what it cost (§8)
    run_uuid UUID REFERENCES runs,
    vendor_type TEXT, vendor_name TEXT, model TEXT,
    units REAL, unit TEXT,          -- seconds | chars | tokens | minutes
    cost_usd NUMERIC(10,6), errors INT, fell_back_from TEXT,
    PRIMARY KEY (run_uuid, vendor_type, vendor_name)
);

CREATE TABLE artifacts (            -- pointers only; bytes live in S3
    run_uuid UUID REFERENCES runs, kind TEXT,   -- bot_audio|caller_audio|raw_frames
    s3_key TEXT NOT NULL, byte_len BIGINT, expires_at TIMESTAMPTZ,
    PRIMARY KEY (run_uuid, kind)
);

CREATE TABLE scenarios (            -- payload registry (§2)
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL, version INT NOT NULL,   -- immutable per version
    suite TEXT, client_uuid UUID, description TEXT,
    schema_version INT NOT NULL,
    payload JSONB NOT NULL,                     -- the §2 payload, not YAML
    sha256 TEXT NOT NULL,
    created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);
CREATE INDEX ON scenarios (suite, client_uuid);
CREATE INDEX ON scenarios USING GIN (payload);   -- "which scenarios use elevenlabs?"

CREATE TABLE profiles (             -- named presets (§3)
    name TEXT PRIMARY KEY, body JSONB NOT NULL, updated_at TIMESTAMPTZ
);

-- providers: DDL in §8 ("What judge_it keeps"). ~20-40 migration-seeded rows
-- mapping provider_key → base_url + vendor + capabilities. No catalog copy;
-- OSV owns the catalog and the dropdowns. Secrets live in Secrets Manager.

CREATE TABLE campaigns (…);  CREATE TABLE campaign_runs (…);
```

### Dedupe the prompts — this one is not optional

The `resurgent` system prompt is ~1400 lines (`plan/docs/resurgent-prompt.md`).
Every caller LLM call carries the full persona, and there are up to
`max_turns: 30` of them per run. Storing prompt bodies inline means roughly
**tens of MB per run**, and thousands of runs makes the table unusable within
weeks.

Content-address them: hash the prompt, insert once into `prompts`, reference
by id. A regression suite re-running the same persona 500 times stores it
once. Offload bodies over ~256 KB to S3 with the row keeping the pointer.

### Operational

- **Async driver + pooling.** `asyncpg` via SQLAlchemy async. Workers hold
  long-lived connections across multi-minute calls — size the pool to worker
  concurrency, not request rate, and put **PgBouncer in transaction mode** in
  front once there are more than a handful of worker pods.
- **Never hold a transaction open across a phone call.** Write at state
  transitions; don't wrap the call in a session.
- **Alembic from commit one**, including the initial schema. Retrofitting
  migrations onto a live results table is miserable.
- **Retention.** `runs`/`run_evaluation`/`run_turns` keep indefinitely (small,
  and they're the trend data). `run_llm_calls` 90 days. Audio: 30 days, or
  keep only for failed/degraded runs. Raw frames: 7 days, `profile: debug`
  only. S3 lifecycle rules do the deleting; `artifacts.expires_at` records
  the intent so the API can say "expired" rather than 404.
- **Partition `run_turns` and `run_llm_calls` by month** when they get big.
  Not on day one.
- **Migration from `judge_it.db`:** don't. Archive the file, start clean, and
  keep a read-only script if a historical lookup is ever needed. Backfilling
  12 MB of rows that lack `client_uuid`, `assistant_uuid`, and correlation
  fields produces records that can't answer any of the new questions anyway.
- **S3 key layout mirrors the old folder names** so triage muscle memory
  survives: `s3://judge_it-artifacts/{region}/{client}/{assistant}/{date}/run-{run_uuid}/{bot,caller}.wav`.

---

## 10. Features worth adding that you didn't ask about

Ordered by how much I think they're worth.

1. **CI gate on assistant version.** `POST /v1/gates/{suite}` with
   `assistant_uuid` + `version` → runs the suite (direct transport, §5c, so
   it's fast and free) → returns pass/fail + report URL. This is what turns
   judge_it from a diagnostic tool into a deploy gate, which is the real
   product. Pair with **version-diff**: run suite S against version N and
   N−1, report pass-rate delta per scenario.
2. **Evaluator validation harness.** The evaluator is an unvalidated LLM judge
   that decides pass/fail. Hand-label ~50 transcripts, then measure the
   evaluator's precision/recall against those labels on every prompt change.
   `judge_it_evaluator_agreement`. Without this you don't know your test results
   are true. `EVALUATOR_TEMPERATURE = 0.0` (`runner.py:39`) shows you're
   already thinking about verdict stability — this is the next step.
3. **Deterministic replay.** Already in the PRD ("Replay never touches Twilio,
   Deepgram, or the FastAPI server"). Prioritise it: it makes evaluator-prompt
   iteration free, and it's the substrate for #2.
4. **Flake detection.** `repeats: N` on a scenario, report per-scenario flake
   rate, `judge_it_scenario_flake_rate`. A scenario at 70% is worse than a
   scenario that fails — it teaches people to ignore red.
5. **Phoenix experiments for evaluator iteration.** Covered in §6 — worth
   repeating here because it replaces work rather than adding it: a curated
   dataset of hand-labelled transcripts + the evaluator prompt as a Phoenix
   experiment gives you #2 without building a harness.
6. **Result push beyond webhooks.** OSV has `app/notifications/channels` —
   Slack summary on campaign completion, per-failure thread with the report
   URL.
7. **Region awareness.** You operate us / india / canada. Tag runs by region,
   dial region-appropriate DIDs, and keep artifacts in-region for data
   residency.
8. **Synthetic-noise scenarios.** Inject background noise, packet loss, and
   accent variation into the caller's audio. You already have the audio path;
   this tests robustness nothing else covers.

---

## 11. Hazards found while reading

Three things I'd want decided before the service mode ships, because they get
worse with volume.

### 11.1 judge_it's calls will pollute production analytics

`docs/core-concepts/is-test-flag.md` states plainly: *"Voice calls
(gRPC/LiveKit) are always live."* `is_test` is settable from the imposter
simulator, the CLI, the text runtime, and the interface API — but **not from
an inbound voice call**.

So every judge_it call into a production bot today is recorded as a **live
production interaction**: post-call processing runs, compliance checks run,
analytics counts it, and any configured webhook or CRM write fires. At CLI
volume that's noise. At nightly-suite volume across clients it corrupts
reporting and may write to customer systems.

**Confirmed empirically, 2026-08-11.** Interaction
`799a39eb-d01e-4fb0-acb1-4211cf8457df` was an judge_it test call that came back
with `disposition = faq_due` — a *computed* post-call field, so post-call
processing ran on it. It carried `x-is-test = 1` in its own
`interaction_metadata` at the time. The flag is present and ignored.

Fixes, in order of preference:

1. **SIP header** — `X-Is-Test: 1` alongside the interaction uuid (§5a).
   **judge_it's half is DONE**: with `JUDGE_IT_CALL_TRANSPORT=sip` the flag is
   already delivered on every call and stored as `x-is-test` on the interaction.
   What remains is only the OSV read — have the LiveKit session handler set
   `is_test=True` from that attribute. This is now the smallest of the three
   fixes, not the largest, because the transport work is already paid for.
2. **Caller-number whitelist** — OSV has `app/interface/views/phone_whitelist.py`.
   Register judge_it's caller IDs; voice runtime sets `is_test=True` on match.
   Works over plain PSTN, needs no header support. Probably the pragmatic
   first move.
3. **Dedicated test DIDs / test assistant versions** — cleanest isolation,
   most operational overhead.

Worth confirming with whoever owns post-call processing whether any of it
writes outward (webhooks, CRM, dialer callbacks) before scaling up call
volume.

### 11.2 Artifacts contain unredacted PII

`clients/resurgent/` scenarios collect **DOB, SSN, and ZIP** as part of the
verification flow. judge_it records them unredacted in every destination:
`run_turns.text`, `run_llm_calls` (full prompt + response),
`runs.scenario_payload` (the persona and `vars`), the audio WAVs, the log lines,
and — new as of §6 — **Phoenix span attributes** (`LLM_INPUT_MESSAGES` carries
the entire persona on every turn).

Today that's a folder on a laptop. After §2, §6, and §9 it's a **Postgres
table, a shared Loki tenant, an S3 bucket, a Phoenix project, and an API that
hands out presigned URLs** — five destinations instead of one, in a
debt-collection context. Two of them make this *more* urgent rather than less:
Loki retention is per-tenant and Phoenix spans are immutable, so neither can be
selectively deleted the way a file could. And with payload-first (§2), the PII
now also arrives **over the network from other services**, so it's in request
logs and any proxy in between.

OSV already solved half of it: `app/lib/redaction/` with pluggable processors,
wired into `slog.py` behind `ENABLE_LOG_REDACTION`. Port it, and apply it at
**three** points, not one:

1. **Log lines** — the `Redactor` on the logfmt formatter, as OSV does. Must
   happen *before* the line leaves the process; you cannot un-ingest from Loki.
   Same for **Phoenix span attributes**: redact `LLM_INPUT_MESSAGES` /
   `LLM_OUTPUT_MESSAGES` at the span-emission point (§6), not downstream.
2. **Postgres writes** — `run_turns.text`, `run_llm_calls`, and
   `resolved_config`. Redact on write, and consider column-level encryption
   for the persona `vars` (OSV has `MetadataEncryptor` / `FieldEncryptor` in
   `context_manager.py` and `app/lib/encryption/` — same pattern).
3. **API responses** — the run-detail endpoint and the webhook payload. Scope
   who can see unredacted text by role, rather than assuming the API is
   internal-only forever.

Audio is the hard case and redaction doesn't help: either don't retain caller
audio for scenarios carrying real PII, or retain it encrypted with a short TTL
and access logging. `profile: regression` probably wants audio; a suite that
speaks real SSNs probably doesn't.

Even though the scenarios likely use synthetic identities, the pipeline should
not depend on that being true forever.

### 11.3 Secrets

`.env` is gitignored and only `.env.example` is tracked — good. But
`.env.bak.1784111876` is sitting untracked in the working tree (the `.env`
pattern doesn't match `.env.bak.*`), which is exactly how stray credential
copies survive on a laptop.

Every vendor credential today lives in that file, read through
`pydantic-settings` (`core/config.py:10-78` — Twilio SID/token, Deepgram,
Gemini, Anthropic, OpenAI keys). §8 replaces that with AWS Secrets Manager, so
the fix is structural rather than hygienic: once keys resolve from Secrets
Manager, there is nothing sensitive left in `.env` and a stray copy is
harmless.

Interim steps worth doing regardless:

- Tighten the gitignore to `.env*` with `!.env.example`.
- Delete `.env.bak.1784111876` and rotate anything it holds — a `.bak` written
  by a script is not a file anyone is tracking the lifetime of.
- Use Infisical (`.infisical.json`, `infisical run --`) for the non-vendor
  config OSV already manages that way, so CI and k8s need no file on disk.

---

## 12. The four steps in detail

Same four steps as the one-pager, expanded. Each is independently useful; none
requires the next.

### Step 1 — See what's happening

No architecture change. Days, not weeks.

- logfmt to **stdout only**; rename `call_id_var` → `run_uuid_var`; add
  `interaction_uuid`, `scenario_hash`, `seq`, `trace_id` as line fields (§6).
- **Delete** `debug.log`, the `FileHandler`, `_CallIdFilter`, every local sink.
  Stand up Alloy → Loki with the bounded label set.
- `judge_it/instruments/metrics.py` with OSV's `LATENCY_BUCKETS`; wire
  `runs_total`, `run_duration`, `active_runs`, `llm_tokens`, `bot_turn_latency`
  from the code that already computes p50/p95.
- Frame types + observer fanout, emitted *alongside* the existing callbacks in
  `stream.py` — don't move logic yet (§1). This is how metrics and spans get
  emitted without editing 1572 lines.
- Phoenix: port `phoenix_utils.py`, root span per run with OSV's attribute names
  (`conversation.id` = interaction uuid), LLM spans carrying input/output (§6).
- **Redaction on the log formatter and the span emitter — before the first line
  or span ships.** Neither can be un-ingested (§11.2).
- Add the correlation fields to the result model, nullable, unpopulated (§5).

*Done when:* pasting an `interaction_uuid` into Grafana returns judge_it's and OSV's
view of the same call, and Phoenix shows the caller's and the bot's LLM calls
side by side.

### Step 2 — Stop losing state

- Async SQLAlchemy + Alembic; full §9 schema in the first migration. Archive
  `judge_it.db`, no backfill.
- Payload schema in `core/schema.py` (§2); semantic validation; canonical-JSON
  hashing; **sandboxed Jinja2 or `str.format`**; `extra="forbid"`.
- `judge_it migrate-scenarios` converter; convert the corpus. Delete
  `scenario_loader`'s YAML path, `campaign_loader`, `detect_schema`,
  `_scenario_log_root`.
- `CallLogWriter` contents become rows: `run_turns`, `run_evaluation`,
  `run_llm_calls` with the **`prompts` hash dedupe — now, not later**.
- S3 for audio + `artifacts` rows + presigned URLs. `_sessions` → Redis.
- `GET /v1/runs/{uuid}` with `debugging_urls` — ship it in the same step that
  deletes the run folder.

*Done when:* a restart doesn't lose in-flight runs, and a six-week-old run is
still fully readable.

### Step 3 — Be callable

- `serve api` / `serve worker` / `serve media` split.
- `POST /v1/runs`, `POST /v1/scenarios`, `?interaction_uuid=`, cancel, campaigns;
  idempotency; admission control; signed callbacks; published OpenAPI.
- `providers` table (~20-40 rows) + Secrets Manager resolution at submit, so bad
  keys and missing credentials fail at admission (§8).
- Secrets: port `secrets_manager.py` + `service_keys.py` under `judge_it/*`, Redis
  read-through, no client dimension, env fallback DEV-only.
- LLM on the OpenAI schema: one `AsyncOpenAI` client, `base_url` from the
  `providers` row; delete `llm/gemini.py`, `detect_provider`, `_api_key_for`;
  write the `openai_compat` normalisation layer; stream the caller (§8).
- Add sarvam/magpie so India-region bots become testable.
- Config resolver + profiles (§3).

*Done when:* OSV's UI can submit a run and display the result.

### Step 4 — Survive unattended

- Run state machine, transitions persisted; **reaper** (§7.3).
- Graceful drain; `terminationGracePeriodSeconds` above max call duration.
- Per-vendor token buckets + circuit breakers; cost caps + kill switch.
- Retention and S3 lifecycle rules.
- `judge_it vendors check` in CI; capability manifests drive lint (§8).

*Done when:* it runs for a month without anyone watching it.

### Then: the payoff

- Direct (non-telephony) transport, so gate runs are fast and free (§5c).
- `POST /v1/gates/{suite}` — CI gate on assistant version (§10).
- Phoenix-experiment evaluator harness (§10) — nearly free once step 1 lands.
- Deterministic replay.

### Deliberately not doing

- **Decomposing `stream.py` into processors.** Large, risky, and step 1's
  observers already give you the visibility that was the main argument for it.
- **Out-of-tree vendor plugins.** A vendor is a `providers` row; nobody has asked
  for a plugin.
- **Flake detection, version-diff.** Real value, but only after the CI gate.
- **Migrating `judge_it.db`.** Rows lacking `client_uuid`, `assistant_uuid`, and
  correlation fields can't answer any of the new questions.

### Cross-team work, runs in parallel from day one

None of this is judge_it code, and all of it has lead time:

- Confirm judge_it's vendor keys are quota-isolated from production (Twilio first).
- `is_test` for voice calls — OSV change or caller-ID whitelist. **Now the only
  item on this list still blocked on another team**, and more urgent than when
  it was written: judge_it already *delivers* `x-is-test=1` to the bot over SIP
  and it is stored on the interaction, but nothing consumes it, so test calls
  are still recorded as live production interactions with computed dispositions.
- ~~`interaction_uuid` injection — OSV change plus a SIP-trunk probe.~~ **Done
  2026-08-11, judge_it-side only** — the probe passed and no OSV change was
  needed (§5a).

---


## Open questions

Grouped by who can answer them. Nothing here blocks step 1.

### Needs the OSV team (3)

1. **Who owns the OSV-side change** for `interaction_uuid` + `is_test` injection?
   Small change, crosses a team boundary. (§5, §11.1)
2. **Will OSV take the capability fields** (`interim_results`, `code_switching`,
   `structured_output`, …) into `RuntimeProvider`? If yes there's one source of
   truth and judge_it's overlay disappears — and OSV needs the same facts anyway,
   for barge timing and its own structured LLM calls. (§8)
3. **Is OSV's `provider_key` naming stable and derivable** (`{vendor}-{type}-{language}`)?
   A CI diff against `GET /providers/` keeps judge_it's rows aligned, but only if
   the keys are predictable. (§8)

### Needs infra / ops (4)

4. **Are judge_it's vendor keys production's keys under a different path?**
   Diff the values. Check Twilio first. If shared, provisioning separate accounts
   blocks any load testing. (§8)
5. **Decided:** same Loki tagged `app="judge_it"`, separate Phoenix *project*,
   isolated Postgres (§6, §9). What's left to confirm: can the existing Phoenix
   instance take judge_it's span volume, and do current production Grafana panels
   pin `app=` — if not they'll start including test traffic.
6. **Is there already an Alloy/promtail DaemonSet** in these clusters, or does log
   shipping need provisioning? Changes step 1's size. (§6)
7. **Celery+Redis or SQS?** Celery matches OSV's `app/session/celery`, SQS matches
   its post-call durability. Pick one. (§7)

### Needs a decision from you (4)

8. **Who edits scenarios** once they're payloads? "Engineers with curl" →
   `judge_it push` from a git directory. "QA or client-success" → a screen in
   `llm-studio-frontend`. Decide before the YAML is gone, not after. (§2)
9. **Who submits scheduled runs?** If OSV's UI is the only caller, an interface
   outage silently skips the nightly suite. JudgeIt's own scheduler is more robust
   for a canary. (§8)
10. **Which vendors beyond Deepgram, in priority order?** Sarvam and Magpie unblock
    India; mirror what the India-region bots actually run in production, not a
    superset. (§8)
11. **Standalone repo or `app/probes/` inside OSV?** I'd stay standalone — same
    independence argument as not sharing the framework. (§1)

### Answerable with a 20-minute check (4)

12. **Does Gemini's OpenAI-compat endpoint** handle strict JSON-schema
    `response_format` and the `2.5-pro` thinking budget? Same rubric, same
    transcript, native vs compat, 20 runs each. Gates deleting `llm/gemini.py`. (§8)
13. ~~**Does the Twilio trunk forward `X-` headers**, and does LiveKit expose
    `sip.h.*`?~~ **ANSWERED 2026-08-11: yes to both.** Verified on interaction
    `799a39eb-d01e-4fb0-acb1-4211cf8457df`. Not via the *trunk*, though — via
    `To=sip:…` on Twilio's REST API, which bypasses the Elastic trunk entirely.
    LiveKit exposes them through the inbound trunk's `headersToAttributes`
    (lowercased). The clean `interaction_uuid` path is open and shipped. (§5a)
14. **Do any personas use Jinja2 loops or conditionals**, or only substitution? If
    only substitution, drop Jinja2 for `str.format` and the template-injection
    risk disappears rather than being sandboxed. One grep. (§2)
15. **Is `legacy/flow.py` + `flows/*.yaml` still driving anything?** Determines
    convert vs delete. (§2)

### Worth confirming, low stakes (2)

16. **Are the PII fields in `clients/*/variables.json` synthetic?** Changes how hard
    redaction has to be, not whether to do it. (§11.2)
17. **Does OSV post-call processing write outward** (webhooks, CRM, dialer) for a
    live interaction? Determines how urgent the `is_test` fix is. (§11.1)
