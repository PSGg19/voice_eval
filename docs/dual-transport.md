# VJudge: one engine, two transports

Implementation spec for running the same scenario over **text** (OSV's text
runtime, HTTP) or **voice** (Twilio, existing). Rationale and coverage analysis
live in [platform-evolution.md §5d](./platform-evolution.md); this file is the
build plan.

## Why this is cheap

Two things already exist:

1. **`AICallerEngine` and `Evaluator` are transport-agnostic.** The engine emits
   `{speech, dtmf, hangup}` per turn; the evaluator scores a transcript. Neither
   touches audio. The only coupling is `orchestrator/runner.py` hardcoding
   `MediaStreamSession`.
2. **OSV already drives a VOICE-channel bot with text**, in 67 lines —
   `app/runtime/cli/main.py`:

```python
config_manager = LLMConfigManager(
    db=db, assistant=assistant, assistant_config=assistant_config,
    recipient_id=recipient_id, virtual_id="100", vendor_call_id="cli-session",
    channel=Channel.VOICE,          # ← the voice flow, no audio
    is_test=True,
)
pipeline_manager = TextPipelineManager(config_manager, streaming_mode=True)
await pipeline_manager.create_pipeline()

initial = await pipeline_manager.process_message("")     # bot greeting
while True:
    response = await pipeline_manager.process_message(user_input)
```

`process_message(text) -> text` is the turn-level API vjudge needs. That is the
whole transport, and it's already proven in-tree.

## The seam

```python
# vjudge/transport/base.py
class Transport(Protocol):
    async def start(self)  -> BotTurn: ...                  # open the conversation, return greeting
    async def send(self, turn: CallerTurn) -> BotTurn: ...  # caller speech/dtmf → bot reply
    async def close(self) -> None: ...
    capabilities: TransportCapabilities

@dataclass(frozen=True)
class CallerTurn:
    speech: str = ""
    dtmf:   str = ""
    hangup: bool = False

@dataclass(frozen=True)
class BotTurn:
    text: str
    ended: bool = False
    # text transport only — None on voice
    current_module:   str | None = None
    tool_calls:       list[dict] | None = None
    reasoning_ms:     float | None = None
    # voice transport only — None on text
    turn_latency_ms:  float | None = None
    barged:           bool = False

@dataclass(frozen=True)
class TransportCapabilities:
    audio:             bool
    barge:             bool
    dtmf_timing:       bool   # collection timeouts, partial entry
    turn_latency:      bool   # bot_turn_latency measurable
    module_visibility: bool   # current_module / tool_calls available
```

`send()` is a natural fit for text — an HTTP request/response *is* a turn. For
voice it wraps what `stream.py` already does (TTS out → wait for turn end →
return transcript), so **the audio machinery does not move.**

Barge is the one thing that doesn't fit turn-level, because it fires *during*
bot speech. Model it as an optional hook the engine registers and only the voice
transport ever calls.

## Text transport

`vjudge/transport/text.py`, ~150 lines. HTTP against
`POST /text_runtime/post_message_web`.

### Request mapping

| Field | Value |
|---|---|
| `assistant_uuid` | from the scenario payload |
| `channel` | **`"VOICE"`** — non-negotiable, see below |
| `is_test` | `false` by default — scenario `is_test:` wins, else `VJUDGE_TEXT_IS_TEST`. `true` hides the run from llm-studio and suppresses only the compliance event, so it is off by default |
| `recipient_id` | `VJUDGE_{scenario}_{run_uuid[:8]}` — mirrors imposter's `IMPOSTER_{test_id}_RUN_{run_id}` |
| `user_message` | `turn.speech`, or `turn.dtmf` when the bot asked for digits |
| `interaction_metadata` | the scenario's `vars` |
| `fc_version` | pin the assistant version |
| `interaction_uuid` | omitted on turn 1; echoed back on turns 2..N |

### Response mapping

| Field | Use |
|---|---|
| `output_message` | `BotTurn.text` |
| `interaction_uuid` | **capture on turn 1** — this is the correlation key |
| `current_module` | `BotTurn.current_module` → module assertions |
| `tool_calls` | `BotTurn.tool_calls` → tool assertions |
| `total_response_time`, `function_times` | `BotTurn.reasoning_ms` |
| `message_type` | `SILENCE` / `NO_RESPONSE` / `RECORD` / `DEFAULT_MESSAGE` → end/idle detection |

**There is no way to close the conversation.** An earlier draft of this doc said
to use `POST /text_runtime/update_interaction_conversation_status/`; that is
wrong. Its request requires `vendor_call_id` and carries `status_long_code` /
`total_message_segments` / `created_reply_uuid` — it is the SMS/email
**delivery-status** hook, and vjudge has no `vendor_call_id`. (Checked against
`llm-studio-frontend`'s generated schema, `UpdateInteractionConversationStatusRequest`.)

Consequence: every text run leaves its interaction at `status=ongoing`, showing
as **live forever** in llm-studio, one dangling row per run — passes included.
Over voice, hanging up ends the call and OSV finalises it; the text runtime has
no equivalent, so the caller LLM's `hangup` is a client-side signal only.
Fixing this needs a new OSV endpoint — approval required before writing one.

### `channel=VOICE` is mandatory

An assistant has **four separate flow configs**
(`app/models/assistant.py:187-190`): `voice_flow_config`, `sms_flow_config`,
`email_flow_config`, `web_chat_flow_config`. `get_channel_flow`
(`app/models/utils.py:29`) filters `FlowConfig.channel == channel` and returns
**`None` with no fallback**.

So `channel=WEB_CHAT` against a voice bot exercises a *different flow* — or
nothing. Always send `VOICE`; treat a `None` flow as a hard failure, never a
fallback. The CLI does exactly this, which is the precedent.

### DTMF works in text

Trace how digits reach the model:

```
bot emits "<DTMF:16>"
  → TagsFrameProcessor._handle_dtmf_tag  (voice/processors/tags.py:424)
    strips the tag, pushes StartDTMFFrame(max_digits=16)
  → [voice] DTMFFrameProcessor collects keypresses, then
            _finalize_dtmf_input pushes them as a TranscriptionFrame (dtmf.py:232)
  → the LLM sees the digits as ordinary user text
```

**DTMF input is already just text to the model**, so posting `"1234…"` as
`user_message` is identical to a real keypad entry. And `TagsFrameProcessor` is
shared — `text_bots.py:155` instantiates the same class — so `<DTMF>`, `<LANG>`,
`<TRANSFER>`, `<HANGUP>` all fire in text mode.

In text nothing consumes `StartDTMFFrame`, so the tag is stripped and the response
carries no explicit "DTMF expected" flag. **That doesn't matter** — the caller LLM
decides from the bot's words, exactly as it does over voice, where no such signal
exists either. See "No OSV changes required" below.

## Voice transport

`vjudge/transport/voice.py` — a thin adapter over the existing
`MediaStreamSession`. `send()` = `_speak()` + `_wait_for_bot_turn()`. Barge stays
inside, driven by the engine's hook.

**Do not extract the shared turn loop first.** `stream.py:_conversation_loop`
holds the smart-grace turn detection and barge scheduling — the two most delicate
behaviours in the codebase. Give the text transport its own ~40-line loop, ship
it, and unify later if the duplication actually hurts. Risking a working audio
path to avoid 40 duplicated lines is a bad trade.

## What actually works today — measured, 2026-08-24

Text mode is sound. It is blocked on exactly ONE runtime gap, and that gap sits
at the front of every collections flow.

**The gap.** The voice runtime fills the flow variable `phone_number` from the
call's caller ID. The text runtime does not — it sets `ani` and leaves
`phone_number` empty. From a failing Veros interaction's own variable dump:

```
"ani":             "7145551212"
"phone_number":    ""            <-- never populated on text
"isAuthenticated": "FALSE"       <-- while current_module is already EXPLAIN_DUE
```

Identity verification compares against `phone_number`, so it can never pass.
Meanwhile `module_switch` has already moved the bot into `EXPLAIN_DUE`, which
then runs its unauthenticated branch and **emits no text**. The LLM returns an
empty completion (`livekit.py:264 llm returned empty response after all
retries`), and the request hangs to the ~301s idle timeout on assistant 1734, or
returns empty in 12s on 1354. Nine reproductions; identical on both assistants
and on flow-config versions 9 and 151, which is what rules out the import.

**So the line is: does the flow read caller-identity variables?**

| | works on text today | why |
|---|---|---|
| SBI Sia (`yono_general`) | ✅ verified PASS | no identity path, no account lookup; modules answer from a knowledge base |
| Veros scenarios that never authenticate | ✅ verified PASS | LIVE-05/07/17/20 — never reach the gated module |
| Veros past authentication | ❌ blocked | needs `phone_number`, which text leaves empty |

Verified on SBI: FAQ answers, and 3 silent module hops in one conversation —
`yono_general → credit_card_yono → cheque_services → fd_rd_deposit →
yono_general`. Module-path assertions are the thing text mode gives you that
voice cannot.

**One OSV change unlocks the rest**: populate `phone_number` from the caller
identity on the text runtime, as voice does. Nothing on the vjudge side can
substitute for it.

**Region is a path segment**, and the assistant resolves from `virtual_id` — no
`assistant_uuid` needed:

```
US     https://llm-studio.vjudge.dev/us/api/text
India  https://llm-studio.vjudge.dev/in/api/text   <-- verified working
```

Sending an India number to the US gateway returns a clean
`404 No assistant found for request.assistant_uuid=None and +91…`, which is how
to confirm a region mismatch.

**`recipient_id` length is capped per deployment.** OSV derives a longer
composite key and stores it in a `varchar(100)`, so an over-long id fails the
INSERT — arriving as HTTP **200** with
`DataError(StringDataRightTruncation)` and `current_module="error"`. Bisected:
US accepted 56 characters, India failed at 49 and passed at 45. The generated id
is therefore capped at 40 total. Another reason `_check_response` must inspect
the body rather than the status code.

## Capability matrix

| | text | voice |
|---|---|---|
| Flow, modules, edges, `general_instruction` | ✅ (`voice_flow_config`) | ✅ |
| LLM provider/model, per-module config | ✅ | ✅ |
| Tools + MCP through PIS (**really execute**) | ✅ | ✅ |
| `variables`, compliance config, guardrails | ✅ | ✅ |
| Tags: `<DTMF>` `<LANG>` `<TRANSFER>` `<HANGUP>` | ✅ | ✅ |
| `is_test` | ✅ request field | ❌ always live |
| `interaction_uuid` | ✅ returned turn 1 | ❌ needs SIP header or reconciliation |
| `current_module` / `tool_calls` | ✅ | ❌ |
| DTMF *logic* (request + digit validation) | ✅ | ✅ |
| DTMF *mechanics* (timeouts, partial entry) | ❌ | ✅ |
| STT mishearing | ❌ | ✅ |
| TTS / pronunciation | ❌ | ✅ |
| Barge-in | ❌ | ✅ |
| Turn-taking, VAD, `user_idle` | ❌ | ✅ |
| `bot_turn_latency`, carrier path | ❌ | ✅ |
| Twilio concurrency load test | ❌ | ✅ |

Scenario declares what it needs; lint rejects impossible combinations **before
dialing**:

```json
"transport": "text",
"barge_in": { "enabled": true }     // → 422: text transport has capabilities.barge = false
```

Run one scenario on both with `"transports": ["text", "voice"]` — two runs, two
`run_uuid`s, comparable verdicts. **A disagreement is itself a finding**: the bug
is in the audio layer, not the reasoning.

## What text mode adds: deterministic assertions

`current_module` and `tool_calls` per turn mean assertions that don't need an
LLM. **Implemented** — the key is `assert:` in the scenario YAML:

```yaml
assert:
  must_visit_modules:     ["auth_dob", "auth_ssn", "mini_miranda"]
  must_not_visit_modules: ["card_collection"]
  must_call_tools:        ["get_accounts_by_additional_info"]
  must_not_call_tools:    ["get_design_your_own_payment_plan_options"]
  tool_order:             ["get_account_scheduled_payments", "get_account_payment_options"]
  max_tag_only_turns:     0
```

> **Not `assertions:`.** An earlier draft of this document proposed that name.
> It is a V1 **legacy key that `lint.py` rejects outright**, so a scenario using
> it fails before it runs. Corrected 2026-08-25.

This matters beyond convenience. VJudge's verdict otherwise comes entirely from
an unvalidated LLM judge. Assertions take the structural half of that judgement
away from the model — the I483617082 failure (pre-flight skipped, wrong tool
called after an error) is caught by `tool_order` with no LLM involved.

Three semantics worth knowing before you write one:

* **A failure vetoes the judge**, via `CallResult.passed`. A conversation that
  reads beautifully but skipped an auth module is a failure.
* **Unobserved is not violated.** Tool calls come only from the post-run OSV
  fetch, so with no token they are INCONCLUSIVE — which does not fail the run.
  Treating an empty list as "called nothing" would fail every run for a
  configuration reason. `must_not_call_tools` is INCONCLUSIVE too rather than a
  free pass: an unproven safety claim is not a pass.
* **`tool_order` is a subsequence**, gaps allowed. Real flows interleave other
  calls, so requiring adjacency would make it unusable.

The veto is one-directional: passing assertions never rescue a failed LLM
verdict.

### …but the HTTP response supplies neither, so we fetch them

Measured, not assumed. `POST /text_runtime/post_message_web` accepts a
`tool_calls` field and **never populates it** — the Veros IB 2.0 runs did an
account lookup and came back with `tool_calls: []`. Separately, OSV strips
marker tags server-side *before* filling `output_message`, so a completion of
nothing but `<LANG:en-US>` arrives as `""` — **indistinguishable from the bot
saying nothing at all**. That is precisely the IB 2.0 stall, and it is why
diagnosing it needed a Metabase query rather than an vjudge log line.

Both survive in `GET /client/{c}/interactions/conversations/{i}/`, so vjudge now
fetches it once after every text run
(`TestRunner._enrich_text_result_from_osv`) and fills:

| Field | From | Gives you |
|---|---|---|
| `CallResult.tool_calls` | stored `tool_calls`, in turn order, each tagged `_turn_index` | `must_call_tools`, `tool_order` |
| `CallResult.tag_only_bot_turns` | bot turns whose raw text is only tags | *"the bot went silent, and here is the tag it sent instead"* |

Properties worth keeping if you touch this:

* **It runs in `finally`, not on the success path.** The IB 2.0 hang ends in a
  `ReadTimeout` → `TextTransportError`; enriching only on success would miss
  every case worth explaining. A source-inspection test pins the placement.
* **It cannot fail a run.** No token, no `client_uuid`, a 401, a timeout, an
  unfamiliar payload shape — all log and return. The call already completed; a
  diagnostic must not retroactively break it.
* **Shape tolerance is deliberate**, not sloppiness. No one has ever pinned this
  payload's shape (`jobs/pipeline` hands it to an LLM verbatim), so the envelope
  key, role key and message key are each accepted in several spellings.
* **Token**: `VJUDGE_OSV_ACCESS_TOKEN`, falling back to
  `VJUDGE_TEXT_RUNTIME_TOKEN` **only when both base URLs are the same host**.
  In practice both are one llm-studio JWT from the same browser session, so
  without the fallback the feature ships switched off; but the text runtime
  defaults to `localhost:8001` and a local token must not be sent to
  llm-studio.

The tag-only line is the one to look for. It is the difference between "vjudge
is broken" and "the bot is broken" — and on voice the same reply is dead air
the caller talks over, so it is a defect on both transports.
Leave the evaluator to the genuinely subjective parts: tone, compliance wording,
hallucination.

## No OSV changes required — verified live

Tested against the running text runtime (`orchestrator-service`, port 8001) on
2026-08-05, assistant `Imported - Resurgent IB`
(`08432036-470c-4941-98b3-2b12c27fcef5`), which has a `voice_flow_config` and no
`web_chat_flow_config` — a clean A/B.

```
POST /text_runtime/post_message_web
{ "assistant_uuid": "08432036-…", "recipient_id": "VJUDGE_probe_voice_…",
  "user_message": "", "channel": "VOICE", "is_test": true, "virtual_id": "100" }
```

| | `channel=VOICE` | `channel=WEB_CHAT` |
|---|---|---|
| HTTP | 200 | 200 |
| `output_message` | *"I am Michelle… from Resurgent Capital Services on a recorded line…"* | *"I encountered an error processing your message"* |
| `current_module` | **`GREETINGS`** ← voice flow's start module (id 8) | `error` |
| `flow_config_version` | **9** ← `voice_flow_config` | `null` |
| `interaction_uuid` | returned on turn 1 | `null` |
| `is_test` | `true` | `false` |
| `error` | *(empty)* | `TypeError("'NoneType' object is not subscriptable")` |

*(This probe sent `is_test: true`, which is why the column reads `true`. The
transport now defaults to `false` — see the request mapping.)*

**`channel=VOICE` works today.** Text mode drives the real voice flow, and
`interaction_uuid` + `is_test` both behave as designed. Nothing needs adding to
the orchestrator.

### The DTMF signal isn't needed either

An earlier draft asked for `message_type: "DTMF"` + `max_digits` in the response.
**Retracted** — vjudge's caller already decides DTMF from the bot's words. From
`clients/resurgent/scenarios/pif/C-002-debit-card-happy-path.yaml`:

```
14. Bot asks for CARD NUMBER via keypad.
15. *** DTMF *** send card:
      { "speech": "", "dtmf": "{{ vars.card_number }}", "hangup": false }
16. Bot ... asks "press 1 to confirm or 2 to re-enter". Send DTMF "1"
      (If bot asks via voice instead of DTMF, respond "yes".)
```

Over voice there is no structured DTMF signal available to vjudge either — the
caller LLM reads the bot's speech and decides. That works identically in text
mode, and better, since vjudge reads the text directly rather than through STT.
`current_module` (e.g. `CARD_PAYMENT`) is a second signal already present.

### The bar for changing the orchestrator

An OSV change is justified only when **all three** hold:

1. The data is genuinely unavailable through any existing interface — not merely
   less convenient.
2. It is load-bearing: the capability fails without it, rather than getting nicer.
3. No workaround exists on vjudge's side.

Applied to everything previously proposed:

| Proposed change | Verdict |
|---|---|
| `channel=VOICE` support | **not needed** — works today |
| `message_type="DTMF"` + `max_digits` | fails all three — caller LLM infers from prose |
| Missing flow → 4xx instead of 200 | fails #2/#3 — vjudge validates the body. Report as a bug, don't block |
| `interaction_uuid` injection / SIP header | not needed for text; PSTN-only, and reconciliation is a workaround |
| Provider capability fields | fails #3 — vjudge holds its own |
| `is_test` for voice calls | only one that passes #1/#3, and only once PSTN volume matters. Caller-ID whitelist is config, not code |

**Net: the text transport is entirely vjudge-side work.**

## Four gotchas the live test surfaced

Not blockers, but the text transport must handle all four.

1. **A wrong channel returns HTTP 200, not 4xx.** `get_channel_flow` returns
   `None`, `flow_config_data["start_module_id"]` raises, and the view swallows it
   into an apology message. **Validate `error == ""` and
   `current_module != "error"`, not just the status code.** (That probe also saw
   `is_test` come back `false` on the failure. Do *not* use that as a tell — it
   was only a signal because the probe sent `true`, and the default is now
   `false`. `current_module` is the reliable one.)
2. **Reusing a `recipient_id` continues the previous conversation.** Two probes
   sharing one got the same `interaction_uuid` back, with the bot repeating its
   greeting. A **unique `recipient_id` per run is mandatory** —
   `VJUDGE_{scenario}_{run_uuid[:8]}`.
3. **Integrations really fire.** The second probe's
   `error_messages.integration_error` showed an HTTP 404 against a dead ngrok
   tunnel. Live confirmation that text mode executes the real tools through PIS —
   `is_test` does **not** protect the client's backend. Test accounts or a PIS
   sandbox remain a prerequisite for running the `pif/` scenarios at volume.

4. **A stalled turn burns ~301s and then returns an empty reply, HTTP 200.** The
   text runtime builds a fresh Pipecat pipeline task *per request*, runs one
   turn, and tears it down. If that turn produces no frames — a hung LLM call, a
   tool that never returns — nothing ends the request except Pipecat's idle
   timeout. Measured twice on Veros IB 2.0 v128 (assistant 1734), to the second:

   | interaction | caller turn sent | `worker.py:1261 idle timeout detected` | elapsed |
   |---|---|---|---|
   | `a10b2719-…` | 17:09:22 | 17:14:23 | 301s |
   | `389777e3-…` | 17:10:59 | 17:16:00 | 301s |

   The handler then logs `returning response - conversation_uuid from config:
   none` and answers with **no bot message** — so waiting the full 301s buys
   nothing.

   **Root cause, isolated 2026-08-18 over 5 reproductions.** The terminal event
   is the LLM returning an empty completion and the runtime giving up:

   ```
   livekit.py:253  WARNING  llm returned empty response, retrying  attempt=1..3  model=google/gemini-2.5-flash
   livekit.py:264  ERROR    llm returned empty response after all retries  total_attempts=4
   ```

   Counted across a whole day of runs, `after all retries` appears **exactly 3
   times — once per hang, and never in a run that completed.** A perfect 1:1
   correlation, so that is the cause.

   Note what it is NOT. The same turns also log:

   ```
   frame_processor.py:697  ERROR  modllmuseraggregator#N exception (…/voice/pipeline/context.py):
     error processing frame: 'nonetype' object has no attribute 'min_words_interruption_enabled'
   ```

   That looks like the culprit and isn't: it fired **72 times during a campaign
   that mostly passed**. It is a separate, pervasive text-runtime bug — the user
   aggregator dereferences an *interruption* config that only the voice path
   populates, so it is `None` on every text turn — but it is survivable and not
   the trigger. Worth its own ticket; do not conflate the two.

   The differentiator is the account, not the module: 5/5 hangs on one live
   Veros account, 0/6 on the `1234568` test account, same assistant, same
   `EXPLAIN_DUE`. Something in that account's context makes Gemini return empty
   deterministically — a safety block or a finish_reason the runtime does not
   inspect. (The live account number is deliberately not recorded here — it
   identifies a real customer, and which account it was does not change the
   finding. It is in the interaction metadata if anyone needs to reproduce.)

   **The OSV bug worth escalating** is not the empty response itself but the
   handling: the runtime *detects* failure at `livekit.py:264` and then lets the
   HTTP request hang for another ~4.5 minutes instead of returning an error. That
   is what turns a 3-second failure into a 5-minute one. `text_runtime_read_timeout=90` is therefore right: fail fast, and
   **never retry**, because the user turn has already been committed (the SSN row
   is in `conversations` on both interactions). A stall is reported as a bot
   failure, which is what it is.

Bonus find: `interaction_metadata` already carries `grafana_url`, `sentry_url`
and `llm_studio_url`. The `debugging_urls` block proposed for vjudge's run record
already exists — carry it through rather than rebuilding it.

Also seen on every turn, both pods: `--- Logging error in Loguru Handler #1 ---`
(`loguru/_handler.py:164`, `_memoize_dynamic_format`) dumping the assistant's
entire four-channel flow config to stderr. An OSV logging bug — not ours, but it
is why the text runtime's Loki stream is enormous and slow to query.


## Not a path today

**The per-interaction event WebSocket.** `app/runtime/text/channel_websocket.py`
exposes `ws://…/websocket/{channel}/events/{interaction_uuid}` and the enum
includes `VOICE`, which looked like a way to get module visibility during a
*voice* call. But `publish_channel_event` is only ever called for **email and
sms** (`views.py:143,212,231,620,639`, `email_views.py:285`,
`sms_views.py:201`). The `voice_events:` and `web_chat_events:` channels are
scaffolding with no publisher. Worth revisiting if that changes — it would give
voice runs the same deterministic assertions text has.

## In-process vs HTTP

The CLI uses `TextPipelineManager` in-process. VJudge should use **HTTP**:
in-process would mean vjudge running inside OSV's codebase and venv, giving up
the independence that justifies a separate harness, and losing `is_test` /
`interaction_uuid` / `current_module` as a clean contract.

## Build order

1. `transport/base.py` — protocol + capability dataclasses.
2. `transport/text.py` — HTTP loop, its own turn loop, reusing `AICallerEngine`
   and `Evaluator` unchanged.
3. `transport` field in the scenario payload; dispatch in `runner.py`.
4. Lint: capability check (barge/DTMF-timing → voice only).
5. Deterministic assertions from `current_module` / `tool_calls`.
6. `transport/voice.py` — wrap `MediaStreamSession` behind the same protocol.
7. *Later, optional:* unify the two turn loops.

Steps 1-5 are the ~98% coverage win and don't touch the audio path at all.
