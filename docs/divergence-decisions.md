# The five transport divergences, decided (V3 Phase 2, P2.5)

Before frames, the text and voice paths drifted apart in five places — each an
accident of having two conversation loops, none a deliberate choice. Phase 2
resolved each one **once**, in the conversation engine or as a declared
adapter policy. This file is the record of what was decided and where each
decision lives; the tests named are the enforcement.

| # | Divergence | Decision | Lives in | Pinned by |
|---|---|---|---|---|
| D1 | The caller's final line was dropped on text (the loop broke before sending) but spoken on voice. | **Delivered on both.** The engine puts the parting speech and the hangup in ONE `deliver()`, speech first. Text POSTs the goodbye (bot-side interaction records it; the bot's reply is post-hangup noise, discarded). Voice speaks it before the loop observes `is_complete`. | `engine/conversation.py::_deliver_caller`, `transport/text_adapter.py::deliver`, `transport/voice_adapter.py::deliver` | `test_final_line_travels_with_the_hangup_speech_first`, `test_hangup_sends_the_final_line_then_stops`, `test_stream_turn_cycle_round_trips_through_the_engine` |
| D2 | Text recorded empty bot turns in the transcript; voice skipped them. | **Every bot turn is a transcript entry, empty included** — silence is evidence (usually the tag-only defect). The engine's transcript records it on both transports. (The voice session's own richer transcript remains authoritative for voice artifacts until Phase 6 decomposition.) | `engine/conversation.py::_on_bot_turn_ended` | `test_empty_bot_turns_are_transcript_entries` |
| D3 | One ambiguous `bot_response_latency`: the text runtime's self-reported thinking time vs voice's carrier+STT+bot+TTS round trip — never comparable, but stored in one field. | **Two named fields**: `BotTurnEnded.reasoning_ms` (the bot's own thinking time; text observes it, voice cannot) and `BotTurnEnded.transport_ms` (round trip at the transport boundary; both measure it). Aggregated as two separate streams on the outcome; never summed or compared. | `frames.py::BotTurnEnded`, `engine/conversation.py` (aggregation), both adapters (stamping) | `test_latencies_are_two_named_streams`, `test_transport_latency_is_stamped_not_merged` |
| D4 | Text nudged a silent bot ("Hello? Are you still there?"); voice sat through dead air until the abandonment counter tripped. Neither policy was written down. | **Adapter-declared policy: `nudge_on_silence`.** Text declares `True` (an HTTP turn is definitively over; nudge like a caller, bounded by `EMPTY_TURN_NUDGE_LIMIT`, allowance refills on recovery). Voice declares `False` (silence is dead air the stream keeps listening through; `AICallerEngine.MAX_EMPTY_BOT_TURNS` decides when it becomes a hangup). | `transport/text_adapter.py` / `transport/voice_adapter.py` (the declaration), `engine/conversation.py` (the enforcement) | `test_nudging_transport_gets_bounded_nudges_then_bot_silence`, `test_non_nudging_transport_defers_to_the_abandonment_counter`, `test_abandonment_counter_still_ends_dead_air` |
| D5 | "Stop the run" was one fuzzy operation. | **Two operations with drain semantics.** Graceful end (every normal termination): `finish(graceful=True)` — the adapter completes queued work before closing. Cancel (`ControlCancelled` frame, or the engine's own deadline): `finish(graceful=False)` — pending work is dropped, nothing further is delivered, the run stops within its bound. | `engine/conversation.py::run` (the split), each adapter's `finish` (the semantics) | `test_cancel_stops_without_draining`, `test_remote_end_is_graceful`, `test_cancel_unblocks_a_parked_stream_without_speaking`, `tests/unit/engine/test_drain_semantics.py` (both PRD acceptance tests) |

Two practical notes for future adapters:

* Declare `nudge_on_silence` explicitly — the engine reads it as the D4
  policy, and there is no default worth inheriting silently.
* `deliver()` is called exactly once per `BotTurnEnded`, possibly with an
  empty frame list ("keep listening"). An adapter that blocks a caller inside
  its turn cycle (as the voice bridge does) synchronises on that ack.
