"""Region substitution — the region is a PATH SEGMENT, and getting it wrong
produces a 404 that blames the assistant_uuid. It has done so twice."""
from __future__ import annotations

import logging

import pytest

from vjudge.core.region import ALLOWED_REGIONS, apply_region, reset_announcements


@pytest.fixture(autouse=True)
def _clean():
    reset_announcements()
    yield
    reset_announcements()


_US = "https://llm-studio.vjudge.dev/us/api/interface"


@pytest.mark.parametrize("region", ALLOWED_REGIONS)
def test_substitutes_every_allowed_region(region):
    assert apply_region(_US, region) == _US.replace("/us/", f"/{region}/")


def test_no_region_leaves_the_url_alone():
    assert apply_region(_US, None) == _US
    assert apply_region(_US, "") == _US


def test_trailing_slash_is_stripped():
    assert apply_region(_US + "/", "us") == _US
    assert apply_region(_US + "/", None) == _US


def test_only_the_first_segment_is_swapped():
    """A path that happens to contain another region word must not be mangled."""
    url = "https://h/us/api/ca/thing"
    assert apply_region(url, "in") == "https://h/in/api/ca/thing"


def test_a_region_word_not_in_a_path_segment_is_not_matched():
    """`us` inside a hostname or a longer word is not a region segment."""
    assert apply_region("https://uspto.example.com/api", "in") == "https://uspto.example.com/api"


def test_a_url_with_no_region_segment_warns_and_is_unchanged(caplog):
    with caplog.at_level(logging.WARNING, logger="vjudge.core.region"):
        out = apply_region("http://localhost:8001", "us")
    assert out == "http://localhost:8001"
    assert "no recognisable region path segment" in caplog.text


def test_the_substitution_is_announced_exactly_once(caplog):
    """`TextTransport.base_url` is a property called every turn and every retry.
    Logging per call printed the same line five times in a three-turn run."""
    with caplog.at_level(logging.INFO, logger="vjudge.core.region"):
        for _ in range(5):
            apply_region("https://llm-studio.vjudge.dev/in/api/text", "us")
    assert caplog.text.count("scenario pins region=us") == 1


def test_a_different_decision_is_announced_separately(caplog):
    """Deduping must key on the decision, not silence everything after the first."""
    with caplog.at_level(logging.INFO, logger="vjudge.core.region"):
        apply_region("https://llm-studio.vjudge.dev/in/api/text", "us", what="text")
        apply_region("https://llm-studio.vjudge.dev/in/api/interface", "us", what="osv")
    assert caplog.text.count("scenario pins region=us") == 2


def test_reset_re_announces(caplog):
    """A campaign runs many scenarios; each should report its own routing."""
    with caplog.at_level(logging.INFO, logger="vjudge.core.region"):
        apply_region("https://llm-studio.vjudge.dev/in/api/text", "us")
        reset_announcements()
        apply_region("https://llm-studio.vjudge.dev/in/api/text", "us")
    assert caplog.text.count("scenario pins region=us") == 2


def test_a_noop_substitution_is_not_announced(caplog):
    """Pinning the region you were already using is not news."""
    with caplog.at_level(logging.INFO, logger="vjudge.core.region"):
        assert apply_region(_US, "us") == _US
    assert "scenario pins region" not in caplog.text


def test_the_loader_and_the_rewriter_agree_on_the_region_list():
    """A region the loader accepts but the rewriter ignores would silently leave
    the URL pointing at the wrong gateway."""
    from vjudge.core.scenario_loader import ALLOWED_REGIONS as loader_regions

    assert loader_regions is ALLOWED_REGIONS
