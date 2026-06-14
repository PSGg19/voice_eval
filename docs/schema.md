# Scenario Schema

Condensed reference for the V2 scenario YAML. Full PRD lives in
`plan/docs/PRD-opkura-v2-ai-caller.md §7`.

## Required keys

- `name: str` — scenario label
- `target: str` — E.164 phone number
- `vars: dict` — facts the caller knows; substituted into persona / eval via `{{ vars.x }}`
- `persona: str` — caller-LLM system prompt
- `evaluation_prompt: str` — evaluator-LLM system prompt

## Optional keys

- `llm: { caller_model, evaluator_model, temperature, provider }`
- `max_turns: int` (default 30)
- `barge_in: { enabled, mode, ... }` — see below

## `barge_in` modes

### `mode: scheduled`

Deterministic triggers fire at specific turn numbers + word counts:

```yaml
barge_in:
  enabled: true
  mode: scheduled
  triggers:
    - at_turn: 3
      after_bot_words: 4
      say: "Wait, slow down a second."
```

### `mode: llm`

Caller LLM polled every `decide_every_ms` during bot speech; decides
whether to barge:

```yaml
barge_in:
  enabled: true
  mode: llm
  decide_every_ms: 500
  bot_context_path: "clients/<name>/bargin-context.md"   # optional BCD
```

Mode rules are mutually exclusive — `scheduled` rejects `decide_every_ms`
and `bot_context_path`; `llm` rejects `triggers`. `opkura lint` enforces
this at load time.

(Fuller schema reference with examples lands in a follow-up — this file
is the scaffold.)
