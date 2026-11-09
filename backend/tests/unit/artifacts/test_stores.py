from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from agentplatform.artifacts.base import ArtifactStore, content_key
from agentplatform.artifacts.frames import store_frame
from agentplatform.artifacts.fs_store import FileSystemArtifactStore
from agentplatform.artifacts.s3_store import S3ArtifactStore
from agentplatform.errors import InvalidRequestError, NotFoundError
from agentplatform.testing import InMemoryArtifactStore


@pytest.fixture(params=["memory", "fs", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ArtifactStore]:
    if request.param == "memory":
        yield InMemoryArtifactStore()
    elif request.param == "fs":
        yield FileSystemArtifactStore(tmp_path.resolve() / "objects")
    else:
        with mock_aws():
            client = boto3.client(
                "s3",
                region_name="us-east-1",
                aws_access_key_id="fake",  # gitleaks:allow
                aws_secret_access_key="fake",  # gitleaks:allow
            )
            client.create_bucket(Bucket="artifacts-test")
            yield S3ArtifactStore(client, "artifacts-test")


async def test_store_contract(store: ArtifactStore) -> None:
    data = b"some content"
    obj = await store.put(data, content_type="text/plain")
    assert obj.sha256 == hashlib.sha256(data).hexdigest()
    assert obj.key == content_key(obj.sha256)
    assert await store.put(data, content_type="text/plain") == obj
    assert await store.get(obj.key) == data
    assert await store.stat(obj.key) == obj
    assert await store.list("sha256/") == [obj]
    assert await store.list("other/") == []
    await store.put(b"new", content_type="text/plain", key=obj.key)
    assert await store.get(obj.key) == b"new"
    assert (await store.stat(obj.key)) != obj
    await store.delete(obj.key)
    await store.delete(obj.key)
    assert await store.stat(obj.key) is None
    with pytest.raises(NotFoundError):
        await store.get(obj.key)


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "/absolute",
        "a/../../escape",
        "a//b",
        "a/./b",
        "a/..",
        "a\\b",
        "a/．．",
        "a/∕b",
        "é",
        "a\x00",
    ],
)
async def test_bad_keys(store: ArtifactStore, key: str) -> None:
    with pytest.raises(InvalidRequestError):
        await store.put(b"x", content_type="text/plain", key=key)
    with pytest.raises(InvalidRequestError):
        await store.get(key)
    with pytest.raises(InvalidRequestError):
        await store.stat(key)
    with pytest.raises(InvalidRequestError):
        await store.delete(key)


async def test_frame(store: ArtifactStore) -> None:
    obj = await store_frame(store, b"jpeg", session_id="s", agent_id="a", step=1)
    assert obj.key == "frames/s/a/1.jpg"
    assert obj.content_type == "image/jpeg"
    assert await store.get(obj.key) == b"jpeg"


@pytest.mark.parametrize("location", ["root", "parent", "leaf"])
async def test_symlinks_refused(tmp_path: Path, location: str) -> None:
    tmp_path = await asyncio.to_thread(tmp_path.resolve)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "file"
    secret.write_bytes(b"unchanged")
    root = tmp_path / "objects"
    root.mkdir()
    if location == "root":
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
        key = "file"
    elif location == "parent":
        (root / "link").symlink_to(outside, target_is_directory=True)
        key = "link/file"
    else:
        (root / "file").symlink_to(secret)
        key = "file"
    store = FileSystemArtifactStore(root)
    with pytest.raises(InvalidRequestError):
        await store.put(b"bad", content_type="text/plain", key=key)
    with pytest.raises(InvalidRequestError):
        await store.get(key)
    with pytest.raises(InvalidRequestError):
        await store.stat(key)
    with pytest.raises(InvalidRequestError):
        await store.delete(key)
    with pytest.raises(InvalidRequestError):
        await store.list()
    assert secret.read_bytes() == b"unchanged"


async def test_atomic_replace(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(await asyncio.to_thread(tmp_path.resolve))
    a, b = b"a" * 100_000, b"b" * 100_000
    await store.put(a, content_type="text/plain", key="object")

    async def writer() -> None:
        for data in (b, a, b, a):
            await store.put(data, content_type="text/plain", key="object")

    async def reader() -> None:
        for _ in range(20):
            assert await store.get("object") in (a, b)
            metadata = await store.stat("object")
            assert metadata is not None
            assert metadata.sha256 in {hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest()}

    await asyncio.gather(writer(), reader())
    assert len(await store.list()) == 1


async def test_valid_hidden_key_is_listed(store: ArtifactStore) -> None:
    obj = await store.put(b"hidden", content_type="text/plain", key=".tmp-visible")
    assert await store.list(".tmp-") == [obj]


async def test_store_factory(tmp_path: Path) -> None:
    from agentplatform.artifacts import create_artifact_store
    from agentplatform.config import Settings
    from agentplatform.errors import ConfigError

    root = await asyncio.to_thread(tmp_path.resolve)
    created = await create_artifact_store(Settings(_env_file=None, artifact_dir=str(root)))
    assert isinstance(created, FileSystemArtifactStore)
    obj = await created.put(b"factory", content_type="text/plain")
    assert await created.get(obj.key) == b"factory"
    with pytest.raises(ConfigError):
        await create_artifact_store(Settings(_env_file=None, artifact_store="s3", s3_bucket=""))


async def test_trailing_newline_key_refused(tmp_path: Path) -> None:
    root = await asyncio.to_thread(tmp_path.resolve)
    store = FileSystemArtifactStore(root)
    with pytest.raises(InvalidRequestError):
        await store.put(b"x", content_type="text/plain", key="name\n")
