# V-Judge — Conversational Voice Bot Evaluation Engine 🎯

> **V-Judge** initiates real-time automated phone calls to voice bots, roleplays as a caller using conversational LLMs, and evaluates bot quality, latency, and compliance.

`vjudge` conducts real-time phone calls over telephony networks, Streams real-time audio (STT/TTS), runs LLM-driven evaluator judging models, and saves detailed evaluation runs under structured `logs/` directories containing full transcripts, WAV recordings, scores, and telemetry.

---

## 🚀 Execution Modes

V-Judge supports three primary execution workflows:

| Workflow Mode | Command / Interface | Description |
| :--- | :--- | :--- |
| **Single Scenario** | `vjudge run <scenario.yaml>` | Executes a single synthetic evaluation scenario defined by the user. |
| **Campaign Sweep** | `vjudge campaign <campaign.yaml>` | Runs a batch sweep of multiple scenarios across configurable concurrency workers. |
| **Job Mode (HTTP API)** | `POST /v1/jobs` | Automatically generates and executes replication scenarios from historical interaction telemetry. |

> **Note**: For external callers invoking an existing hosted instance via HTTP API, see [docs/job-api.md](docs/job-api.md) for endpoint details.

---

## 🛠️ Environment Setup & Quick Start

### Fast-Track Setup

V-Judge includes automated lifecycle scripts for dependency installation, database migrations, Docker observability stack setup, and web dashboard invocation:

```bash
./scripts/setup.sh     # One-time initialization & DB migration script
./scripts/start.sh     # Launches public tunnel → API server → worker pool → web console (localhost:3400)
./scripts/stop.sh      # Gracefully shuts down background services
```

Detailed local setup instructions are available in [docs/local-dev.md](docs/local-dev.md). For Claude Code workflows, see `CLAUDE.md`.

---

### Manual Setup Steps

#### 1. Python Environment (3.11 or 3.12)
*Note: Python 3.13 removes stdlib `audioop`; Python 3.12 is recommended.*

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

To enable the optional Silero VAD turn-end detection engine (installs PyTorch ~200MB):
```bash
pip install -e '.[vad]'
# Enable via environment variable: VJUDGE_USE_SILERO_VAD=true
```

#### 2. HTTPS Telephony Tunnel (Required)
Twilio requires a public HTTPS endpoint to send TwiML callbacks and establish inbound WebSocket audio streams.

```bash
cloudflared tunnel --url http://localhost:8765
# Set the resulting HTTPS URL into VJUDGE_PUBLIC_URL inside .env
```

#### 3. Redis Service (Required for Job Mode & Worker Streams)
Job mode queuing and prompt caching rely on Redis.

```bash
docker run -d -p 6379:6379 --name vjudge-redis redis
```

#### 4. Configuration Settings
Create a local environment configuration file:
```bash
cp .env.example .env
```
*All configuration keys use the `VJUDGE_` prefix.*

##### Standard Execution Variables:
```env
VJUDGE_TWILIO_ACCOUNT_SID=AC...
VJUDGE_TWILIO_AUTH_TOKEN=...
VJUDGE_TWILIO_PHONE_NUMBER=+1...      # Caller ID
VJUDGE_DEEPGRAM_API_KEY=...           # STT + TTS engine key
VJUDGE_GEMINI_API_KEY=...             # Caller & Evaluator LLM key
VJUDGE_PUBLIC_URL=https://<your-tunnel-domain>
```

##### Additional Job Mode & SIP Integration Variables:
```env
VJUDGE_CALL_TRANSPORT=sip             # SIP transport mode
VJUDGE_SIP_DESTINATION_HOST=your-sip-host.livekit.cloud:5060
VJUDGE_SIP_TRUNK_NUMBER=1205
VJUDGE_OSV_BASE_URL=https://llm-studio.vjudge.dev/us/api/interface
VJUDGE_OSV_ACCESS_TOKEN="<Bearer JWT Access Token>"
VJUDGE_OPENAI_API_KEY=sk-...          # Scenario synthesis LLM key
VJUDGE_REDIS_URL=redis://localhost:6379/0
```

#### 5. Launch Server
```bash
vjudge serve                          # Launch server & API handlers
```

---

## 🌐 Telephony Transport: PSTN vs. Native SIP

V-Judge supports two outbound dialing mechanisms:

1. **PSTN (Default)**: Dials target numbers as standard E.164 phone numbers over the public switched telephone network.
2. **Native SIP (`VJUDGE_CALL_TRANSPORT=sip`)**: Direct SIP INVITE routing to LiveKit / SIP servers.

### SIP Header Correlation
In SIP mode, custom headers pass correlation metadata directly into the bot's call session:
```
From: +12202206062
To:   sip:1205@your-sip-host.livekit.cloud:5060
        ?X-Is-Test=1
        &X-VJudge-Session-Id=<session-id>
        &X-VirtualNumber=%2B18447331982
```
- `X-VirtualNumber`: Maps the target E.164 phone number.
- `X-VJudge-Session-Id`: Primary session correlation identifier.
- `X-Is-Test`: Flags the interaction as a synthetic test.

---

## 📜 Command-Line Tooling (CLI)

```bash
# Execute a single scenario evaluation
vjudge run clients/_examples/happy-path.yaml

# Run a campaign sweep with 4 parallel workers
vjudge campaign clients/acs/campaigns/auth-sweep.yaml -m 4

# Lint scenario files for syntax and structural integrity
vjudge lint "clients/_examples/*.yaml"

# Generate a Barge Context Document (BCD) from bot system prompt
vjudge bcg path/to/bot-prompt.txt -o clients/demo/bargin-context.md
```

---

## 🤖 Autonomous Job Mode (Replication Pipeline)

Job mode allows testing against historical production interaction logs:

1. **`POST /v1/jobs`**: Submits a job request referencing production call records.
2. **`GET /v1/jobs/{id}/wait`**: Synthesizes custom test scenarios using historical call metadata.
3. **`POST /v1/jobs/{id}/run`**: Executes the scenario call sequence and correlates evaluator output back to interaction records.

---

## 📋 Scenario YAML Schema Reference

Scenario files specify the persona, target, assertions, and evaluation rules:

```yaml
name: "Customer Verification Happy Path"
target: "+18447331982"
vars:
  account_id: "ACC-9921"
persona: |
  You are roleplaying as a customer attempting to verify account details.
evaluation_prompt: |
  Evaluate if the bot greeted politely, verified identity, and completed the task.
llm:
  caller_model: gemini-2.5-flash
  evaluator_model: gemini-2.5-pro
  temperature: 0.3
max_turns: 30
barge_in:
  enabled: true
  mode: scheduled
```

---

## 📂 Repository Directory Layout

```
vjudge/
├── core/             # Configuration, schema loaders, models, and linters
├── engine/           # Conversational AI caller & barge-in logic
├── transport/        # Telephony drivers (Twilio, SIP, WebSockets, STT/TTS)
├── orchestrator/     # TestRunner, campaign dispatcher, and judge engine
├── jobs/             # Autonomous job pipeline & API handlers
├── services/         # Telemetry & telemetry integration services
├── llm/              # LLM client abstractions & providers
├── bcg/              # Barge Context Document generator
├── persistence/      # Result storage & structured log writer
└── cli/              # vjudge command dispatcher & CLI modules
```

---

## 🧪 Testing Suite

Run unit and integration test suites:
```bash
# Execute offline test suite
pytest tests/ -q

# Run live end-to-end smoke test against target bot
RUN_LIVE=1 pytest tests/e2e/test_smoke.py
```

---

## 📄 License
Internal Use & Evaluation Engine © 2026 PSGg19
