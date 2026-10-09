"""Artifact storage port (P5.3): local filesystem default, S3/MinIO adapter.

A run's artifacts — transcript, model-call summary, later audio — are written
through ONE interface addressed by KEY (`runs/<run_id>/transcript.json`), and
run rows hold keys, not paths. That is what makes the local→S3 move a config
change instead of a migration: the key is the identity, the store is where it
happens to live today.

    ARTIFACT_STORE = local | s3          (default local)
    local: ARTIFACT_LOCAL_ROOT           (default ./logs/artifacts)
    s3:    ARTIFACT_S3_BUCKET / _ENDPOINT / _ACCESS_KEY / _SECRET_KEY
           (endpoint set → MinIO or any S3-compatible; unset → real AWS)

Same selection contract as the other connectors: off-by-default heavy deps
(boto3 imports only inside the s3 factory), and a broken s3 config degrades
to local with a warning rather than failing the run.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Protocol

from vjudge.connectors import select

logger = logging.getLogger(__name__)

ENV_VAR = "ARTIFACT_STORE"
DEFAULT = "local"

# Keys are relative, forward-slash, and must never escape the root.
_BAD_KEY_PARTS = ("..", "\\")


def _check_key(key: str) -> str:
    if not key or key.startswith("/") or any(p in key for p in _BAD_KEY_PARTS):
        raise ValueError(f"artifact key must be a relative posix path, got {key!r}")
    return key


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> str: ...
    def get_bytes(self, key: str) -> bytes: ...


class LocalArtifactStore:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root or os.getenv("ARTIFACT_LOCAL_ROOT", "./logs/artifacts"))

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        path = self.root / _check_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_bytes(self, key: str) -> bytes:
        return (self.root / _check_key(key)).read_bytes()


class S3ArtifactStore:
    """S3 and MinIO — the only difference is the endpoint."""

    def __init__(
        self,
        bucket: Optional[str] = None,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        import boto3  # imported here so ARTIFACT_STORE=local never pays for it

        self.bucket = bucket or os.environ["ARTIFACT_S3_BUCKET"]
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint or os.getenv("ARTIFACT_S3_ENDPOINT") or None,
            aws_access_key_id=access_key or os.getenv("ARTIFACT_S3_ACCESS_KEY"),
            aws_secret_access_key=secret_key or os.getenv("ARTIFACT_S3_SECRET_KEY"),
        )

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self._client.put_object(
            Bucket=self.bucket, Key=_check_key(key), Body=data,
            ContentType=content_type,
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        return self._client.get_object(
            Bucket=self.bucket, Key=_check_key(key)
        )["Body"].read()


def build_store(override: Optional[str] = None) -> ArtifactStore:
    """Resolve the store. A broken s3 config degrades to local, loudly."""
    return select(
        kind="artifact store",
        env_var=ENV_VAR,
        default=DEFAULT,
        override=override,
        factories={"local": LocalArtifactStore, "s3": S3ArtifactStore},
    )
