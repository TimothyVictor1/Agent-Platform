"""Byte stores and settings-based construction for the composition root (T13)."""

from __future__ import annotations

import asyncio

import boto3

from agentplatform.artifacts.base import ArtifactStore
from agentplatform.artifacts.fs_store import FileSystemArtifactStore
from agentplatform.artifacts.s3_store import S3ArtifactStore
from agentplatform.config import Settings
from agentplatform.errors import ConfigError


async def create_artifact_store(settings: Settings) -> ArtifactStore:
    """Create the selected store; SDK credential discovery never blocks the loop."""
    if settings.artifact_store == "fs":
        return await asyncio.to_thread(FileSystemArtifactStore, settings.artifact_dir)
    if not settings.s3_bucket:
        raise ConfigError("S3_BUCKET is required for S3 artifact storage")
    client = await asyncio.to_thread(
        boto3.client,
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        aws_access_key_id=(
            settings.s3_access_key_id.get_secret_value() if settings.s3_access_key_id else None
        ),
        aws_secret_access_key=(
            settings.s3_secret_access_key.get_secret_value()
            if settings.s3_secret_access_key
            else None
        ),
    )
    return S3ArtifactStore(client, settings.s3_bucket)
