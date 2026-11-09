"""S3-compatible store. Inject a boto3 client configured with S3_ENDPOINT.

All SDK I/O runs in worker threads. Credentials stay on the host; callers create
and inject clients in the composition root (MinIO/R2 use endpoint_url).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, NoReturn

from botocore.exceptions import BotoCoreError, ClientError

from agentplatform.artifacts.base import StoredObject, content_key
from agentplatform.artifacts.keys import validate_storage_key
from agentplatform.errors import AppError, NotFoundError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class S3ArtifactStore:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self.bucket = bucket

    async def put(self, data: bytes, *, content_type: str, key: str | None = None) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        automatic_key = key is None
        key = validate_storage_key(key if key is not None else content_key(digest))
        obj = StoredObject(key, len(data), digest, content_type)
        if automatic_key and await self.stat(key) == obj:
            return obj
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": digest},
            )
        except (BotoCoreError, ClientError) as exc:
            raise AppError("S3 write failed") from exc
        return obj

    async def get(self, key: str) -> bytes:
        validate_storage_key(key)

        def read() -> bytes:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            with response["Body"] as body:
                return bytes(body.read())

        try:
            return await asyncio.to_thread(read)
        except (BotoCoreError, ClientError) as exc:
            self._raise(exc)

    def _raise(self, exc: BotoCoreError | ClientError) -> NoReturn:
        if isinstance(exc, ClientError) and exc.response["Error"]["Code"] in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            raise NotFoundError("object not found") from exc
        raise AppError("S3 operation failed") from exc

    async def stat(self, key: str) -> StoredObject | None:
        validate_storage_key(key)
        try:
            response = await asyncio.to_thread(
                self._client.head_object, Bucket=self.bucket, Key=key
            )
        except (BotoCoreError, ClientError) as exc:
            try:
                self._raise(exc)
            except NotFoundError:
                return None
        return StoredObject(
            key, response["ContentLength"], response["Metadata"]["sha256"], response["ContentType"]
        )

    async def list(self, prefix: str = "") -> list[StoredObject]:
        def keys() -> list[str]:
            pages = self._client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            )
            return [item["Key"] for page in pages for item in page.get("Contents", [])]

        try:
            names = await asyncio.to_thread(keys)
        except (BotoCoreError, ClientError) as exc:
            raise AppError("S3 listing failed") from exc
        objects = [await self.stat(key) for key in sorted(names)]
        return [obj for obj in objects if obj is not None]

    async def delete(self, key: str) -> None:
        validate_storage_key(key)
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise AppError("S3 delete failed") from exc
