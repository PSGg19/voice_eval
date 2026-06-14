# Architecture

High-level layout of the `opkura` package post-Task-9 restructure.

## Layering

```
opkura/
├── cli/            # Typer subcommand handlers; thin wrappers over orchestrator + core
├── core/           # Models, scenario loader, lint, settings — no I/O dependencies
├── engine/         # AI caller, evaluator, barge triggers; pure logic over transcripts
├── transport/      # Twilio + Deepgram + WebSocket adapters
├── persistence/    # SQLite store, per-call log folder writer
├── llm/            # LLMClient Protocol + provider implementations
├── bcg/            # Barge Context Document generator
├── orchestrator/   # TestRunner — wires scenario → engine → transport → persistence
└── legacy/         # V1 (FlowEngine, load tester) — frozen
```

## Allowed dependency direction

```
cli  →  orchestrator  →  engine, transport, persistence, llm, bcg
                                    │
                                    └→  core
```

`core` has no deps on other opkura subpackages.
`legacy` is leaf — nothing imports from it except `orchestrator/runner.py` for
the V1 dispatch path.

## Tests

Tiered to match cadence:

```
tests/unit/         # fast, no I/O; mirrors opkura/<subpackage>/ structure
tests/integration/  # runner-level tests with stubbed transport / LLM
tests/e2e/          # live Twilio + LLM calls behind RUN_LIVE=1
```

(Fuller architecture write-up lands in a follow-up — this file is the scaffold.)
