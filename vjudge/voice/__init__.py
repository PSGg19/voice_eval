"""P6 — voice at density: the decomposed stream.

The 1,572-line `MediaStreamSession` becomes a chain of processors with
bounded queues between links:

    audio-in → STT → turn-detect → engine bridge → TTS → audio-out
                    ↘ barge (watches partials, emits early caller turns)

* `frames.py`     — the voice-internal frame vocabulary (audio chunks, wire
                    lifecycle, speech signals). The ENGINE vocabulary stays
                    in `vjudge/frames.py`; these are chain plumbing.
* `processor.py`  — Processor + Chain: bounded inboxes (backpressure is
                    structural — a slow consumer blocks its producer at
                    `await put`, no code decides), lifecycle, and the task
                    registry that P6.5's reaping asserts against.

`transport/stream.py` remains the live path until the chain reaches golden-
baseline parity; each processor lands with an isolated frame-in/frame-out
test first.
"""
