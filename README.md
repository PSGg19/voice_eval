# V-Judge 🎯

> **Autonomous AI Voice Agent & Conversational LLM Evaluation Engine**

V-Judge is an enterprise-grade framework for continuous evaluation, benchmarking, and automated testing of AI voice bots and LLM agents. It simulates human-like turn-taking, barge-in scenarios, DTMF signalling, latency analysis, and multi-turn conversational quality assessment.

---

## 🌟 Key Features

- **🎙️ Real-time Voice & Telephony Transport**: Full support for Twilio Media Streams, LiveKit SIP, and WebSockets.
- **⚡ Simulated Human Interaction**: Interactive multi-turn dialogs with configurable persona, barge-in (scheduled & LLM-triggered), and DTMF tone injection.
- **📊 Observability & Quality Metrics**: Turn-by-turn latency metrics, STT/TTS fidelity scoring, LLM judge evaluation, and Phoenix/Langfuse/Prometheus integration.
- **🚀 Scale & Load Testing**: Concurrency controls, worker-owned dialing, distributed test suites, and load profiling.
- **🛠️ Extensible CLI**: Rich CLI for running scenarios, linting flows, generation (`vjudge run`, `vjudge lint`, `vjudge bcg`).

---

## 🏗️ Architecture Overview

```
                        ┌───────────────────────────────┐
                        │          V-Judge CLI          │
                        └──────────────┬────────────────┘
                                       │
                        ┌──────────────▼────────────────┐
                        │      Scenario & Flow Engine   │
                        └──────────────┬────────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            │                          │                          │
  ┌─────────▼─────────┐      ┌─────────▼─────────┐      ┌─────────▼─────────┐
  │   Twilio / SIP    │      │    VAD / STT      │      │    LLM Evaluator  │
  │    Transports     │      │   Interactions    │      │    Judge System   │
  └───────────────────┘      └───────────────────┘      └───────────────────┘
```

---

## 🚦 Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/PSGg19/V-judge.git
cd V-judge

# Create virtual environment & install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configuration

Copy the example environment file:
```bash
cp .env.example .env
```
Edit `.env` to configure your LLM providers (e.g. `GEMINI_API_KEY`, `OPENAI_API_KEY`), database, and transport settings.

### 3. Run a Scenario

```bash
# Validate scenario YAML syntax
vjudge lint clients/_examples/happy-path.yaml

# Execute test scenario
vjudge run clients/_examples/happy-path.yaml
```

---

## 📜 CLI Reference

| Command | Description |
| :--- | :--- |
| `vjudge run <scenario.yaml>` | Execute an AI voice evaluation scenario |
| `vjudge lint <scenario.yaml>` | Validate scenario structure and syntax |
| `vjudge bcg` | Benchmark Scenario Generator CLI |
| `vjudge serve` | Start local API & web dashboard server |
| `vjudge load` | Execute concurrent load testing |

---

## 📊 Observability & Metrics

V-Judge integrates natively with:
- **Phoenix & Langfuse**: Trace conversation turns and LLM evaluation prompts.
- **Prometheus & Grafana**: Monitor call density, turn latencies (P50/P95/P99), and error rates.
- **SQLite / PostgreSQL**: Store structured run results, audio recordings, and transcripts.

---

## 📄 License

MIT License © 2026 PSGg19
