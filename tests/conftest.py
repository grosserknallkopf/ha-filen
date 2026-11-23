"""Fixtures for the Filen integration tests.

The library itself is tested against a fake HTTP server in aiofilen/tests; here an
in-memory client stands in for it, so the tests cover the Home Assistant side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from dataclasses import replace
import itertools
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.filen.aiofilen import (
    Credentials,
    DriveFile,
    DriveFolder,
    FilenAuthError,
    FilenNotFoundError,
    FilenTwoFactorRequired,
    User,
)
from custom_components.filen.const import (
    CONF_CREDENTIALS,
    CONF_FOLDER_NAME,
    DEFAULT_FOLDER_NAME,
    DOMAIN,
)

from .common import MockConfigEntry

EMAIL = "user@example.com"
PASSWORD = "secret password"
USER = User(
    user_id=42,
    email=EMAIL,
    max_storage=10**12,
    storage_used=1_000,
    base_folder_uuid="root",
)
CREDENTIALS = Credentials(
    api_key="api-key-stored",
    master_keys=["master-key"],
    auth_version=2,
    email=EMAIL,
    base_folder_uuid="root",
    private_key="private-key",
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Load integrations from custom_components (pytest-homeassistant does that by default)."""
    if "enable_custom_integrations" in request.fixturenames or _has_fixture(
        request, "enable_custom_integrations"
    ):
        request.getfixturevalue("enable_custom_integrations")


def _has_fixture(request: pytest.FixtureRequest, name: str) -> bool:
    return bool(request._fixturemanager.getfixturedefs(name, request.node))


def _file(uuid: str, name: str, size: int, folder_uuid: str) -> DriveFile:
    return DriveFile(
        uuid=uuid,
        name=name,
        size=size,
        mime="application/octet-stream",
        key="file-key",
        chunks=1,
        bucket="bucket",
        region="region",
        parent_uuid=folder_uuid,
        version=2,
        last_modified=0,
        creation=0,
        hash=None,
        rm="",
        timestamp=0,
    )


class FakeClient:
    """In-memory stand-in for FilenClient with switches for error cases."""

    def __init__(self) -> None:
        self.two_factor = False
        self.login_error: Exception | None = None
        self.user_info_error: Exception | None = None
        self.upload_error_for: str | None = None  # file name suffix that fails
        self.storage_used = 1_000
        self.max_storage = 10**12
        self._ids = itertools.count(1)
        self.credentials_value: Credentials | None = None
        self._user: User | None = None
        self.folders: dict[str, DriveFolder] = {}
        self.files: dict[str, DriveFile] = {}
        self.data: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.trashed: list[str] = []
        self.stream_fail_after: int | None = None
        self.download_ranges: list[tuple[int, int | None]] = []

    # session-bound constructor signature
    def __call__(self, *args: Any, **kwargs: Any) -> FakeClient:
        return self

    @property
    def credentials(self) -> Credentials:
        if self.credentials_value is None:
            raise FilenAuthError("not logged in")
        return self.credentials_value

    @property
    def user(self) -> User:
        assert self._user is not None
        return self._user

    @property
    def root_folder_uuid(self) -> str:
        return self.credentials.base_folder_uuid

    def set_credentials(self, credentials: Credentials) -> None:
        self.credentials_value = credentials

    async def login(
        self, email: str, password: str, tfa_code: str | None = None
    ) -> Credentials:
        if self.login_error:
            raise self.login_error
        if self.two_factor and tfa_code != "123456":
            raise FilenTwoFactorRequired("Invalid 2FA code")
        if password != PASSWORD or email != EMAIL:
            raise FilenAuthError("Invalid email or password")
        self.credentials_value = replace(CREDENTIALS, api_key="api-key-login")
        self._user = USER
        return self.credentials_value

    async def user_info(self) -> User:
        if self.user_info_error:
            raise self.user_info_error
        self._user = replace(
            USER, storage_used=self.storage_used, max_storage=self.max_storage
        )
        return self._user

    async def base_folder(self) -> str:
        return "root"

    async def ensure_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        for folder in self.folders.values():
            if folder.name == name and folder.parent_uuid == parent_uuid:
                return folder
        folder = DriveFolder(
            uuid=f"folder-{next(self._ids)}", name=name, parent_uuid=parent_uuid
        )
        self.folders[folder.uuid] = folder
        return folder

    async def list_files(self, folder_uuid: str) -> list[DriveFile]:
        return [f for f in self.files.values() if f.parent_uuid == folder_uuid]

    async def list_folders(self, folder_uuid: str) -> list[DriveFolder]:
        return [f for f in self.folders.values() if f.parent_uuid == folder_uuid]

    async def list_dir(
        self, folder_uuid: str
    ) -> tuple[list[DriveFile], list[DriveFolder]]:
        return await self.list_files(folder_uuid), await self.list_folders(folder_uuid)

    async def ensure_folder_path(self, parent_uuid: str, path: str) -> str:
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            current = (await self.ensure_folder(current, part)).uuid
        return current

    async def find_folder_path(self, parent_uuid: str, path: str) -> str | None:
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            match = next(
                (f for f in await self.list_folders(current) if f.name == part), None
            )
            if match is None:
                return None
            current = match.uuid
        return current

    async def find_file(self, folder_uuid: str, name: str) -> DriveFile | None:
        return next(
            (f for f in await self.list_files(folder_uuid) if f.name == name), None
        )

    async def file_meta(self, file_uuid: str) -> DriveFile:
        if file_uuid not in self.files:
            raise FilenNotFoundError("not found")
        return self.files[file_uuid]

    async def trash_file(self, file_uuid: str) -> None:
        self.trashed.append(file_uuid)
        del self.files[file_uuid]

    async def upload(
        self, stream: AsyncIterator[bytes], *, size: int, folder_uuid: str, name: str,
        mime: str = "application/octet-stream", progress: Any = None,
    ) -> DriveFile:  # fmt: skip
        if self.upload_error_for and name.endswith(self.upload_error_for):
            from custom_components.filen.aiofilen import FilenError

            raise FilenError("upload failed")
        data = b"".join([chunk async for chunk in stream])
        assert len(data) == size
        if progress:
            progress(size)
        file = _file(f"file-{next(self._ids)}", name, size, folder_uuid)
        self.files[file.uuid] = file
        self.data[file.uuid] = data
        return file

    async def upload_bytes(
        self, data: bytes, *, folder_uuid: str, name: str,
        mime: str = "application/octet-stream",
    ) -> DriveFile:  # fmt: skip
        async def gen() -> AsyncIterator[bytes]:
            yield data

        return await self.upload(
            gen(), size=len(data), folder_uuid=folder_uuid, name=name, mime=mime
        )

    async def download(
        self, file: DriveFile, *, start: int = 0, end: int | None = None
    ) -> FakeStream:
        self.download_ranges.append((start, end))
        if file.uuid not in self.files:
            raise FilenNotFoundError("gone")
        data = self.data[file.uuid]
        return FakeStream(
            data[start : None if end is None else end + 1], self.stream_fail_after
        )

    async def download_bytes(self, file: DriveFile) -> bytes:
        return self.data[file.uuid]

    async def delete_file(self, file_uuid: str) -> None:
        if file_uuid not in self.files:
            raise FilenNotFoundError("not found")
        del self.files[file_uuid]
        self.deleted.append(file_uuid)


class FakeStream:
    """Mimics aiofilen.DownloadStream."""

    def __init__(self, data: bytes, fail_after: int | None = None) -> None:
        self._chunks = [data[i : i + 1000] for i in range(0, len(data), 1000)]
        self.closed = False
        self._fail_after = fail_after

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        if self._fail_after is not None:
            if self._fail_after == 0:
                from custom_components.filen.aiofilen import FilenConnectionError

                raise FilenConnectionError("connection lost")
            self._fail_after -= 1
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


@pytest.fixture
def fake_client() -> Generator[FakeClient]:
    """Patch the library client everywhere the integration creates one."""
    client = FakeClient()
    with (
        patch("custom_components.filen.FilenClient", client),
        patch("custom_components.filen.config_flow.FilenClient", client),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured Filen entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=str(USER.user_id),
        data={
            "email": EMAIL,
            CONF_FOLDER_NAME: DEFAULT_FOLDER_NAME,
            CONF_CREDENTIALS: CREDENTIALS.as_dict(),
        },
        entry_id="01JFILENENTRY00000000000",
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant,
    fake_client: FakeClient,
    config_entry: MockConfigEntry,
    tmp_path: Path,
) -> MockConfigEntry:
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with patch("homeassistant.components.backup.is_hassio", return_value=False):
        assert await async_setup_component(hass, "backup", {})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return config_entry