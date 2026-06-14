# Opkura — Voice-Bot Test Runner

Opkura places a **real phone call** to a voice bot, plays the caller with an LLM,
and judges the result.

```
scenario YAML  →  opkura places the call  →  caller LLM holds the conversation
(STT/TTS in real time)  →  evaluator LLM scores the transcript  →  a folder
under logs/ with everything: transcript, audio, evaluation, debug log.
```

Three ways to drive it:

| Mode | Input | Who writes the scenario |
|---|---|---|
| `opkura run <scenario.yaml>` | one scenario | you |
| `opkura campaign <campaign.yaml>` | a list of scenarios | you |
| **job mode** (HTTP, `POST /v1/jobs`) | **a list of faulty production calls** | **opkura, from the real call data** |

> **Only want to *use* job mode against someone else's running instance?**
> You need nothing from this repo — read **[docs/job-api.md](docs/job-api.md)**
> instead. No clone, no install, no credentials. Everything below is for running
> opkura yourself.

---

## Setup

### 1. Python 3.11 or 3.12

Not 3.13: `transport/vad.py` uses `audioop`, which PEP 594 removed from the
stdlib in 3.13. Verified on 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Add the Silero turn-end detector only if you want it — it pulls torch (~200MB)
and defaults to off:

```bash
pip install -e '.[vad]'      # then AUTOCALLS_USE_SILERO_VAD=true
```

### 2. A public HTTPS tunnel — always required

Twilio fetches TwiML from opkura and opens the media WebSocket *inbound*, so
localhost cannot work. Any tunnel is fine:

```bash
cloudflared tunnel --url http://localhost:8765
# copy the https URL into AUTOCALLS_PUBLIC_URL
```

### 3. Redis — required for job mode only

Job state and the per-client prompt cache. `run`, `campaign` and `lint` don't
touch it.

```bash
docker run -d -p 6379:6379 --name opkura-redis redis
```

If `redis` isn't installed or isn't reachable, the server still starts and the
Twilio webhooks still serve — that's deliberate, an in-flight call must not die
for a missing subsystem — but `/v1/jobs` returns **404** and the only clue is a
startup line:

```
WARNING  Job API not mounted (ModuleNotFoundError: No module named 'redis')
```

### 4. Configure

Copy [.env.example](.env.example) to `.env` and fill it in. **Every variable
takes the `AUTOCALLS_` prefix** — pydantic-settings reads `.env` itself, and the
file is never exported to the process environment.

Minimum, whatever you're running:

```
AUTOCALLS_TWILIO_ACCOUNT_SID=AC...
AUTOCALLS_TWILIO_AUTH_TOKEN=...
AUTOCALLS_TWILIO_PHONE_NUMBER=+1...      # caller ID
AUTOCALLS_DEEPGRAM_API_KEY=...           # STT + TTS
AUTOCALLS_GEMINI_API_KEY=...             # caller + evaluator LLM
AUTOCALLS_PUBLIC_URL=https://<tunnel>
```

Job mode adds:

```
AUTOCALLS_CALL_TRANSPORT=sip             # job mode refuses pstn — see below
AUTOCALLS_SIP_DESTINATION_HOST=581mqcm8zvu.sip.livekit.cloud:5060
AUTOCALLS_SIP_TRUNK_NUMBER=1205
AUTOCALLS_OSV_BASE_URL=https://llm-studio.skit.ai/us/api/interface
AUTOCALLS_OSV_ACCESS_TOKEN="<SkitAdmin access JWT>"
AUTOCALLS_OPENAI_API_KEY=sk-...          # writes the scenarios
AUTOCALLS_REDIS_URL=redis://localhost:6379/0
```

The OSV token is a **short-lived access token (~8h)** copied from a logged-in
llm-studio session: DevTools → Network → any `/api/interface/` request → the
`authorization: Bearer …` header. Keep it quoted in `.env`; pydantic strips the
quotes, but any script that parses `.env` by hand will send the quotes along and
get `401 Invalid token` from a perfectly good token.

### 5. Start the server

Needed for job mode, and for `run`/`campaign` too — that's what Twilio calls back
into.

```bash
opkura serve                 # or: python -m opkura serve
```

---

## Outbound transport — PSTN or pure SIP

By default opkura dials the scenario's `target` as an E.164 number, so the call
routes over the PSTN to the bot's DID and in through that DID's SIP trunk. That
works, but it costs a carrier leg and drops every custom SIP header — leaving
`(caller#, called#, wall-clock)` as the only way to join an opkura run to the
bot's own interaction record.

Set `AUTOCALLS_CALL_TRANSPORT=sip` and Twilio INVITEs the LiveKit SIP host
directly instead:

```
From: +12202206062
To:   sip:1205@581mqcm8zvu.sip.livekit.cloud:5060
        ?X-Is-Test=1
        &X-Opkura-Session-Id=<session>
        &X-VirtualNumber=%2B18447331982     ← the scenario `target`
```

Note where the numbers go. The **user part is LiveKit's trunk identifier**
(`1205`), not the bot's DID — the DID travels in `X-VirtualNumber`, which the
inbound trunk maps to the `virtualNumber` attribute:

```json
"headersToAttributes": { "X-VirtualNumber": "virtualNumber" }
```

Scenario YAML is **identical** between the two modes: `target` stays E.164 and
the lint rule still enforces that. Only env changes.

**Job mode requires SIP** and returns `400` under `pstn`, because correlation
keys on the `X-Opkura-Session-Id` header and a PSTN call cannot carry it.

`X-Opkura-Session-Id` is the correlation key rather than the Twilio Call SID,
because the SID is minted by the very `calls.create` request that sends the
INVITE and so cannot appear in it. opkura records session_id → call_sid, which
closes the loop.

**Verified end to end** on 2026-08-11 (interaction
`ed845f16-9fe7-4837-a038-903c5fe54f9d`). A probe call landed on the bot and the
interaction metadata carried:

```
x-virtualnumber      = +18447331982     # %2B decoded back to '+'
virtual_number       = +18447331982     # the headersToAttributes mapping
x-opkura-session-id  = probe-session    # our correlation key
x-twilio-callsid     = CAe372f045…      # stamped by Twilio itself
x-is-test            = 1
ani / caller_number  = +12202206097     # Twilio caller ID preserved
livekit_sip_call_id  = SCL_EwBjeGdH92R7
```

So LiveKit percent-decodes the value (no `+`-stripping needed), and header names
arrive **lowercased** — read `x-virtualnumber`, not `X-VirtualNumber`.

Note `x-twilio-callsid`: Twilio stamps its own Call SID on the INVITE, so the bot
receives the Twilio SID *and* our session id. That is undocumented by Twilio, so
treat it as a convenience — `x-opkura-session-id` is the identifier we control.

**LiveKit prerequisite.** The inbound trunk needs both a trunk entry AND a
**dispatch rule** covering the trunk number. With the trunk alone, LiveKit
answers `404 Does not match Trunks or Dispatch Rules` and Twilio reports only a
bare `failed`/0s with no error code. See `scripts/probe_sip_invite.py` for how to
read the real SIP response.

---

## CLI

### Run one scenario

```bash
opkura run clients/_examples/happy-path.yaml
```

Auto-lints V2 scenarios before dialing. `--no-lint` skips it while iterating.

### Run a campaign

```bash
opkura campaign clients/acs/campaigns/auth-sweep.yaml -m 4    # 4 concurrent
```

### Lint without making calls

```bash
opkura lint "clients/acs/scenarios/**/*.yaml"
opkura lint "clients/_examples/*.yaml" --probe    # also dry-runs persona vs LLM
```

### Generate a Barge Context Document (BCD)

For `mode: llm` barge scenarios — extracts a bot's modules, transition phrases
and tool-call moments into a markdown file the caller LLM uses to time barges.
Once per bot:

```bash
opkura bcg path/to/bot-system-prompt.txt \
  -o clients/<client>/bargin-context.md
```

The BCD is checked into git, hand-editable, and referenced from scenarios via
`barge_in.bot_context_path`. `--regenerate` overwrites.
[clients/resurgent/bargin-context.md](clients/resurgent/bargin-context.md) is a
generated one to look at.

### Everything else

`opkura serve`, `load`, `watch`, `results` — `opkura --help` lists them all.

---

## Job mode

You hand it broken production calls. It reads each one from OSV, writes a
scenario that reproduces the issue **using that call's own real account data**,
dials the bot, and correlates the result back to the bot's interaction record.

```
POST /v1/jobs            → job_id, immediately
GET  /v1/jobs/{id}/wait  → blocks until status=ready, then read the scenarios
POST /v1/jobs/{id}/run   → dials
GET  /v1/jobs/{id}/wait  → status=completed + results[] with a call link each
```

You do not write scenarios and you do not supply test credentials — the account
number, DOB, SSN last-4, name and registered phone all come from the faulty
call's own `interaction_metadata`. That is what makes the replication faithful.

- **[docs/job-api.md](docs/job-api.md)** — the caller's guide. Hand this to
  anyone who wants to *use* a running instance; it assumes no repo access.
- **[CLAUDE.md](CLAUDE.md)** — operating it: regions, token refresh, correlation,
  failure modes.

**`jobs/` holds real customer PII** — names, account numbers, DOB, SSN last-4,
lifted from production interactions on purpose. It is gitignored and must stay
that way. Never commit it, never paste it into a ticket, never move it out of the
repo. It's regenerated by re-running the job.

**Test calls are still recorded as live production interactions.** Opkura sends
`X-Is-Test: 1` and it is stored, but nothing on the OSV side consumes it yet, so
post-call processing, analytics and dispositions all fire. Keep jobs small.

---

## Scenario schema

```yaml
name: "..."                   # required, scenario label
target: "+1..."               # required, E.164 target
vars: {...}                   # required, facts the caller knows
persona: |                    # required, system prompt for the caller LLM
  ...
evaluation_prompt: |          # required, system prompt for the evaluator LLM
  ...
llm:                          # optional, model + temperature overrides
  caller_model: gemini-2.5-flash
  evaluator_model: gemini-2.5-pro
  temperature: 0.3
max_turns: 30                 # optional, default 30
barge_in:                     # optional
  enabled: true
  mode: scheduled             # or "llm"
  triggers: [...]             # required for mode: scheduled
```

**[opkura/core/lint.py](opkura/core/lint.py) is the spec** — it's what enforces
the schema, so it's the file to read when a scenario is rejected. Prose version:
[docs/scenario-yaml-guidelines.md](docs/scenario-yaml-guidelines.md).

Worked examples live in **[clients/_examples/](clients/_examples/)**:
`happy-path.yaml` (debit card payment, conversational realism),
`barge-scheduled.yaml` (barges fired at specific turns),
`barge-llm.yaml` (caller LLM decides when to interrupt). Real per-client
scenarios and campaigns sit alongside in `clients/<name>/`.

---

## Layout

| Path | Purpose |
|---|---|
| [opkura/core/](opkura/core/) | config, models, scenario/campaign loaders, lint |
| [opkura/engine/](opkura/engine/) | the AI caller — turn-taking, barge modes |
| [opkura/transport/](opkura/transport/) | Twilio + SIP dialing, media streams, DTMF, VAD |
| [opkura/orchestrator/](opkura/orchestrator/) | TestRunner, campaign runner, evaluator |
| [opkura/jobs/](opkura/jobs/) | job mode: api, pipeline, generator, store, workspace |
| [opkura/services/osv/](opkura/services/osv/) | OSV client (metadata, transcript, prompt, interactions) |
| [opkura/llm/](opkura/llm/) | `LLMClient` protocol + providers |
| [opkura/bcg/](opkura/bcg/) | Barge Context Document generator |
| [opkura/persistence/](opkura/persistence/) | result store + per-call log folders |
| [opkura/cli/](opkura/cli/) | the `opkura` command |
| [clients/](clients/) | hand-written scenarios and campaigns, per client |

## Logs

Every call writes a folder — `logs/clients/<client>/…/call-<slug>-<utc>-<id>/`
for hand-written runs, `logs/jobs/<job_id>/scenarios/call-…/` for job mode:

```
scenario.yaml           snapshot of the input
rendered_persona.txt    persona after Jinja2 + BCD application
rendered_eval.txt       evaluation prompt after Jinja2
transcript.json         structured turns (speaker, text, ms, barge fields)
transcript.txt          human-readable transcript with [BARGE] markers
evaluation.json         evaluator verdict (overall_passed, criteria, metrics)
llm_calls.json          every caller + evaluator LLM call (input/output/latency)
metadata.json           call_sid, duration, p50/p95/max latencies, tokens
bot.wav, caller.wav     mulaw → PCM 16-bit WAV recordings
debug.log               per-call logs captured from the `opkura` logger
```

## Testing

```bash
pytest tests/ -q                          # unit + integration, no live calls
RUN_LIVE=1 pytest tests/e2e/test_smoke.py  # live, against your configured target
```

Live tests skip without `RUN_LIVE=1`. Two `tests/unit/core/test_lint.py` CLI
tests fail on a stock checkout because of ANSI colour capture — pre-existing and
unrelated to anything you change.

## Backward compatibility

V1 flow YAMLs in [flows/](flows/) still work via `opkura run flows/foo.yaml`.
Schema detection is automatic: `persona:` + `evaluation_prompt:` means V2,
`steps:` means V1. Hybrid YAMLs are rejected at load time.

## Docs

| Doc | For |
|---|---|
| [docs/job-api.md](docs/job-api.md) | **using** job mode over HTTP — hand this out |
| [CLAUDE.md](CLAUDE.md) | operating job mode: regions, tokens, correlation |
| [docs/architecture.md](docs/architecture.md) | how the pieces fit |
| [docs/scenario-yaml-guidelines.md](docs/scenario-yaml-guidelines.md) | writing scenarios |
| [docs/schema.md](docs/schema.md) | schema reference |
| [docs/audio-pipeline-and-latency.md](docs/audio-pipeline-and-latency.md) | STT/TTS path, latency budget |
| [docs/platform-evolution.md](docs/platform-evolution.md) | design decisions and open questions |

## License / Contact

Internal.
