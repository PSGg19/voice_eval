"""P5.3 acceptance: artifact keys resolve identically through the local
adapter and the MinIO adapter — same key, same bytes. Skips when MinIO is not
running (docker compose --profile tracing/storage up -d)."""
from __future__ import annotations

import uuid

import httpx
import pytest

from opkura.artifacts import LocalArtifactStore, S3ArtifactStore

MINIO = "http://localhost:9000"


def _minio_up() -> bool:
    try:
        httpx.get(f"{MINIO}/minio/health/live", timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _minio_up(), reason="MinIO not reachable — docker compose --profile storage up -d")
def test_same_key_same_bytes_through_both_adapters(tmp_path):
    key = f"runs/parity-{uuid.uuid4().hex}/transcript.json"
    payload = b'{"turns": [{"speaker": "bot", "text": "parity \xe2\x9c\x93"}]}'

    local = LocalArtifactStore(root=str(tmp_path))
    s3 = S3ArtifactStore(
        bucket="opkura-artifacts",
        endpoint=MINIO,
        access_key="opkura",
        secret_key="opkura-local-only",
    )

    # The KEY is the identity: both adapters accept it verbatim…
    assert local.put_bytes(key, payload) == key
    assert s3.put_bytes(key, payload) == key
    # …and both hand back the identical bytes.
    assert local.get_bytes(key) == payload
    assert s3.get_bytes(key) == payload
    assert local.get_bytes(key) == s3.get_bytes(key)
