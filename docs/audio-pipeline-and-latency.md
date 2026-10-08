# Audio Pipeline & Latency

How audio flows through vjudge end-to-end, where time goes, and why the
current Deepgram-based endpointing fails for long bot turns with internal
pauses.

---

## Architecture overview — two WebSockets, two HTTP services

```
┌──────────┐   WebSocket A   ┌──────────┐   WebSocket B   ┌──────────┐
│ Twilio   │ ◄──────────────►│  VJudge  │ ◄──────────────►│ Deepgram │
│ (audio)  │ mulaw 8kHz      │ (FastAPI │ mulaw bytes /   │  (STT)   │
│          │ 20ms chunks     │  server) │ STT events      │          │
└──────────┘                 └──────────┘                 └──────────┘
                                  │
                                  │ HTTPS POST per turn
                                  ▼
                             ┌──────────┐
                             │ Deepgram │   text → mulaw bytes
                             │  (TTS)   │   aura-2-thalia-en
                             └──────────┘
                                  │
                                  │ HTTPS POST per turn
                                  ▼
                             ┌──────────┐
                             │  Gemini  │   bot text → caller JSON
                             │ (caller) │   gemini-2.5-flash, thinking=0
                             └──────────┘
```

WebSocket A and WebSocket B are **independent connections** with separate
latency budgets. VJudge is the bridge.

### File map

| Component | File | Key entry point |
|---|---|---|
| Twilio dial-out | `vjudge/transport/call.py` | `CallManager.make_call` |
| FastAPI server + webhooks | `vjudge/transport/server.py` | `/twiml/{session_id}`, `/ws/{session_id}` |
| WebSocket A handler | `vjudge/transport/stream.py` | `MediaStreamSession.handle()` |
| WebSocket B (STT) | `vjudge/transport/stt.py` | `DeepgramSTT.start()` |
| TTS (HTTP) | `vjudge/transport/tts.py` | `DeepgramTTS.synthesize()` |
| Caller LLM (HTTP) | `vjudge/llm/gemini.py` | `GeminiClient.complete_json()` |
| Turn-taking orchestration | `vjudge/engine/caller.py` | `AICallerEngine.end_bot_turn()` |
| Call lifecycle | `vjudge/orchestrator/runner.py` | `TestRunner._run_v2_scenario()` |

---

## End-to-end flow per turn

### 1. Inbound audio (bot → vjudge → Deepgram STT)

```
Bot speaks → phone network → Twilio
   ▼
Twilio sends 20ms mulaw chunks over WebSocket A
   ▼
_receive_loop (stream.py)
   ├─► _bot_audio_buffer.extend(audio)        ← saved for bot.wav
   └─► self._stt.send_audio(audio)            ← forwarded over WebSocket B
                ▼
        Deepgram processes audio
                ▼
        Emits events back over WebSocket B:
          • Results (is_final=true/false) → on_final / on_interim
          • UtteranceEnd                  → on_final("") → _utterance_boundary.set()
          • SpeechStarted (vad_events)    → on_speech_started → _speech_started=True
```

### 2. Turn detection (stream.py)

`_wait_for_bot_turn` runs two phases:

- **Phase 1**: poll `_speech_started` (set by interim/final/VAD) until True
- **Phase 2**: `await _utterance_boundary.wait()` (Deepgram UtteranceEnd)
- **Grace** (1.5s): clear flags, watch for resume. If new speech → keep
  waiting. If silence holds → real turn end.

### 3. LLM call (caller.py)

`AICallerEngine.end_bot_turn()`:
- Joins accumulated `_bot_speech_buffer` into one string
- Calls `GeminiClient.complete_json` with persona + history + bot turn
- Returns `CallerTurn{speech, dtmf, hangup}`

### 4. Outbound audio (Gemini text → TTS → Twilio → bot)

```
turn.speech text
   ▼
DeepgramTTS.synthesize() — HTTPS POST to /v1/speak
   ▼
Returns mulaw 8kHz bytes
   ▼
_send_audio() — chunked into 160-byte (20ms) pieces
   • Real-time pacing using deadline-based scheduling
   • No cumulative drift (no `await asyncio.sleep(0.02)` per chunk)
   ▼
Twilio WebSocket A → phone line → bot hears it
```

### 5. End of call

Engine sets `hangup=True` (LLM returned `hangup: true`, or `max_turns`
hit, or 3 consecutive empty bot turns). Conversation loop exits.

`handle()` fires `_signal_completion()` which uses
`runner_loop.call_soon_threadsafe(event.set)` — cross-thread set because
the FastAPI server runs in its own thread/loop, and `asyncio.Event.set()`
is NOT thread-safe.

Runner wakes → `call_mgr.hang_up(call_sid)` → Twilio REST API → call
ends → evaluator runs → logs finalized.

---

## Latency breakdown

Estimates for typical India ↔ US-East routing (ngrok tunnel + cloud
servers). Numbers will vary with your network.

### Per-leg latency

| Hop | One-way latency | Notes |
|---|---|---|
| Bot's mouth → phone network → Twilio ingress | 50-150ms | Carrier side, uncontrollable |
| **WebSocket A: Twilio → VJudge** | **200-300ms** | Twilio US → ngrok → your laptop |
| ngrok tunnel overhead | +20-50ms | Adds to either direction |
| **WebSocket B: VJudge → Deepgram** | **200-300ms** | Your laptop → Deepgram US |
| Deepgram STT inference | 100-300ms | Per audio chunk processed |
| **WebSocket B: Deepgram → VJudge** | **200-300ms** | Event back to your laptop |
| Gemini LLM HTTPS (Flash, thinking=0) | 2000-3000ms | Most of the per-turn budget |
| Deepgram TTS HTTPS | 300-800ms | One POST per caller utterance |
| **WebSocket A: VJudge → Twilio** | **200-300ms** | First chunk to Twilio |
| Twilio buffer + playback to phone | 100-300ms | Per chunk played in real time |

### Total: "bot speaks word → vjudge knows the word"

```
Bot says word (T=0)
    │
    ▼ ~100ms       Twilio receives audio
    │
    ▼ ~250ms       WebSocket A: Twilio → VJudge
    │
    ▼ ~5ms         VJudge forwards to STT
    │
    ▼ ~250ms       WebSocket B: VJudge → Deepgram
    │
    ▼ ~200ms       Deepgram STT processing
    │
    ▼ ~250ms       WebSocket B: Deepgram → VJudge
    │
    ▼
on_stt_final fires (~T=1050ms)
```

**~1 second** from bot saying a word to us knowing what was said.

### Total: turn-end detection delay

```
Bot stops speaking (T=0)
    │
    ▼ ~250ms       Silence arrives via WebSocket A
    │
    ▼ ~250ms       Silence arrives at Deepgram via WebSocket B
    │
    ▼ ~2500ms      Deepgram waits utterance_end_ms of silence
    │
    ▼ ~250ms       UtteranceEnd event back to VJudge
    │
    ▼ ~1500ms      Our GRACE_PERIOD_S confirmation
    │
    ▼
VJudge declares "bot turn ended" (~T=4750ms)
```

**~4.5-5 seconds** of mandatory wait just to confirm the bot is done.

### Total: turn-end → bot hears reply

```
"Bot turn ended" detected (T=0)
    │
    ▼ 2000-3000ms     Gemini call (HTTPS, thinking=0)
    │
    ▼ 300-800ms       Deepgram TTS synthesis (HTTPS)
    │
    ▼ ~250ms          First chunk over WebSocket A to Twilio
    │
    ▼ ~150ms          Twilio plays first chunk on the phone line
    │
    ▼
Bot's STT picks up our first word (~T=2700-4200ms)
```

### Grand total per turn

| Phase | Time |
|---|---|
| Silence detection (Deepgram + grace) | **~4.5s** |
| Gemini LLM | 2-3s |
| TTS synthesis | 0.3-0.8s |
| Network to bot + first-chunk playback | 0.4s |
| **Floor** | **~7-8 seconds** |

Matches what we observe in live runs (~7-10s between bot turns).

---

## Why Deepgram VAD/endpointing fails for long bot turns

### The two Deepgram silence parameters

| Parameter | Controls | Our value | Default |
|---|---|---|---|
| `endpointing=N` | When Deepgram **finalizes a transcript** (interim → final). After N ms of silence, current transcript locks. | 300ms | 10ms |
| `utterance_end_ms=N` | When Deepgram **emits UtteranceEnd event**. Separate timer, triggers our turn-end logic. | 2500ms | 1000ms |
| `vad_events=true` | Adds `SpeechStarted` events from server-side VAD. We use only as a faster `_speech_started=True` signal. | enabled | disabled |

They are **independent timers**, both triggered by silence.

### Concrete failure case — Resurgent's Mini-Miranda

Real run from May 14:

```
Bot says:
  "Thanks Iris. Before we move forward, I'm required to inform you.
   This is an attempt to collect a debt by a debt collector, and any
   information obtained will be used for this purpose."
  [pause 2-3 seconds — natural sentence boundary]
  "Please also be aware that this call may be recorded for training and
   quality purposes. I can see three accounts associated with your
   profile..."
```

The pause after "...used for this purpose." is ~2.5-3 seconds.

What happens with `utterance_end_ms=2500 + grace=1500`:

```
T=0       Bot finishes "...used for this purpose."
T=2500    Deepgram fires UtteranceEnd (silence reached threshold)
T=2500    VJudge starts 1.5s grace period, watching for resume
T=2800    Bot resumes: "Please also be aware..."
T=2800    STT interim fires → _speech_started=True (grace catches it ✓)
          → loop back to wait for next UtteranceEnd
```

This case **works** because the bot resumed within the grace window.

But if the bot's pause is longer than `utterance_end_ms + grace = 4000ms`:

```
T=0       Bot finishes sentence
T=2500    UtteranceEnd fires
T=2500    Grace 1.5s starts
T=4000    Grace expires with no resume detected
T=4000    VJudge declares "turn ended" — calls Gemini → caller speaks
T=4200    Bot resumes speaking → caller's audio collides → BARGE
```

We've observed pauses up to ~3s consistently. Bumping `utterance_end_ms`
fixes barging in exchange for **+1.5s latency on every real turn end**
because the same threshold also gates the case where the bot truly is done.

### Why this approach has a ceiling

Deepgram's UtteranceEnd is fundamentally **silence-duration based**. It
cannot distinguish:

- *Bot is done* (long silence) — we should reply
- *Bot is pausing for breath / sentence boundary* (long silence) — we should wait
- *Bot's TTS is buffering / generating next chunk* (long silence) — we should wait

All three look identical to a silence-detection model. The only signal
that distinguishes them is **future audio**: did the bot resume? Our
grace period checks exactly that, but extending it costs latency on
every real turn end.

### Endpointing doesn't help with barging

A common misconception: bump `endpointing` from 300ms → 1500ms to "make
vjudge wait longer for the bot."

This is **wrong**. `endpointing` only controls when transcripts get
finalized — when interim→final transitions happen. It does **not** affect
UtteranceEnd, which is what triggers our turn-end logic. Bumping
endpointing gives cleaner transcripts (one big final per sentence instead
of fragments). It does nothing for barging.

To fix barging via Deepgram alone, you have to bump
`utterance_end_ms` (and/or `GRACE_PERIOD_S`), accepting the latency cost.

### Where audio-domain VAD (e.g., Silero) wins

A local VAD model classifies each audio frame as speech-or-silence based
on **audio energy + pitch + spectral patterns**, not just silence
duration. It runs in-process (~5ms per frame) — no WebSocket roundtrip.

```
With Silero replacing Deepgram UtteranceEnd:

T=0       Bot stops speaking
T=250     Audio reaches VJudge via WebSocket A
T=255     Silero processes (CPU-local)
T=305     Silero confirms 50ms of silence → "turn ended"
T=305     Caller starts thinking
```

**Theoretical floor: ~300ms instead of ~4500ms** for the silence-detection
step. The Gemini call (2-3s) and TTS (0.3-0.8s) are unchanged. So
per-turn floor drops from ~7-8s to ~3-4s — roughly 2x faster.

The trade-off is:

- ~700MB PyTorch dependency added
- Additional CPU cost per call (small — <5% on modern hardware)
- New failure modes (model false-positives on background noise, hum, etc.)

---

## Current open issues

| # | Issue | Impact | Root cause |
|---|---|---|---|
| 1 | Bot can't parse our spoken digits (SSN, ZIP, DOB) | Auth flow blocked on every digit turn — bot says "Sorry I wasn't able to get that" repeatedly | Bot-side STT issue, not vjudge's. Possibly aggravated by Aura-2 voice rendering of digits |
| 2 | Per-turn latency ~7-10s (default path) | Bot occasionally gives up and exits with "Are you still there?" | Fundamental limit of Deepgram-based endpointing (4.5s) + Gemini Flash (2-3s). **Mitigated by Silero VAD (toggle on via `VJUDGE_USE_SILERO_VAD=true`) → ~3-4s expected.** |
| 3 | Mid-turn pauses occasionally barge (default path) | Caller speaks while bot still has more to say → bot hears interruption | `utterance_end_ms + grace = 4000ms` is at the edge of observed bot pauses (~3s). **Mitigated by Silero VAD — audio-domain detection doesn't trigger on silence alone.** |
| 4 | Bot tool errors cascade | Session terminated mid-flow, bot loops on "Are you still there?" | Resurgent backend tool bug (`get_customer_names_by_ani` returns "Session terminated") |
| 5 | Caller LLM occasionally engages past Mini-Miranda | Test runs longer than needed, new failure modes possible | Persona prompt edge cases — fixed per-scenario via "END-OF-TEST TRIGGER" + "NO DTMF" sections |

---

## Potential improvements

### Implemented

| Change | Where | Toggle |
|---|---|---|
| **Silero VAD as turn-end signal** (replaces Deepgram UtteranceEnd) | `vjudge/transport/vad.py`, integrated in `_wait_for_bot_turn` Phase 2 | `VJUDGE_USE_SILERO_VAD=true` in `.env`. Default false (legacy path). |

When Silero VAD is enabled, Phase 2 of `_wait_for_bot_turn` polls the
local Silero model's `time_since_voice_s()` instead of awaiting
Deepgram's `UtteranceEnd`. Threshold: 600ms of confirmed silence at the
audio level declares the bot turn ended. Bot's mid-sentence pauses keep
refreshing the VAD's last-voice timestamp, so they don't trigger
turn-end. Deepgram STT keeps running for transcript text — only the
turn-end signal is replaced.

Expected effect (target, not yet validated on live calls):

- Silence-detection floor: **4.5s → ~0.6s**
- Per-turn floor: **~7-10s → ~3-4s**
- Barging on mid-turn pauses: eliminated (audio-domain detection)

### Not yet implemented

| Change | Effort | Expected gain |
|---|---|---|
| Per-scenario `stt.grace_period_ms` knob | 30m | Per-scenario tuning of barge-prevention vs latency trade-off |
| Streaming Deepgram TTS (SSE) | 4-6h | ~0.5s saved per turn (TTS audio starts arriving before full synthesis) |
| Switch caller to `gemini-2.5-flash-lite` | 5m + risk | ~1-2s saved per turn, more JSON parse failures expected |
| Pre-cache common opening phrases | 1h | ~2s saved on greeting turn only |
| ONNX-only Silero (drop torch dep) | 2h | ~250MB disk saved; no latency change |
