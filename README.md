# judge-it ⚖️

> **Autonomous AI Voice Agent & Conversational LLM Evaluation Framework**

`judge-it` is a high-performance evaluation engine designed for continuous benchmarking, quality scoring, and load testing of AI voice bots, conversational agents, and LLM pipelines.

---

## ✨ Features

- **🎙️ Telephony & Real-time Voice Transport**: Support for WebSockets, Twilio Media Streams, and LiveKit SIP trunks.
- **🗣️ Human-like Turn Simulation**: Multi-turn conversation orchestration with configurable LLM persona, barge-in detection, and DTMF signalling.
- **📈 Comprehensive Observability**: Latency profiling (P50/P95/P99), turn-by-turn STT/TTS fidelity scoring, LLM judge evaluation, and OpenTelemetry / Phoenix integration.
- **⚡ Concurrency & Load Profiling**: Worker-owned dialing, distributed runner lanes, and rate-limited load generation.
- **💻 Command-Line Suite**: Comprehensive CLI for running evaluation flows (`judge-it run`), linting scenario definitions (`judge-it lint`), and benchmark scenario generation (`judge-it bcg`).

---

## 🏗️ Architecture

```
                       ┌───────────────────────────────┐
                       │          judge-it CLI         │
                       └──────────────┬────────────────┘
                                      │
                       ┌──────────────▼────────────────┐
                       │    Scenario & Flow Engine     │
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

## 🚀 Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/PSGg19/judge-it.git
cd judge-it

# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Usage

```bash
# Lint a scenario YAML flow
judge-it lint clients/_examples/happy-path.yaml

# Run evaluation scenario
judge-it run clients/_examples/happy-path.yaml
```

---

## 📄 License

MIT License © 2026 PSGg19
