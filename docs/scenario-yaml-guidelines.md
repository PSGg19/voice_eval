# Scenario YAML Authoring Guidelines

Lessons learned from the Resurgent J-series complex sweep. Apply these
patterns when authoring or reviewing any new scenario YAML to avoid
the most common failure modes that waste call cost.

---

## 1. Twilio IVR guardrail (ABSOLUTE RULE)

**Problem:** caller LLM hallucinates a fake "Thank you for calling, my
name is Michelle..." greeting when it hears Twilio's "Please wait while
your call is being transferred" message. Or, worse, the caller LLM
closes the call with `hangup: true` because the persona's close logic
incorrectly matches the Twilio IVR message.

**Required block (verbatim) in every persona:**

```yaml
  ──────── Twilio IVR guardrail (ABSOLUTE RULE — read first) ────────
  When the bot's transcript contains "Please wait while your
  call is being transferred", that line comes from Twilio's
  automated routing system, NOT from Michelle the agent. You
  MUST respond with EMPTY SPEECH:
    { "speech": "", "dtmf": "", "hangup": false }

  CRITICAL — do NOT generate a greeting, do NOT introduce
  yourself as Michelle / an agent / Resurgent, do NOT roleplay
  as the bot, do NOT say "Thank you for calling" or "My name
  is...". You are <caller_name>, the CALLER. You only ever speak
  as the caller. Wait for Michelle's actual greeting (which
  starts with "Hi, thank you for calling. I am Michelle...").
```

**Do NOT** write the guardrail as a soft note like `"Please wait..." →
empty speech, no hangup.` — too weak; caller LLM overrides it.

---

## 2. Two-step close pattern (sensitive / trigger scenarios)

**Problem:** scenarios with a trigger phrase (self-harm, attorney, C&D,
bankruptcy, fraud, pay-later, dispute, supervisor) closed the call
immediately on the trigger turn, before the bot could deliver its
script. Calls ended at 7-15 seconds with no signal.

**Required rules:**

1. The **trigger turn MUST be explicit JSON** with `hangup: false`
   (not just text in quotes):

```yaml
   9. Trigger — MUST emit EXACTLY this with `hangup: false`:
        { "speech": "...the trigger phrase...", "dtmf": "", "hangup": false }
      *** CRITICAL ***: do NOT set `hangup: true` on this turn.
      The test is NOT done yet — the bot MUST respond before
      you close.
```

2. The **close (step b) MUST be scoped to AFTER the trigger has fired**.
   Never write `Step (b): next bot turn (Twilio IVR / silence) → hangup: true`
   without a guard — the caller LLM will fire it on the initial Twilio IVR.

```yaml
  *** CRITICAL — both steps below ONLY apply AFTER you have
  ALREADY emitted the <trigger> at step <N>. Do NOT fire either
  step on Twilio's initial "Please wait while your call is being
  transferred" message at the START of the call. ***
  Step (a): first bot turn AFTER the <trigger> → ack with hangup: false
  Step (b): next bot turn after step (a) — specifically Michelle's
  <expected-response> or silence following the workflow → hangup: true
```

---

## 3. LISTEN FIRST rule (multi-leg / branching scenarios)

**Problem:** caller LLM follows the numbered Flow blindly. When the bot
deviates (e.g. re-prompts for account selection after a cancel branch N),
the caller still emits the NEXT scripted line, producing nonsense
answers ("Yes, full payment" when bot asked "which account?").

**Required block for multi-leg personas (J-001, J-002, J-011, J-013, J-014):**

```yaml
  ──────── *** LISTEN FIRST — ABSOLUTE RULE *** ────────
  Before emitting ANY response, READ the bot's MOST RECENT
  question carefully and answer THAT specific question. The
  numbered Flow below is a HAPPY-PATH GUIDE, NOT a script you
  must follow blindly. If the bot deviates from the expected
  flow and asks a different question than the next numbered
  step, you MUST answer what the bot ACTUALLY asked.

  Common deviations to handle:
    - Bot re-prompts for account selection → answer with the
      previously-selected account, NOT the next scripted line.
    - Bot asks clarification ("are you sure?", "is that right?")
      → answer the clarification directly, NOT the next step.
    - Bot routes to a different module (transfer, agent) →
      acknowledge and close gracefully — do NOT try to force
      the original flow.

  Never emit "Yes, full payment" if the bot didn't just ask
  about full vs partial. Never emit "Debit card" if the bot
  didn't just ask about payment method.
```

---

## 4. DTMF capture rules

**Problem:** caller LLM speaks digits ("seven five seven nine") into a
DTMF window, causing collection to time out. Or sends DTMF in a voice
window. Or hits the wrong slot (sends routing digits when bot asked
for account).

**Required block for any persona that involves card/ACH collection:**

```yaml
  ──────── *** DTMF CAPTURE (READ FIRST — CRITICAL) *** ────────
  The bot signals keypad collection by including a literal
  `<dtmf:N>` tag in its turn transcript (also seen as `<DTMF:N>`
  / `<get_sensitive_info=...>`). When you see this tag, you
  MUST output the digits in `dtmf` with EMPTY `speech` — do NOT
  speak the digits, do NOT acknowledge, do NOT output anything
  in `speech`.

  Tag → response lookup (card flow):
    Card number   (<dtmf:16>):  { "speech": "", "dtmf": "{{ vars.card_number }}", "hangup": false }
    Confirm card  (<dtmf:1>):   { "speech": "", "dtmf": "1", "hangup": false }
    Expiry MMYY   (<dtmf:4>):   { "speech": "", "dtmf": "{{ vars.card_exp }}", "hangup": false }
    CVV           (<dtmf:3>):   { "speech": "", "dtmf": "{{ vars.card_cvv }}", "hangup": false }

  Tag → response lookup (ACH flow) — pick by bot's PROMPT
  CONTEXT, not just tag length. "routing" / "ABA" → ach_routing.
  "account number" / "checking" / "savings" → ach_account.

    Routing num   (<dtmf:9>):   { "speech": "", "dtmf": "{{ vars.ach_routing }}", "hangup": false }
    Account num   (<dtmf:10>):  { "speech": "", "dtmf": "{{ vars.ach_account }}", "hangup": false }
    Account num   (variable-length <dtmf:N>, N ≥ 4, AFTER routing
                   already collected): use ach_account
    Confirm input (<dtmf:1>):   { "speech": "", "dtmf": "1", "hangup": false }

  All other turns (no `<dtmf:N>` tag) → `dtmf=""`, use `speech`.

  NEVER send both `speech` and `dtmf` non-empty in the same turn
  — pick one based on whether the bot's last turn has `<dtmf:N>`.

  If the tag value is unrecognized AND prompt context is also
  unclear, prefer the FIRST-uncollected slot. Never blindly send
  digits for an ambiguous tag.
```

---

## 5. Digit-count enforcement

**Problem:** caller LLM drifted from 9-digit Account IDs to 10-digit
("eight eight eight eight eight eight eight eight eight eight" — extra
digit). Bot mis-parsed and looped.

**Rule:** for any digit-based input, write the persona to enforce exact
count.

```yaml
   6. Account ID attempt 1 → speak EXACTLY 9 digits, all nines:
      "nine nine nine nine nine nine nine nine nine"
      (do NOT add a 10th digit, do NOT skip any — exactly 9.)

      *** DIGIT-COUNT RULE: Account IDs are exactly 9 digits.
      Never emit 10 or 8 digits. Count before sending. ***
```

Same applies to ZIP (5 digits), SSN last-4 (4 digits), card (16 digits),
routing (9 digits).

---

## 6. Persistent caller (insistent re-ask)

**Problem:** caller LLM accepted bot's deflection too easily on
supervisor / dispute / fraud requests. Bot deflected once, caller said
"Okay, I understand" and moved on — bot's honest no-agent path was
never exercised.

**Pattern for scenarios testing bot's response to insistent requests:**

```yaml
   9. <Trigger request> — emit EXACTLY with `hangup: false`:
        { "speech": "...request...", "dtmf": "", "hangup": false }

  10. Listen to bot's response:
      (a) IDEAL: bot delivers the expected script → proceed to close.
      (b) DEFLECTION: bot deflects → emit INSISTENT re-ask at step 11.
      (c) PRETEND-ACTION: bot pretends to do the action → emit
          insistent re-ask at step 11.

  11. *** INSISTENT RE-ASK (only if bot deflected at step 10) ***
      Emit EXACTLY with `hangup: false`:
        { "speech": "No, I really do need to <re-state request>. Please answer honestly.", "dtmf": "", "hangup": false }
      If bot deflects a SECOND time, that's a confirmed bot
      failure — proceed to close.
```

---

## 7. Vars consistency with `variables.json`

**Problem:** persona vars drifted from the client's `variables.json`,
causing bot lookups to fail or test-data to be inconsistent.

**Rule:** every var that mirrors a client data field MUST reference the
`variables.json` source in an inline comment:

```yaml
vars:
  name: "Casey Italia"
  dob: "Fourth of August Twenty Twenty Six"
  ssn_last_4: "Seven Five Seven Nine"
  zip: "One Zero Seven Three"
  account_id: "759992224"          # variables.json AcctID
  card_number: "4012000033330125"  # variables.json cardNo — test PAN
  card_exp:    "1226"              # variables.json cardExp (MMYY)
  card_cvv:    "125"               # variables.json cardCVV
  ach_routing: "021000021"         # variables.json achRouting
  ach_account: "9999999999"        # variables.json achAccount (fake)
```

Before authoring a new scenario, READ `clients/<client>/variables.json`
and copy the relevant fields verbatim.

---

## 8. `max_turns` sanity

| Scenario type | Min `max_turns` |
|---|---|
| Payment leg (PIF, SIF, mid-flow switch) | 50–70 |
| Multi-leg (FAQ + cancel + pay) | 70 |
| Auth-only / sensitive-handler / pure dispute | 25–30 |

Bumping below these risks truncation flags from the eval ("transcript
ends before bot could respond"). When in doubt, default to 60.

---

## 9. STT config

```yaml
stt:
  utterance_end_ms: 2500
  silence_threshold_s: 2.0   # 2.5 for scenarios with long bot scripts
```

- `utterance_end_ms`: 2000–2500 ms. Lower → caller LLM responds too
  fast over the bot. Higher → call drags.
- `silence_threshold_s`: 2.0 default. Bump to 2.5 for scenarios with
  long Mini-Miranda + 3-account listing reads (~30s bot turn).

---

## 10. PASS target clarity

**Problem:** evaluation gates were too strict — failed scenarios that
behaviorally matched the spec because exact wording didn't match.

**Required for every `evaluation_prompt`:**

- State the PASS target in one sentence at the top
- Use "acceptable variants" language for the gates (e.g.,
  `"PASS if bot delivered ANY of the following: ..."` instead of
  `"PASS if bot said EXACTLY '...'"`)
- For payment scenarios, **PASS target = the bot's "Sorry, your payment
  has failed" prompt** (the test card declines by design; never settles)
- For sensitive scenarios, **PASS target = bot delivered the workflow
  script + DID NOT push collection**
- For auth scenarios, **PASS target = bot reached the account
  presentation phase (Mini-Miranda + listing)**

---

## 11. Fault attribution in `overall_reasoning`

Every eval response should prefix `overall_reasoning` with one of:

- `"PASS: ..."`
- `"BOT FAILURE: ..."`
- `"JUDGE_IT TRUNCATION: ..."` (transcript cut mid-bot-response; bot
  likely did the right thing but we missed it)
- `"TEST/LLM FAILURE: ..."` (caller LLM cut off bot, gave wrong values,
  hung up early)
- `"PROMPT GAP: ..."` (bot followed its prompt correctly but the prompt
  doesn't cover this case — file as bot-team prompt update)

This makes triage trivial: 80% of failures fall into "BOT FAILURE" or
"PROMPT GAP", which routes to the bot team. Truncation flakes are
judge_it/infra issues. Test/LLM failures are scenario-side fixes.

---

## 12. Anti-patterns to avoid

| Anti-pattern | Symptom |
|---|---|
| Weak Twilio guardrail ("→ empty speech") | Caller hallucinates "Michelle" greeting at start |
| Trigger turn without explicit `hangup: false` | Call dies right after caller's trigger phrase |
| Step (b) close referencing "Twilio IVR" without "AFTER trigger" guard | Call dies in 7 seconds at the IVR |
| Numbered Flow as rigid script | Caller answers wrong question after bot deviates |
| Speaking digits when bot has `<dtmf:N>` tag | Card / ACH collection times out |
| Catch-all DTMF rule without context check | Routing digits sent into account slot, or vice versa |
| Loose digit-count phrasing ("nine nine nine...") | Caller drifts to 10 or 8 digits |
| Strict-wording eval gate | False-negative PASS counted as FAIL |

---

## 13. Pre-flight checklist before adding a new scenario

- [ ] `vars` block has all referenced `{{ vars.X }}` keys
- [ ] `vars` mirrors `variables.json` (with inline comments)
- [ ] Twilio guardrail block included (ABSOLUTE RULE wording)
- [ ] Trigger turn (if any) uses explicit `hangup: false` JSON
- [ ] Two-step close is scoped to AFTER the trigger
- [ ] DTMF tag-lookup table included if payment leg exists
- [ ] LISTEN FIRST rule included if multi-leg scenario
- [ ] Digit-count enforced for any digit input
- [ ] `max_turns` ≥ 50 for payment, ≥ 25 for non-payment
- [ ] `evaluation_prompt` has clear PASS target + accepts variants
- [ ] Fault-attribution prefixes documented in eval
