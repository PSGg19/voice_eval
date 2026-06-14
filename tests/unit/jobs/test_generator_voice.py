"""The caller's TTS voice must follow the scenario's language.

A Spanish persona spoken by the default English voice is mispronounced badly
enough that the BOT's own STT garbles it, so the test stops measuring the bug and
nothing in the result says so — it just passes. Observed on job_4ccab2353f57.
"""

import yaml

from opkura.jobs.generator import (
    EXAMPLE_SCENARIO,
    _validate,
    dump_scenario,
    normalise_doc,
    stamp_scenario_id,
    voice_for_languages,
)

SPANISH = "aura-2-javier-es"


def test_spanish_locale_selects_the_spanish_voice():
    assert voice_for_languages(["es-US"]) == SPANISH
    assert voice_for_languages(["es"]) == SPANISH
    assert voice_for_languages(["ES-us"]) == SPANISH


def test_a_bilingual_job_still_uses_the_spanish_voice():
    """Those scenarios switch language mid-call; the hand-written bilingual ones
    (LIVE-18-spanish-switch-and-language-stability.yaml) use Spanish throughout."""
    assert voice_for_languages(["en-US", "es-US"]) == SPANISH


def test_english_and_unknown_languages_keep_the_default():
    assert voice_for_languages(["en-US"]) is None
    assert voice_for_languages([]) is None
    assert voice_for_languages(None) is None
    # No voice is proven for these bots — guessing a name would 400 at Deepgram.
    assert voice_for_languages(["hi-IN"]) is None
    assert voice_for_languages(["fr-CA"]) is None


def _doc():
    return {"name": "n", "target": "+15550001111", "persona": "p", "vars": {}}


def test_normalise_doc_injects_the_voice_for_a_spanish_job():
    doc, changed = normalise_doc(_doc(), "+15550001111", ["es-US"])
    assert changed is True
    assert doc["tts"]["model"] == SPANISH


def test_normalise_doc_leaves_an_english_job_alone():
    doc, _ = normalise_doc(_doc(), "+15550001111", ["en-US"])
    assert "tts" not in doc


def test_normalise_doc_never_overrides_a_voice_the_model_chose():
    doc = _doc() | {"tts": {"model": "aura-2-celeste-es"}}
    doc, _ = normalise_doc(doc, "+15550001111", ["es-US"])
    assert doc["tts"]["model"] == "aura-2-celeste-es"


def test_normalise_doc_preserves_other_tts_keys():
    doc = _doc() | {"tts": {"speed": 0.9}}
    doc, _ = normalise_doc(doc, "+15550001111", ["es-US"])
    assert doc["tts"] == {"speed": 0.9, "model": SPANISH}


def test_normalise_doc_replaces_a_malformed_tts_block():
    """The model has emitted scalars where a mapping belongs before now."""
    doc = _doc() | {"tts": "aura-2-javier-es"}
    doc, _ = normalise_doc(doc, "+15550001111", ["es-US"])
    assert doc["tts"] == {"model": SPANISH}


def test_the_injected_block_survives_lint_and_the_real_loader(tmp_path):
    """The check that matters: a scenario carrying the injected voice must pass
    BOTH opkura's linter and load_scenario, since the campaign runner uses the
    loader and lint alone has passed loader-invalid scenarios before."""
    doc, changed = normalise_doc(dict(EXAMPLE_SCENARIO), None, ["es-US"])
    assert changed is True
    path = tmp_path / "S01-es.yaml"
    path.write_text(dump_scenario(stamp_scenario_id(doc, "S01-es")))

    ok, errors = _validate(path)
    assert ok, errors

    written = yaml.safe_load(path.read_text())
    assert written["tts"]["model"] == SPANISH
