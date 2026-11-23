"""Async client for Filen (filen.io).

Protocol ported from the official SDKs (FilenCloudDienste/filen-sdk-ts and
filen-sdk-go, MIT). All file content and names are encrypted on this device
before they reach Filen; the API key and master keys are what a caller persists.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import replace
import functools
import hashlib
import json as jsonlib
import logging
import math
import random
import time
from typing import Any
import uuid as uuidlib

import aiohttp

from . import crypto
from .exceptions import (
    FilenAccountBlocked,
    FilenAuthError,
    FilenConflictError,
    FilenConnectionError,
    FilenCryptoError,
    FilenError,
    FilenIntegrityError,
    FilenNotFoundError,
    FilenQuotaError,
    FilenRateLimitError,
    FilenStreamError,
    FilenTwoFactorRequired,
)
from .models import AuthInfo, Credentials, DriveFile, DriveFolder, User

_LOGGER = logging.getLogger(__name__)

GATEWAY_URL = "https://gateway.filen.io"
INGEST_URLS = (
    "https://ingest.filen.io",
    "https://ingest.filen.net",
    "https://ingest.filen-1.net",
    "https://ingest.filen-2.net",
    "https://ingest.filen-3.net",
    "https://ingest.filen-4.net",
    "https://ingest.filen-5.net",
    "https://ingest.filen-6.net",
)
EGEST_URLS = (
    "https://egest.filen.io",
    "https://egest.filen.net",
    "https://egest.filen-1.net",
    "https://egest.filen-2.net",
    "https://egest.filen-3.net",
    "https://egest.filen-4.net",
    "https://egest.filen-5.net",
    "https://egest.filen-6.net",
)

MIB = 1024 * 1024  # Filen stores files in chunks of 1 MiB
NO_2FA_CODE = "XXXXXX"  # what the API expects when 2FA is disabled
RETRIES = 3
MAX_RETRY_WAIT = 60.0
THREAD_THRESHOLD = 256 * 1024  # crypto on larger blocks runs in a thread

ProgressCallback = Callable[[int], None]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _translate_unexpected[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Turn surprises in response shapes into FilenError, so callers only see our errors."""

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except FilenError:
            raise
        except (KeyError, TypeError, AttributeError, IndexError, ValueError) as err:
            raise FilenError(f"unexpected response: {type(err).__name__}: {err}") from err

    return wrapper


def _retry_wait(attempt: int, err: FilenConnectionError) -> float:
    if isinstance(err, FilenRateLimitError) and err.retry_after is not None:
        return min(err.retry_after, MAX_RETRY_WAIT)
    return min(float(2**attempt) + random.random(), MAX_RETRY_WAIT)  # noqa: S311


class FilenClient:
    """Filen client.

    Pass an aiohttp session owned by the caller. After `login()`, read
    `credentials` and persist them; the API key does not rotate, so there is
    no refresh step. `set_credentials()` restores a previous login.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        gateway_url: str = GATEWAY_URL,
        ingest_urls: tuple[str, ...] = INGEST_URLS,
        egest_urls: tuple[str, ...] = EGEST_URLS,
        request_timeout: float = 60,
    ) -> None:
        self._session = session
        self._gateway = gateway_url
        self._ingest_urls = ingest_urls
        self._egest_urls = egest_urls
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        # No total limit for transfers: a restore over a slow line may take hours.
        self._transfer_timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=300
        )
        self._credentials: Credentials | None = None
        self._user: User | None = None
        self._hmac_key: bytes | None = None

    # ------------------------------------------------------------------ state

    @property
    def credentials(self) -> Credentials:
        if self._credentials is None:
            raise FilenAuthError("not logged in")
        return self._credentials

    @property
    def user(self) -> User:
        if self._user is None:
            raise FilenError("no account data yet, call user_info() first")
        return self._user

    @property
    def root_folder_uuid(self) -> str:
        return self.credentials.base_folder_uuid

    def set_credentials(self, credentials: Credentials) -> None:
        self._credentials = credentials
        self._hmac_key = None

    @property
    def _meta_version(self) -> int:
        """Metadata format: 003 for auth version 3 accounts, 002 otherwise."""
        return 3 if self.credentials.auth_version == 3 else 2

    # ------------------------------------------------------------------ http

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.api_key}"}

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Any = None,
        auth: bool = True,
        retries: int | None = None,
        login: bool = False,
        timeout: aiohttp.ClientTimeout | None = None,  # noqa: ASYNC109 - per-request aiohttp timeout
    ) -> Any:
        """Call the gateway and unwrap the {status, message, code, data} envelope."""
        attempts = retries if retries is not None else (RETRIES if method == "GET" else 1)
        url = f"{self._gateway}{endpoint}"
        for attempt in range(1, attempts + 1):
            try:
                return await self._request_once(
                    method, url, json=json, auth=auth, login=login,
                    timeout=timeout or self._timeout,
                )  # fmt: skip
            except FilenConnectionError as err:
                if attempt == attempts:
                    raise
                wait = _retry_wait(attempt, err)
                _LOGGER.debug("%s %s failed (%s), retry in %.1fs", method, endpoint, err, wait)
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        json: Any,
        auth: bool,
        login: bool,
        timeout: aiohttp.ClientTimeout,  # noqa: ASYNC109
    ) -> Any:
        headers = self._bearer() if auth else {}
        what = f"{method} {url}"
        try:
            async with self._session.request(
                method, url, headers=headers, json=json, timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    await _raise_for_status(resp, what, login=login)
                text = await resp.text()
        except TimeoutError as err:
            raise FilenConnectionError(f"timeout on {what}") from err
        except aiohttp.ClientError as err:
            raise FilenConnectionError(f"{what}: {type(err).__name__}: {err}") from err
        try:
            body = jsonlib.loads(text) if text else None
        except ValueError as err:
            raise FilenError(f"{what}: response is not JSON") from err
        if isinstance(body, dict) and body.get("status") is False:
            _raise_api_error(
                str(body.get("code") or ""), str(body.get("message") or ""),
                what, login=login,
            )  # fmt: skip
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # ------------------------------------------------------------------ auth

    @_translate_unexpected
    async def auth_info(self, email: str) -> AuthInfo:
        """First login step: the password salt and auth version of the account."""
        data = await self._request(
            "POST", "/v3/auth/info", json={"email": email}, auth=False, login=True
        )
        return AuthInfo(
            salt=data["salt"],
            auth_version=int(data["authVersion"]),
            user_id=int(data["id"]),
        )

    @_translate_unexpected
    async def login(
        self, email: str, password: str, tfa_code: str | None = None
    ) -> Credentials:
        """Log in and set up all keys. The password is not kept.

        Failed attempts are rate limited by Filen, so never retry automatically.
        """
        info = await self.auth_info(email)
        master_key, derived_password = await asyncio.to_thread(
            crypto.derive_login_keys, password, info.auth_version, info.salt
        )
        data = await self._request(
            "POST",
            "/v3/login",
            json={
                "email": email,
                "password": derived_password,
                "twoFactorCode": tfa_code or NO_2FA_CODE,
                "authVersion": info.auth_version,
            },
            auth=False,
            login=True,
        )
        self._credentials = Credentials(
            api_key=data["apiKey"],
            master_keys=[master_key],
            auth_version=info.auth_version,
            email=email,
            base_folder_uuid="",
        )
        user, base_folder = await asyncio.gather(self.user_info(), self.base_folder())
        if info.auth_version == 2:
            await self._update_master_keys(master_key)
        else:
            await self._setup_dek(master_key)
        private_key = await self._setup_key_pair()
        self._credentials = Credentials(
            api_key=self.credentials.api_key,
            master_keys=self.credentials.master_keys,
            auth_version=info.auth_version,
            email=email,
            base_folder_uuid=base_folder or user.base_folder_uuid,
            private_key=private_key,
        )
        return self._credentials

    async def _update_master_keys(self, current_key: str) -> None:
        """Fetch the list of all master keys (older ones survive password changes)."""
        encrypted = await asyncio.to_thread(
            crypto.encrypt_metadata, current_key, current_key, 2
        )
        data = await self._request(
            "POST", "/v3/user/masterKeys", json={"masterKeys": encrypted}
        )
        joined = await asyncio.to_thread(
            crypto.decrypt_metadata, data["keys"], self.credentials.master_keys
        )
        keys = self.credentials.master_keys
        for key in joined.split("|"):
            if key and key not in keys:
                keys.append(key)

    async def _setup_dek(self, kek: str) -> None:
        """Auth version 3: the master key is a DEK, encrypted with the login key."""
        data = await self._request("GET", "/v3/user/dek")
        encrypted_dek = data.get("dek") if isinstance(data, dict) else None
        if encrypted_dek:
            dek = await asyncio.to_thread(crypto.decrypt_metadata, encrypted_dek, [kek])
        else:
            dek = crypto.random_hex(64)
            encrypted = await asyncio.to_thread(crypto.encrypt_metadata, dek, kek, 3)
            await self._request("POST", "/v3/user/dek", json={"dek": encrypted})
        self.credentials.master_keys[:] = [dek]

    async def _setup_key_pair(self) -> str:
        """Fetch (or create) the RSA keypair; the private key feeds v3 name hashes."""
        data = await self._request("GET", "/v3/user/keyPair/info")
        public_key = data.get("publicKey") if isinstance(data, dict) else None
        private_encrypted = data.get("privateKey") if isinstance(data, dict) else None
        if public_key and private_encrypted:
            return await asyncio.to_thread(
                crypto.decrypt_metadata,
                private_encrypted,
                self.credentials.master_keys,
            )
        public_key, private_key = await asyncio.to_thread(crypto.generate_key_pair)
        encrypted = await asyncio.to_thread(
            crypto.encrypt_metadata,
            private_key,
            self.credentials.master_keys[-1],
            self._meta_version,
        )
        await self._request(
            "POST",
            "/v3/user/keyPair/set",
            json={"publicKey": public_key, "privateKey": encrypted},
        )
        return private_key

    async def _name_hash(self, name: str) -> str:
        if self.credentials.auth_version == 3 and self._hmac_key is None:
            if not self.credentials.private_key:
                self.credentials.private_key = await self._setup_key_pair()
            self._hmac_key = await asyncio.to_thread(
                crypto.hmac_key_from_private_key, self.credentials.private_key
            )
        return await asyncio.to_thread(
            crypto.hash_name, name, self.credentials.auth_version, self._hmac_key
        )

    # ------------------------------------------------------------------ account

    @_translate_unexpected
    async def user_info(self) -> User:
        data = await self._request("GET", "/v3/user/info")
        self._user = User.from_api(data)
        return self._user

    @_translate_unexpected
    async def base_folder(self) -> str:
        data = await self._request("GET", "/v3/user/baseFolder")
        return data["uuid"]

    # ------------------------------------------------------------------ folders

    @_translate_unexpected
    async def list_dir(self, folder_uuid: str) -> tuple[list[DriveFile], list[DriveFolder]]:
        """Files and subfolders of a folder, with decrypted names."""
        data = await self._request(
            "POST", "/v3/dir/content", json={"uuid": folder_uuid}, retries=RETRIES
        )
        keys = self.credentials.master_keys
        files: list[DriveFile] = []
        for item in data.get("uploads") or []:
            try:
                metadata = jsonlib.loads(
                    await asyncio.to_thread(crypto.decrypt_metadata, item["metadata"], keys)
                )
            except (FilenCryptoError, ValueError) as err:
                _LOGGER.warning("Skipping file %s with unreadable metadata: %s", item.get("uuid"), err)
                continue
            files.append(DriveFile.from_api(item, metadata))
        folders: list[DriveFolder] = []
        for item in data.get("folders") or []:
            try:
                metadata = jsonlib.loads(
                    await asyncio.to_thread(crypto.decrypt_metadata, item["name"], keys)
                )
                name = metadata["name"]
            except (FilenCryptoError, ValueError, KeyError) as err:
                _LOGGER.warning("Skipping folder %s with unreadable name: %s", item.get("uuid"), err)
                continue
            folders.append(
                DriveFolder(uuid=item["uuid"], name=name, parent_uuid=item.get("parent"))
            )
        return files, folders

    async def list_files(self, folder_uuid: str) -> list[DriveFile]:
        files, _ = await self.list_dir(folder_uuid)
        return files

    async def list_folders(self, folder_uuid: str) -> list[DriveFolder]:
        _, folders = await self.list_dir(folder_uuid)
        return folders

    @_translate_unexpected
    async def create_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        folder_uuid = str(uuidlib.uuid4())
        metadata = await asyncio.to_thread(
            crypto.encrypt_metadata,
            jsonlib.dumps({"name": name}),
            self.credentials.master_keys[-1],
            self._meta_version,
        )
        await self._request(
            "POST",
            "/v3/dir/create",
            json={
                "uuid": folder_uuid,
                "name": metadata,
                "nameHashed": await self._name_hash(name),
                "parent": parent_uuid,
            },
        )
        return DriveFolder(uuid=folder_uuid, name=name, parent_uuid=parent_uuid)

    async def ensure_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        data = await self._request(
            "POST",
            "/v3/dir/exists",
            json={"parent": parent_uuid, "nameHashed": await self._name_hash(name)},
        )
        if data.get("exists") and data.get("uuid"):
            return DriveFolder(uuid=data["uuid"], name=name, parent_uuid=parent_uuid)
        return await self.create_folder(parent_uuid, name)

    async def ensure_folder_path(self, parent_uuid: str, path: str) -> str:
        """Create every folder of a slash-separated path below parent; return the last uuid."""
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            current = (await self.ensure_folder(current, part)).uuid
        return current

    async def find_folder_path(self, parent_uuid: str, path: str) -> str | None:
        """Uuid of an existing slash-separated folder path below parent, or None."""
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            data = await self._request(
                "POST",
                "/v3/dir/exists",
                json={"parent": current, "nameHashed": await self._name_hash(part)},
            )
            if not data.get("exists") or not data.get("uuid"):
                return None
            current = data["uuid"]
        return current

    # ------------------------------------------------------------------ files

    @_translate_unexpected
    async def file_meta(self, file_uuid: str) -> DriveFile:
        """Metadata of one file by uuid (POST /v3/file does not return chunks)."""
        data = await self._request("POST", "/v3/file", json={"uuid": file_uuid})
        metadata = jsonlib.loads(
            await asyncio.to_thread(
                crypto.decrypt_metadata, data["metadata"], self.credentials.master_keys
            )
        )
        file = DriveFile.from_api(data, metadata)
        if not file.chunks and file.size:
            file = replace(file, chunks=math.ceil(file.size / MIB))
        return file

    @_translate_unexpected
    async def find_file(self, folder_uuid: str, name: str) -> DriveFile | None:
        """File with this name in a folder, or None."""
        for file in await self.list_files(folder_uuid):
            if file.name == name:
                return file
        return None

    @_translate_unexpected
    async def trash_file(self, file_uuid: str) -> None:
        """Move a file to the Filen trash (recoverable there)."""
        await self._request("POST", "/v3/file/trash", json={"uuid": file_uuid})

    @_translate_unexpected
    async def delete_file(self, file_uuid: str) -> None:
        """Delete permanently (not to trash)."""
        await self._request("POST", "/v3/file/delete/permanent", json={"uuid": file_uuid})

    # ------------------------------------------------------------------ upload

    async def upload(
        self,
        stream: AsyncIterator[bytes],
        *,
        size: int,
        folder_uuid: str,
        name: str,
        mime: str = "application/octet-stream",
        progress: ProgressCallback | None = None,
    ) -> DriveFile:
        """Encrypt and upload a stream of exactly `size` bytes.

        Errors from `stream` itself are raised as FilenStreamError. The stream
        is closed afterwards (if it has `aclose`), also when the upload fails.
        """
        try:
            return await self._upload(stream, size, folder_uuid, name, mime, progress)
        finally:
            if (aclose := getattr(stream, "aclose", None)) is not None:
                await aclose()

    async def _upload(
        self,
        stream: AsyncIterator[bytes],
        size: int,
        folder_uuid: str,
        name: str,
        mime: str,
        progress: ProgressCallback | None,
    ) -> DriveFile:
        version = 3 if self.credentials.auth_version == 3 else 2
        file_uuid = str(uuidlib.uuid4())
        key = crypto.random_hex(64) if version == 3 else crypto.random_string(32)
        upload_key = crypto.random_string(32)
        rm = crypto.random_string(32)
        now = _now_ms()
        sha = hashlib.sha512()
        source = _SourceReader(stream)
        chunks = math.ceil(size / MIB)
        bucket = region = ""

        sent = 0
        for index in range(chunks):
            expected = min(MIB, size - sent)
            plain = bytearray(expected)
            n = await source.readinto(memoryview(plain))
            if n != expected:
                raise FilenError(f"stream ended after {sent + n} of {size} bytes")
            sha.update(memoryview(plain)[:n])
            encrypted = await _encrypt_chunk(bytes(plain[:n]), key, version)
            bucket, region = await self._upload_chunk(
                file_uuid, index, folder_uuid, upload_key, encrypted
            )
            sent += n
            if progress:
                progress(sent)
        if chunks and await source.readinto(memoryview(bytearray(1))):
            raise FilenError(f"stream is longer than the announced {size} bytes")

        metadata = {
            "name": name,
            "size": size,
            "mime": mime,
            "key": key,
            "lastModified": now,
            "creation": now,
            "hash": sha.hexdigest(),
        }
        master_key = self.credentials.master_keys[-1]
        name_enc, mime_enc, size_enc, meta_enc, name_hashed = await asyncio.gather(
            asyncio.to_thread(crypto.encrypt_metadata, name, key, version),
            asyncio.to_thread(crypto.encrypt_metadata, mime, key, version),
            asyncio.to_thread(crypto.encrypt_metadata, str(size), key, version),
            asyncio.to_thread(
                crypto.encrypt_metadata, jsonlib.dumps(metadata), master_key,
                self._meta_version,
            ),  # fmt: skip
            self._name_hash(name),
        )
        if size > 0:
            await self._request(
                "POST",
                "/v3/upload/done",
                json={
                    "uuid": file_uuid,
                    "name": name_enc,
                    "nameHashed": name_hashed,
                    "size": size_enc,
                    "chunks": chunks,
                    "mime": mime_enc,
                    "rm": rm,
                    "metadata": meta_enc,
                    "version": version,
                    "uploadKey": upload_key,
                },
            )
        else:
            await self._request(
                "POST",
                "/v3/upload/empty",
                json={
                    "uuid": file_uuid,
                    "name": name_enc,
                    "nameHashed": name_hashed,
                    "size": size_enc,
                    "mime": mime_enc,
                    "metadata": meta_enc,
                    "version": version,
                    "parent": folder_uuid,
                },
            )
        return DriveFile(
            uuid=file_uuid,
            name=name,
            size=size,
            mime=mime,
            key=key,
            chunks=chunks,
            bucket=bucket,
            region=region,
            parent_uuid=folder_uuid,
            version=version,
            last_modified=now,
            creation=now,
            hash=sha.hexdigest(),
            rm=rm,
            timestamp=now,
        )

    async def _upload_chunk(
        self,
        file_uuid: str,
        index: int,
        parent_uuid: str,
        upload_key: str,
        encrypted: bytes,
    ) -> tuple[str, str]:
        params = {
            "uuid": file_uuid,
            "index": str(index),
            "parent": parent_uuid,
            "uploadKey": upload_key,
            "hash": hashlib.sha512(encrypted).hexdigest(),
        }
        for attempt in range(1, RETRIES + 1):
            url = f"{random.choice(self._ingest_urls)}/v3/upload"  # noqa: S311
            try:
                async with self._session.post(
                    url,
                    params=params,
                    data=encrypted,
                    headers={
                        **self._bearer(),
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=self._transfer_timeout,
                ) as resp:
                    if resp.status >= 400:
                        await _raise_for_status(resp, "chunk upload")
                    body = await resp.json(content_type=None)
            except TimeoutError as err:
                error: FilenConnectionError = FilenConnectionError("timeout during upload")
                error.__cause__ = err
            except aiohttp.ClientError as err:
                error = FilenConnectionError(f"upload failed: {type(err).__name__}")
                error.__cause__ = err
            else:
                if isinstance(body, dict) and body.get("status") is False:
                    _raise_api_error(
                        str(body.get("code") or ""), str(body.get("message") or ""),
                        "chunk upload",
                    )  # fmt: skip
                data = body.get("data") if isinstance(body, dict) and "data" in body else body
                try:
                    return data["bucket"], data["region"]
                except (KeyError, TypeError) as err:
                    raise FilenError("chunk upload: response missing bucket/region") from err
            if attempt == RETRIES:
                raise error
            wait = _retry_wait(attempt, error)
            _LOGGER.debug("Chunk upload failed (%s), retry in %.1fs", error, wait)
            await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def upload_bytes(
        self, data: bytes, *, folder_uuid: str, name: str, mime: str = "application/octet-stream"
    ) -> DriveFile:
        async def gen() -> AsyncIterator[bytes]:
            yield data

        return await self.upload(
            gen(), size=len(data), folder_uuid=folder_uuid, name=name, mime=mime
        )

    # ------------------------------------------------------------------ download

    async def download(
        self, file: DriveFile, *, start: int = 0, end: int | None = None
    ) -> DownloadStream:
        """Return a stream of the decrypted file; iterate it or close it with `aclose()`.

        `start`/`end` (inclusive) select a byte range, e.g. for seeking in videos.
        For a full download the stored SHA-512 is checked after the last byte; a
        mismatch raises FilenIntegrityError instead of ending the iteration.
        """
        if end is None or end >= file.size:
            end = file.size - 1
        if file.size and (start < 0 or start > end):
            raise FilenError(f"invalid range {start}-{end} for {file.size} bytes")
        return DownloadStream(self, file, start, end)

    async def download_bytes(self, file: DriveFile) -> bytes:
        stream = await self.download(file)
        async with stream:
            return b"".join([chunk async for chunk in stream])

    async def _download_chunk(self, file: DriveFile, index: int) -> bytes:
        for attempt in range(1, RETRIES + 1):
            url = (
                f"{random.choice(self._egest_urls)}/"  # noqa: S311
                f"{file.region}/{file.bucket}/{file.uuid}/{index}"
            )
            try:
                async with self._session.get(
                    url, headers=self._bearer(), timeout=self._transfer_timeout
                ) as resp:
                    if resp.status >= 400:
                        await _raise_for_status(resp, "chunk download")
                    return await resp.read()
            except TimeoutError as err:
                error: FilenConnectionError = FilenConnectionError("timeout during download")
                error.__cause__ = err
            except aiohttp.ClientError as err:
                error = FilenConnectionError(f"download failed: {type(err).__name__}")
                error.__cause__ = err
            if attempt == RETRIES:
                raise error
            wait = _retry_wait(attempt, error)
            _LOGGER.debug("Chunk download failed (%s), retry in %.1fs", error, wait)
            await asyncio.sleep(wait)
        raise AssertionError("unreachable")


# ---------------------------------------------------------------------- streams


class DownloadStream:
    """Decrypting download stream over Filen's 1 MiB chunks, with byte ranges."""

    def __init__(self, client: FilenClient, file: DriveFile, start: int, end: int) -> None:
        self._client = client
        self._file = file
        self._start = start
        self._end = end
        self._index = start // MIB if file.size else 0
        self._last = end // MIB if file.size else -1
        self._buf = b""
        self._done = file.size == 0
        full = start == 0 and end == file.size - 1
        self._sha = hashlib.sha512() if full and file.hash else None

    def __aiter__(self) -> DownloadStream:
        return self

    async def __anext__(self) -> bytes:
        while not self._done:
            if self._buf:
                chunk, self._buf = self._buf, b""
                return chunk
            if self._index > self._last:
                self._finish()
                raise StopAsyncIteration
            offset = self._index * MIB
            encrypted = await self._client._download_chunk(self._file, self._index)
            plain = await _decrypt_chunk(encrypted, self._file.key, self._file.version)
            if self._sha is not None:
                self._sha.update(plain)
            lo = self._start - offset if self._index == self._start // MIB else 0
            hi = self._end - offset + 1 if self._index == self._last else len(plain)
            self._buf = plain[lo:hi]
            self._index += 1
        raise StopAsyncIteration

    def _finish(self) -> None:
        self._done = True
        if self._sha is not None and self._sha.hexdigest() != self._file.hash:
            raise FilenIntegrityError("downloaded data does not match the stored hash")

    async def aclose(self) -> None:
        self._done = True

    async def __aenter__(self) -> DownloadStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._done = True


class _SourceReader:
    """Reads a caller's async byte stream into fixed buffers."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._it = stream.__aiter__()
        self._pending = memoryview(b"")
        self._eof = False

    async def readinto(self, buf: memoryview) -> int:
        filled = 0
        while filled < len(buf):
            if not len(self._pending):
                if self._eof:
                    break
                try:
                    chunk = await self._it.__anext__()
                except StopAsyncIteration:
                    self._eof = True
                    break
                except FilenError:
                    raise
                except Exception as err:
                    raise FilenStreamError(
                        f"reading the source stream failed: {type(err).__name__}"
                    ) from err
                self._pending = memoryview(chunk)
                continue
            n = min(len(buf) - filled, len(self._pending))
            buf[filled : filled + n] = self._pending[:n]
            self._pending = self._pending[n:]
            filled += n
        return filled


async def _encrypt_chunk(plain: bytes, key: str, version: int) -> bytes:
    if len(plain) >= THREAD_THRESHOLD:
        return await asyncio.to_thread(crypto.encrypt_chunk, plain, key, version)
    return crypto.encrypt_chunk(plain, key, version)


async def _decrypt_chunk(encrypted: bytes, key: str, version: int) -> bytes:
    try:
        if len(encrypted) >= THREAD_THRESHOLD:
            return await asyncio.to_thread(crypto.decrypt_chunk, encrypted, key, version)
        return crypto.decrypt_chunk(encrypted, key, version)
    except FilenCryptoError as err:
        raise FilenIntegrityError(str(err)) from err


# ---------------------------------------------------------------------- errors


def _raise_api_error(code: str, message: str, what: str, *, login: bool = False) -> None:
    """Map a {status: false} envelope to our exception hierarchy."""
    text = f"{code} {message}".lower()
    msg = f"{what}: {code}: {message}" if code else f"{what}: {message}"
    if any(s in text for s in ("2fa", "two factor", "two-factor", "totp", "authenticator")):
        raise FilenTwoFactorRequired(msg)
    if any(s in text for s in ("blocked", "locked", "banned", "disabled")):
        raise FilenAccountBlocked(msg)
    if login:
        # During login, anything else (wrong password, unknown e-mail, ...) is an auth problem.
        raise FilenAuthError(msg)
    if any(s in text for s in ("quota", "storage full", "not enough space", "max storage")):
        raise FilenQuotaError(msg)
    if any(s in text for s in ("not found", "does not exist", "no such")):
        raise FilenNotFoundError(msg)
    if "already exists" in text:
        raise FilenConflictError(msg)
    if any(
        s in text
        for s in ("api key", "apikey", "unauthorized", "not authorized", "invalid key", "session")
    ):
        raise FilenAuthError(msg)
    if any(
        s in text for s in ("password", "credentials", "invalid email", "wrong email", "login")
    ):
        raise FilenAuthError(msg)
    raise FilenError(msg)


async def _raise_for_status(
    resp: aiohttp.ClientResponse, what: str, *, login: bool = False
) -> None:
    status = resp.status
    try:
        text = await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError, TimeoutError):
        text = ""
    body: Any = None
    with contextlib.suppress(ValueError):
        body = jsonlib.loads(text) if text else None
    if isinstance(body, dict) and body.get("status") is False:
        _raise_api_error(
            str(body.get("code") or ""), str(body.get("message") or ""), what, login=login
        )
    message = body.get("message") if isinstance(body, dict) else text[:200]
    msg = f"{what}: HTTP {status}: {message}"
    if status == 401:
        raise FilenAuthError(msg)
    if status == 404:
        raise FilenNotFoundError(msg)
    if status == 409:
        raise FilenConflictError(msg)
    if status == 413:
        raise FilenQuotaError(msg)
    if status == 429:
        retry = resp.headers.get("Retry-After", "")
        raise FilenRateLimitError(msg, float(retry) if retry.isdigit() else None)
    if status == 408 or status >= 500:
        raise FilenConnectionError(msg)
    if login:
        raise FilenAuthError(msg)
    raise FilenError(msg)