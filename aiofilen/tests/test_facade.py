"""Facade tests with an in-memory native module.

The real protocol lives in filen-sdk-rs and is exercised by the SDK itself.
These tests cover the Python side: model conversion, the async iterator
adapters, and error translation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from dataclasses import dataclass, field
import itertools
import threading
from types import SimpleNamespace

import pytest

from aiofilen import Credentials, DriveFile, FilenClient
from aiofilen import _native
from aiofilen.client import READ_CHUNK, _AsyncByteSource
from aiofilen.exceptions import FilenAuthError, FilenStreamError

EMAIL = "user@example.com"


async def test_source_pump_does_not_depend_on_default_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy HA executor must not stall the first upload byte."""

    async def stream() -> AsyncIterator[bytes]:
        yield b"x" * (READ_CHUNK * 5 + 17)

    def executor_is_unavailable(*args: object, **kwargs: object) -> object:
        raise AssertionError("the upload pump must not use asyncio.to_thread")

    monkeypatch.setattr("aiofilen.client.asyncio.to_thread", executor_is_unavailable)
    loop = asyncio.get_running_loop()
    source = _AsyncByteSource(stream(), loop)
    chunks: list[bytes] = []
    done = asyncio.Event()

    def read_all() -> None:
        while chunk := source.read(64 * 1024):
            chunks.append(chunk)
        loop.call_soon_threadsafe(done.set)

    thread = threading.Thread(target=read_all)
    thread.start()
    await asyncio.wait_for(done.wait(), timeout=2)
    thread.join()
    await source.aclose()
    assert b"".join(chunks) == b"x" * (READ_CHUNK * 5 + 17)


class SdkError(Exception):
    pass


for _variant in (
    "Auth",
    "TwoFactor",
    "Blocked",
    "Connection",
    "RateLimit",
    "Quota",
    "NotFound",
    "Conflict",
    "Integrity",
    "Crypto",
    "Stream",
    "Other",
):
    setattr(SdkError, _variant, type(_variant, (SdkError,), {}))


@dataclass
class NativeCredentials:
    api_key: str
    master_keys: list[str]
    auth_version: int
    email: str
    base_folder_uuid: str
    user_id: int
    private_key: str | None


@dataclass
class NativeUser:
    user_id: int
    email: str
    max_storage: int
    storage_used: int
    base_folder_uuid: str


@dataclass
class NativeFile:
    uuid: str
    name: str
    size: int
    mime: str
    key: str = "key"
    chunks: int = 1
    bucket: str = "bucket"
    region: str = "region"
    parent_uuid: str = "root"
    version: int = 3
    last_modified: int | None = 0
    creation: int | None = 0
    timestamp: int | None = 0


@dataclass
class NativeFolder:
    uuid: str
    name: str
    parent_uuid: str | None


@dataclass
class NativeListing:
    files: list[NativeFile] = field(default_factory=list)
    folders: list[NativeFolder] = field(default_factory=list)


class FakeSdkClient:
    instances: list[FakeSdkClient] = []

    def __init__(self, credentials: NativeCredentials) -> None:
        self.credentials = credentials
        self.folders: dict[str, NativeFolder] = {}
        self.files: dict[str, bytes] = {}
        self.meta: dict[str, NativeFile] = {}
        self.ids = itertools.count(1)
        FakeSdkClient.instances.append(self)

    @classmethod
    def from_credentials(cls, credentials: NativeCredentials) -> FakeSdkClient:
        return cls(credentials)

    def root_folder_uuid(self) -> str:
        return self.credentials.base_folder_uuid

    async def user_info(self) -> NativeUser:
        return NativeUser(
            user_id=self.credentials.user_id,
            email=self.credentials.email,
            max_storage=1000,
            storage_used=10,
            base_folder_uuid=self.credentials.base_folder_uuid,
        )

    async def list_dir(self, folder_uuid: str) -> NativeListing:
        return NativeListing(
            files=[f for f in self.meta.values() if f.parent_uuid == folder_uuid],
            folders=[f for f in self.folders.values() if f.parent_uuid == folder_uuid],
        )

    async def ensure_folder(self, parent_uuid: str, name: str) -> NativeFolder:
        for folder in self.folders.values():
            if folder.parent_uuid == parent_uuid and folder.name == name:
                return folder
        folder = NativeFolder(f"folder-{next(self.ids)}", name, parent_uuid)
        self.folders[folder.uuid] = folder
        return folder

    async def find_folder(self, parent_uuid: str, name: str) -> NativeFolder | None:
        return next(
            (
                folder
                for folder in self.folders.values()
                if folder.parent_uuid == parent_uuid and folder.name == name
            ),
            None,
        )

    async def file_meta(self, file_uuid: str) -> NativeFile:
        return self.meta[file_uuid]

    async def trash_file(self, file_uuid: str) -> None:
        del self.files[file_uuid]
        del self.meta[file_uuid]

    async def delete_file(self, file_uuid: str) -> None:
        await self.trash_file(file_uuid)

    async def upload(
        self,
        source: object,
        size: int,
        folder_uuid: str,
        name: str,
        mime: str,
        progress: object | None,
    ) -> NativeFile:
        chunks: list[bytes] = []
        remaining = size
        import asyncio

        while remaining:
            data = await asyncio.to_thread(source.read, min(remaining, 1024))
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
            if progress is not None:
                progress.on_progress(size - remaining)
        file = NativeFile(
            uuid=f"file-{next(self.ids)}",
            name=name,
            size=size,
            mime=mime,
            parent_uuid=folder_uuid,
        )
        joined = b"".join(chunks)
        assert len(joined) == size, (len(joined), size)
        self.meta[file.uuid] = file
        self.files[file.uuid] = joined
        return file

    async def download(
        self,
        file_uuid: str,
        start: int,
        end: int,
        sink: object,
        progress: object | None,
    ) -> None:
        data = self.files[file_uuid][start:end]
        for offset in range(0, len(data), 1000):
            if not sink.write(data[offset : offset + 1000]):
                raise SdkError.Stream("stopped")
            # The real sink is called on a Rust thread; yielding here lets the
            # event-loop consumer release the bounded slots.
            import asyncio

            await asyncio.sleep(0)
        sink.finish()


def fake_module() -> SimpleNamespace:
    return SimpleNamespace(
        SdkClient=FakeSdkClient,
        Credentials=NativeCredentials,
        User=NativeUser,
        DriveFile=NativeFile,
        DriveFolder=NativeFolder,
        DirListing=NativeListing,
        SdkError=SdkError,
    )


@pytest.fixture(autouse=True)
def fake_native(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSdkClient.instances.clear()
    monkeypatch.setattr(_native, "_MODULE", fake_module())


def credentials() -> Credentials:
    return Credentials(
        api_key="api-key",
        master_keys=["master"],
        auth_version=3,
        email=EMAIL,
        base_folder_uuid="root",
        private_key="private",
        user_id=42,
    )


def make_client() -> FilenClient:
    client = FilenClient()
    client.set_credentials(credentials())
    return client


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), 777):
        yield data[offset : offset + 777]


async def test_models_and_credentials_roundtrip() -> None:
    data = credentials().as_dict()
    assert Credentials.from_dict(data) == credentials()
    # Old entries have no user_id.
    data.pop("user_id")
    assert Credentials.from_dict(data).user_id == 0


async def test_user_and_folders() -> None:
    client = make_client()
    assert (await client.user_info()).user_id == 42
    folder = await client.ensure_folder("root", "Backups")
    assert await client.find_folder_path("root", "Backups") == folder.uuid
    assert await client.find_folder_path("root", "Missing") is None
    nested = await client.ensure_folder_path("root", "Backups/2026/01")
    assert nested != folder.uuid


async def test_upload_download_and_range() -> None:
    client = make_client()
    folder = await client.ensure_folder("root", "Backups")
    data = bytes(range(256)) * 100
    progress: list[int] = []
    uploaded = await client.upload(
        _stream(data),
        size=len(data),
        folder_uuid=folder.uuid,
        name="backup.tar",
        progress=progress.append,
    )
    assert uploaded.size == len(data)
    assert await client.list_files(folder.uuid) == [uploaded]
    assert await client.download_bytes(uploaded) == data

    stream = await client.download(uploaded, start=100, end=199)
    async with stream:
        assert b"".join([chunk async for chunk in stream]) == data[100:200]


async def test_empty_download() -> None:
    client = make_client()
    file = DriveFile(
        uuid="empty",
        name="empty",
        size=0,
        mime="application/octet-stream",
        key="key",
        chunks=0,
        bucket="",
        region="",
        parent_uuid="root",
        version=3,
        last_modified=0,
        creation=0,
        hash=None,
        rm="",
        timestamp=0,
    )
    stream = await client.download(file)
    assert b"".join([chunk async for chunk in stream]) == b""


async def test_source_error_becomes_stream_error() -> None:
    client = make_client()
    folder = await client.ensure_folder("root", "Backups")

    async def broken() -> AsyncIterator[bytes]:
        yield b"abc"
        raise RuntimeError("source broke")

    with pytest.raises(FilenStreamError):
        await client.upload(
            broken(), size=10, folder_uuid=folder.uuid, name="broken.tar"
        )


async def test_native_auth_error_is_translated() -> None:
    client = make_client()
    native = _native.load()
    assert native is not None

    class Failing(FakeSdkClient):
        async def user_info(self) -> NativeUser:
            raise SdkError.Auth("api key rejected")

    native.SdkClient = Failing
    with pytest.raises(FilenAuthError):
        await client.user_info()
