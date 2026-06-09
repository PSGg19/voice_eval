# Opkura

Automated voice AI testing tool. Call your bots, follow scripted conversation flows, send DTMF tones, and verify they're up and stable.

```
You define a YAML flow → Opkura calls your bot → listens via STT → speaks back via TTS → evaluates pass/fail
```

## How It Works

```
┌─────────────┐     Twilio API      ┌──────────────┐
│   Opkura     │ ──── outbound ────▶ │  Your Voice   │
│   Engine     │     phone call      │  Bot (phone#) │
│              │                     └──────┬───────┘
│  ┌────────┐  │◀── Media Stream (WS) ─────┘
│  │  STT   │◀─│─── transcribes bot audio
│  │  TTS   │──│──▶ speaks as caller
│  │  DTMF  │──│──▶ sends keypad tones
│  │  Flow   │  │    follows YAML script
│  │  Eval   │  │    checks pass/fail
│  └────────┘  │
└──────┬───────┘
       │
  ┌────▼────┐
  │ SQLite   │  Results + transcripts
  └─────────┘
```

**The call pipeline step by step:**

1. Opkura makes an outbound call to your bot's phone number via Twilio
2. Twilio opens a bidirectional audio WebSocket (Media Stream) back to Opkura
3. Bot speaks → audio is streamed to Deepgram STT → real-time transcript
4. Opkura's flow engine evaluates the transcript against your expected keywords
5. Flow engine picks the next response → Deepgram TTS generates audio (or DTMF tones are synthesized)
6. Audio is sent back through the Twilio WebSocket → bot hears it
7. Repeat until flow completes
8. Results are stored in SQLite and printed to terminal

## Prerequisites

- **Python 3.11+**
- **Twilio account** with:
  - Account SID and Auth Token ([console.twilio.com](https://console.twilio.com))
  - A phone number with Voice capability (this is the caller ID for outbound calls)
- **Deepgram account** for STT and TTS ([deepgram.com](https://deepgram.com))
- **ngrok** (for local development) or a server with a public URL

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/skit-ai/opkura.git
cd opkura
```

### 2. Create a virtual environment and install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```bash
# Twilio — from https://console.twilio.com
AUTOCALLS_TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AUTOCALLS_TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
AUTOCALLS_TWILIO_PHONE_NUMBER=+1xxxxxxxxxx    # your Twilio number (caller ID)

# Deepgram — from https://console.deepgram.com
AUTOCALLS_DEEPGRAM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Server
AUTOCALLS_SERVER_HOST=0.0.0.0
AUTOCALLS_SERVER_PORT=8765
AUTOCALLS_PUBLIC_URL=https://your-ngrok-url.ngrok-free.dev    # see step 4

# Storage
AUTOCALLS_DB_PATH=autocalls.db
```

### 4. Start ngrok (local development)

Twilio needs to reach your machine to stream audio. In a **separate terminal**:

```bash
ngrok http 8765
```

Copy the `https://...ngrok-free.dev` URL from the output and set it in `.env`:

```bash
AUTOCALLS_PUBLIC_URL=https://paragenetic-unsluiced-elayne.ngrok-free.dev
```

> **Note:** The ngrok URL changes every time you restart ngrok (on the free plan). Update `.env` accordingly. For production, deploy on a server with a stable public URL.

## Usage

### Run a single test

```bash
autocalls run flows/example.yaml
```

With verbose logging:

```bash
autocalls run flows/example.yaml -v
```

### Watch mode (repeated calls)

Run every 5 minutes for uptime monitoring:

```bash
autocalls watch flows/example.yaml --interval 300
```

Run exactly 10 times, 1 minute apart:

```bash
autocalls watch flows/example.yaml --interval 60 --count 10
```

### View past results

```bash
autocalls results
```

Filter by flow name:

```bash
autocalls results --flow "Penn Credit Auth Check"
```

### Run the server standalone

If you want to keep the webhook server running separately:

```bash
autocalls serve
```

## Writing Test Flows

Flows are YAML files that define a scripted conversation. Each step is one turn:

1. **Bot speaks** → Opkura listens and checks against `expect` rules
2. **Opkura responds** → via `say` (speech), `dtmf` (keypad tones), or both

### Basic example

```yaml
name: "Greeting Check"
target: "+15551234567"   # your bot's phone number

steps:
  # Step 0: Bot greets, we ask a question
  - expect:
      contains_any: ["hello", "welcome", "how can I help"]
    say: "Hi, what are your business hours?"

  # Step 1: Bot answers, we say thanks
  - expect:
      contains_any: ["hours", "open", "monday", "9"]
    say: "Great, thank you. Goodbye."

  # Step 2: Bot says farewell (no response needed)
  - expect:
      contains_any: ["goodbye", "bye", "have a great day"]

success_criteria:
  all_steps_passed: true
  max_latency_ms: 5000
```

### DTMF example (IVR / keypad navigation)

```yaml
name: "IVR Navigation"
target: "+15551234567"

steps:
  # Bot: "Press 1 for sales, press 2 for support"
  - expect:
      contains_any: ["press 1", "press 2", "sales", "support"]
    dtmf: "1"

  # Bot: "Press 1 to confirm"
  - expect:
      contains_any: ["confirm", "press 1"]
    dtmf: "1"

  # Bot confirms
  - expect:
      contains_any: ["confirmed", "connected", "transferring"]

success_criteria:
  all_steps_passed: true
  max_latency_ms: 5000
```

### Mixed speech + DTMF

A single step can have both `say` and `dtmf`:

```yaml
  - expect:
      contains_any: ["enter your account number"]
    say: "Sure, let me enter it now"
    dtmf: "123456"
```

### Flow reference

#### Step fields

| Field | Type | Description |
|-------|------|-------------|
| `expect` | object | What we expect the bot to say (optional) |
| `expect.contains_any` | list[str] | Pass if bot's speech contains ANY of these (case-insensitive) |
| `expect.contains_all` | list[str] | Pass if bot's speech contains ALL of these |
| `expect.not_contains` | list[str] | Pass if bot's speech contains NONE of these |
| `say` | string | Text to speak via TTS (optional) |
| `dtmf` | string | DTMF digits to send: `0-9`, `*`, `#`, `A-D`, `w` for 0.5s pause (optional) |
| `timeout_ms` | int | Max time to wait for bot to speak (default: 10000) |

#### Top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Test name (shown in results) |
| `target` | string | Phone number to call (E.164 format: `+1...`) |
| `steps` | list | Conversation steps |
| `success_criteria.all_steps_passed` | bool | Require all steps to pass (default: true) |
| `success_criteria.max_latency_ms` | int | Max acceptable response latency (default: 5000) |

## CLI Reference

```
autocalls run <flow.yaml> [-v]              Run a single test flow
autocalls watch <flow.yaml> [-i SEC] [-n N] Run repeatedly (interval/count)
autocalls results [-f NAME] [-n LIMIT]      View past results
autocalls serve [-v]                        Run webhook server standalone
```

## Project Structure

```
opkura/
├── autocalls/
│   ├── cli.py          # Typer CLI (run, watch, results, serve)
│   ├── config.py       # Pydantic settings from .env
│   ├── models.py       # Flow, CallResult, StepResult
│   ├── stt.py          # Deepgram streaming STT (mulaw → text)
│   ├── tts.py          # Deepgram TTS (text → mulaw)
│   ├── dtmf.py         # DTMF tone generator (mulaw 8kHz)
│   ├── flow.py         # Conversation state machine
│   ├── stream.py       # Twilio Media Stream WebSocket handler
│   ├── call.py         # Twilio outbound call management
│   ├── server.py       # FastAPI webhooks + WS endpoints
│   ├── runner.py       # Test orchestrator
│   └── store.py        # SQLite result storage
├── flows/
│   ├── example.yaml    # Basic greeting flow
│   └── ivr_booking.yaml# IVR with DTMF example
├── pyproject.toml
├── .env.example
└── .gitignore
```

## Troubleshooting

### "Call created but no audio / WebSocket never connects"

- Verify ngrok is running and the URL in `AUTOCALLS_PUBLIC_URL` matches
- Check that port 8765 isn't blocked or in use (`lsof -i :8765`)
- ngrok free plan URLs change on restart — update `.env`

### "Twilio returns 401 / authentication error"

- Double-check `AUTOCALLS_TWILIO_ACCOUNT_SID` and `AUTOCALLS_TWILIO_AUTH_TOKEN`
- Ensure the account is active at [console.twilio.com](https://console.twilio.com)

### "STT returns empty transcripts"

- Verify your Deepgram API key is valid
- Check that your Deepgram plan includes the Nova-2 model
- The bot may not be speaking — try with `-v` flag to see raw STT output

### "TTS error / no audio sent to bot"

- Deepgram TTS requires the Aura model — ensure your plan supports it
- Check verbose logs for HTTP errors from the TTS endpoint

### Steps failing unexpectedly

- Run with `-v` to see exactly what the bot said
- Adjust `expect.contains_any` keywords to match the bot's actual wording
- Increase `timeout_ms` if the bot takes long to respond
