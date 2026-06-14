# BCG Extraction Prompt (v0.1.0)

You are analyzing a voice-bot's system prompt to produce a **Barge Context
Document (BCD)**. The BCD will be used by a test caller LLM to decide when to
interrupt the bot during stress-testing.

Extract ONLY structural information useful for timing barges:

- **Modules** — the bot's high-level states / sections in the order they occur
  during a typical call.
- **Module transitions** — verbatim phrases the bot uses to move between
  modules. These are barge targets for module-switch tests.
- **Tool-call moments** — phrases that indicate the bot is about to do a
  backend lookup or external call (e.g. account lookup, card validation,
  payment processing). These are barge targets for tool-call tests.
- **Compliance requirements** — specific phrases the bot is required to say
  (e.g. mini-Miranda, recorded-line disclosure).

DO NOT include:

- The bot's verbatim system prompt
- Examples of dialog
- General behavior descriptions
- Anything not directly useful for barge timing

## Output format

Respond with STRICT JSON in exactly this shape (no markdown fences, no prose
before or after):

```
{
  "bcd_markdown": "<full markdown document — see structure below>"
}
```

Where `bcd_markdown` is a single markdown string with this structure (and no
other text):

```
# Barge Context — <Bot Name> (<Org Name>)

## Modules (in order)
1. <Module 1> — <one-line description>
2. <Module 2> — <one-line description>
...

## Module Transitions (barge targets for module-switch tests)
- <Module A> → <Module B>: "<phrase>", "<phrase>"
- <Module B> → <Module C>: "<phrase>"
...

## Tool-Call Moments (barge targets for tool-call tests)
- <Tool 1>: "<phrase>", "<phrase>"
- <Tool 2>: "<phrase>"
...

## Compliance Requirements (bot must say)
- <Requirement 1>: <details>
- <Requirement 2>: <details>

## Notes
<!-- (empty for first draft; engineers may add hand-curated notes here later) -->
```

If the bot's name or organization is not stated in the prompt, infer a
reasonable label from context (e.g. "the bot", "the agent"). If a section has
no applicable items, keep the heading and write `- (none identified)` so the
structure stays consistent across BCDs.
