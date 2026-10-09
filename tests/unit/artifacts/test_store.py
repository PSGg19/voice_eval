"""P5.3 — the artifact store port (local adapter + selection)."""
from __future__ import annotations

import pytest

from vjudge.artifacts import LocalArtifactStore, build_store


def test_local_round_trip(tmp_path):
    store = LocalArtifactStore(root=str(tmp_path))
    key = store.put_bytes("runs/r1/transcript.json", b'{"x":1}')
    assert key == "runs/r1/transcript.json"          # key is the identity
    assert store.get_bytes(key) == b'{"x":1}'
    assert (tmp_path / "runs" / "r1" / "transcript.json").exists()


@pytest.mark.parametrize("bad", ["/abs/path", "../escape", "a\\b", ""])
def test_keys_cannot_escape_the_root(tmp_path, bad):
    store = LocalArtifactStore(root=str(tmp_path))
    with pytest.raises(ValueError):
        store.put_bytes(bad, b"x")


def test_default_selection_is_local(monkeypatch, tmp_path):
    monkeypatch.delenv("ARTIFACT_STORE", raising=False)
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", str(tmp_path))
    assert isinstance(build_store(), LocalArtifactStore)


def test_broken_s3_config_degrades_to_local(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_STORE", "s3")
    monkeypatch.delenv("ARTIFACT_S3_BUCKET", raising=False)  # factory will raise
    monkeypatch.setenv("ARTIFACT_LOCAL_ROOT", str(tmp_path))
    store = build_store()
    assert isinstance(store, LocalArtifactStore)  # degraded, not crashed
