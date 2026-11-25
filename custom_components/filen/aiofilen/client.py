"""Async client for Filen (filen.io), backed by the official Rust SDK.

The protocol, cryptography and transfer logic live in `filen-sdk-rs`; this
module is the small Python facade Home Assistant uses. It keeps the old
aiofilen API (`login`, `list_dir`, `upload`, `download`, ...) so the
integration, backup agent, services and media source stay unchanged.

Uploads and downloads are still streaming: an async iterator is pumped into a
bounded queue that the native source reads from, and native download bytes are
pushed into a bounded asyncio queue while the HTTP response consumes them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
import asyncio
import contextlib
import logging
import queue
import threading
from typing import Any

from . import _native
from .exceptions import (
    FilenAuthError,
    FilenError,
    FilenStreamError,
)
from .models import Credentials, DriveFile, DriveFolder, User

_LOGGER = logging.getLogger(__name__)

GATEWAY_URL = "https://gateway.filen.io"
MIB = 1024 * 1024

ProgressCallback = Callable[[int], None]

READ_CHUNK = 1024 * 1024
TRANSFER_SLOTS = 4

_EOF = object()
_ERROR = object()
_CLOSED = object()


class FilenClient:
    """Filen client backed by the official Filen Rust SDK.

    `session`, `gateway_url`, `ingest_urls`, `egest_urls` and `request_timeout`
    are accepted for backwards compatibility; the native SDK owns its HTTP
    client and transfer configuration now.
    """

    def __init__(
        self,
        session: Any = None,
        *,
        gateway_url: str = GATEWAY_URL,
        ingest_urls: tuple[str, ...] = (),
        egest_urls: tuple[str, ...] = (),
        request_timeout: float = 60,
    ) -> None:
        self._session = session
        self._gateway = gateway_url
        self._ingest_urls = ingest_urls
        self._egest_urls = egest_urls
        self._timeout = request_timeout
        self._credentials: Credentials | None = None
        self._user: User | None = None
        self._native_client: Any = None
        self._native_lock = asyncio.Lock()

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
        self._native_client = None

    async def _client(self) -> Any:
        """The native client, created once from the stored credentials."""
        if self._native_client is not None:
            return self._native_client
        async with self._native_lock:
            if self._native_client is None:
                native = _native.load()
                credentials = _native.credentials_to_native(self.credentials)
                constructor = getattr(native.SdkClient, "from_credentials", None)
                if constructor is None:
                    constructor = native.SdkClient
                try:
                    self._native_client = await asyncio.to_thread(
                        constructor, credentials
                    )
                except Exception as err:
                    raise _native.translate_error(err) from err
        return self._native_client

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        client = await self._client()
        try:
            return await getattr(client, method)(*args, **kwargs)
        except Exception as err:
            raise _native.translate_error(err) from err

    # ------------------------------------------------------------------ auth

    async def login(
        self, email: str, password: str, tfa_code: str | None = None
    ) -> Credentials:
        """Log in through the official SDK and return the persisted credentials.

        The password is not stored. Failed attempts are rate limited by Filen,
        so they are never retried automatically.
        """
        native = _native.load()
        try:
            result = await native.login(email, password, tfa_code)
        except Exception as err:
            raise _native.translate_error(err) from err
        self._credentials = Credentials(**_native.credentials_fields(result.credentials))
        self._user = User(**_native.user_fields(result.user))
        self._native_client = None
        return self._credentials

    # ------------------------------------------------------------------ account

    async def user_info(self) -> User:
        data = await self._call("user_info")
        self._user = User(**_native.user_fields(data))
        return self._user

    async def base_folder(self) -> str:
        return self.root_folder_uuid

    # ------------------------------------------------------------------ folders

    async def list_dir(
        self, folder_uuid: str
    ) -> tuple[list[DriveFile], list[DriveFolder]]:
        listing = await self._call("list_dir", folder_uuid)
        files = [DriveFile(**_native.file_fields(file)) for file in listing.files]
        folders = [
            DriveFolder(**_native.folder_fields(folder)) for folder in listing.folders
        ]
        return files, folders

    async def list_files(self, folder_uuid: str) -> list[DriveFile]:
        files, _ = await self.list_dir(folder_uuid)
        return files

    async def list_folders(self, folder_uuid: str) -> list[DriveFolder]:
        _, folders = await self.list_dir(folder_uuid)
        return folders

    async def create_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        data = await self._call("create_folder", parent_uuid, name)
        return DriveFolder(**_native.folder_fields(data))

    async def ensure_folder(self, parent_uuid: str, name: str) -> DriveFolder:
        data = await self._call("ensure_folder", parent_uuid, name)
        return DriveFolder(**_native.folder_fields(data))

    async def ensure_folder_path(self, parent_uuid: str, path: str) -> str:
        """Create every folder of a slash-separated path below parent; return the last uuid."""
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            current = (await self.ensure_folder(current, part)).uuid
        return current

    async def find_folder(self, parent_uuid: str, name: str) -> DriveFolder | None:
        data = await self._call("find_folder", parent_uuid, name)
        return None if data is None else DriveFolder(**_native.folder_fields(data))

    async def find_folder_path(self, parent_uuid: str, path: str) -> str | None:
        """Uuid of an existing slash-separated folder path below parent, or None."""
        current = parent_uuid
        for part in (p for p in path.strip("/").split("/") if p):
            folder = await self.find_folder(current, part)
            if folder is None:
                return None
            current = folder.uuid
        return current

    # ------------------------------------------------------------------ files

    async def file_meta(self, file_uuid: str) -> DriveFile:
        data = await self._call("file_meta", file_uuid)
        return DriveFile(**_native.file_fields(data))

    async def find_file(self, folder_uuid: str, name: str) -> DriveFile | None:
        for file in await self.list_files(folder_uuid):
            if file.name == name:
                return file
        return None

    async def trash_file(self, file_uuid: str) -> None:
        """Move a file to the Filen trash (recoverable there)."""
        await self._call("trash_file", file_uuid)

    async def delete_file(self, file_uuid: str) -> None:
        """Delete permanently (not to trash)."""
        await self._call("delete_file", file_uuid)

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
        """Upload exactly `size` bytes from an async byte stream.

        The stream is closed afterwards (if it has `aclose`), also when the
        upload fails.
        """
        loop = asyncio.get_running_loop()
        source = _AsyncByteSource(stream, loop)
        callback = None if progress is None else _ProgressAdapter(progress, loop)
        try:
            client = await self._client()
            result = await client.upload(
                source, size, folder_uuid, name, mime, callback
            )
        except Exception as err:
            if source.error is not None:
                raise FilenStreamError(
                    f"reading the source stream failed: "
                    f"{type(source.error).__name__}"
                ) from source.error
            raise _native.translate_error(err) from err
        finally:
            await source.aclose()
            if (aclose := getattr(stream, "aclose", None)) is not None:
                await aclose()
        if source.error is not None:
            raise FilenStreamError(
                f"reading the source stream failed: {type(source.error).__name__}"
            ) from source.error
        return DriveFile(**_native.file_fields(result))

    async def upload_bytes(
        self, data: bytes, *, folder_uuid: str, name: str,
        mime: str = "application/octet-stream",
    ) -> DriveFile:  # fmt: skip
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
        The official SDK verifies the stored BLAKE3 hash after a full download.
        """
        if end is None or end >= file.size:
            end = file.size - 1
        if file.size and (start < 0 or start > end):
            raise FilenError(f"invalid range {start}-{end} for {file.size} bytes")
        if file.size == 0:
            return DownloadStream(None, file, start, end)
        client = await self._client()
        return DownloadStream(client, file, start, end)

    async def download_bytes(self, file: DriveFile) -> bytes:
        stream = await self.download(file)
        async with stream:
            return b"".join([chunk async for chunk in stream])


# ---------------------------------------------------------------------- streams


class _AsyncByteSource:
    """Adapts a caller's async byte iterator to the native ByteSource callback."""

    def __init__(self, stream: AsyncIterator[bytes], loop: asyncio.AbstractEventLoop) -> None:
        self._stream = stream
        self._loop = loop
        # Do not use asyncio.to_thread(queue.put, ...) here. Home Assistant's
        # general executor can be saturated while a backup is prepared; then
        # the producer never places its first byte and the native reader waits
        # forever. The asyncio semaphore provides the same bounded buffering
        # without depending on an executor worker.
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._slots = asyncio.Semaphore(TRANSFER_SLOTS)
        self._closed = False
        self.error: BaseException | None = None
        self._buffer = b""
        self._task = loop.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for chunk in self._stream:
                if not chunk:
                    continue
                view = memoryview(chunk)
                for offset in range(0, len(view), READ_CHUNK):
                    if self._closed:
                        return
                    piece = bytes(view[offset : offset + READ_CHUNK])
                    await self._slots.acquire()
                    if self._closed:
                        self._slots.release()
                        return
                    self._queue.put(piece)
            self._queue.put(_EOF)
        except asyncio.CancelledError:
            raise
        except BaseException as err:  # noqa: BLE001 - reported via self.error
            self.error = err
            self._queue.put(_ERROR)

    def read(self, max_bytes: int) -> Any:
        """Poll for source bytes from the native upload thread."""
        max_bytes = int(max_bytes)
        if self._buffer:
            data, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
            return _native.load().SourceRead(data=data, eof=False)
        if self._closed:
            return _native.load().SourceRead(data=b"", eof=True)
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            # This callback holds Python's GIL. It must return rather than
            # wait for _pump(), which runs on the event loop.
            return _native.load().SourceRead(data=b"", eof=False)
        if item is _EOF or item is _ERROR:
            return _native.load().SourceRead(data=b"", eof=True)
        self._loop.call_soon_threadsafe(self._slots.release)
        self._buffer = item
        data, self._buffer = self._buffer[:max_bytes], self._buffer[max_bytes:]
        return _native.load().SourceRead(data=data, eof=False)

    async def aclose(self) -> None:
        self._closed = True
        if not self._task.done():
            self._task.cancel()
        with contextlib.suppress(BaseException):
            await self._task
        # Unblock a native reader that is waiting in queue.get().
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put(_EOF)


class _NativeByteSink:
    """Adapts the native download callback to a bounded asyncio queue."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        target: asyncio.Queue[Any],
        slots: threading.Semaphore,
    ) -> None:
        self._loop = loop
        self._target = target
        self._slots = slots
        self.closed = False

    def write(self, data: bytes) -> bool:
        if self.closed:
            return False
        self._slots.acquire()
        if self.closed:
            self._slots.release()
            return False
        try:
            self._loop.call_soon_threadsafe(self._target.put_nowait, data)
        except RuntimeError:
            self._slots.release()
            return False
        return True

    def finish(self) -> None:
        if self.closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._target.put_nowait, _EOF)
        except RuntimeError:
            pass


class _ProgressAdapter:
    """Marshals native (worker-thread) progress callbacks to the HA event loop."""

    def __init__(
        self, callback: ProgressCallback, loop: asyncio.AbstractEventLoop
    ) -> None:
        self._callback = callback
        self._loop = loop

    def on_progress(self, bytes_uploaded: int) -> None:
        try:
            self._loop.call_soon_threadsafe(self._callback, int(bytes_uploaded))
        except RuntimeError:
            pass


class DownloadStream:
    """Decrypting download stream backed by the native SDK, with byte ranges."""

    def __init__(
        self,
        client: Any | None,
        file: DriveFile,
        start: int,
        end: int,
    ) -> None:
        self._client = client
        self._file = file
        self._start = start
        self._end = end
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._slots = threading.Semaphore(TRANSFER_SLOTS)
        self._closed = False
        self._error: BaseException | None = None
        self._sink = _NativeByteSink(self._loop, self._queue, self._slots)
        self._task: asyncio.Task[None] | None = None
        if client is not None:
            self._task = self._loop.create_task(self._run())
        else:
            self._queue.put_nowait(_EOF)

    async def _run(self) -> None:
        assert self._client is not None
        try:
            # The old API uses an inclusive end; the SDK reader uses exclusive.
            await self._client.download(
                self._file.uuid,
                self._start,
                self._end + 1,
                self._sink,
                None,
            )
        except Exception as err:  # noqa: BLE001 - surfaced by __anext__
            if not self._closed:
                self._error = err
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(
                        self._queue.put_nowait, _ERROR
                    )
        finally:
            if not self._closed:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(
                        self._queue.put_nowait, _EOF
                    )

    def __aiter__(self) -> DownloadStream:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _CLOSED:
            self._closed = True
            raise StopAsyncIteration
        if item is _ERROR:
            self._closed = True
            await self._wait()
            if self._error is not None:
                raise _native.translate_error(self._error) from self._error
            raise FilenError("download failed")
        if item is _EOF:
            self._closed = True
            await self._wait()
            if self._error is not None:
                raise _native.translate_error(self._error) from self._error
            raise StopAsyncIteration
        self._slots.release()
        return item

    async def _wait(self) -> None:
        if self._task is None:
            return
        with contextlib.suppress(BaseException):
            await self._task

    async def aclose(self) -> None:
        self._closed = True
        self._sink.closed = True
        for _ in range(TRANSFER_SLOTS):
            self._slots.release()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_CLOSED)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def __aenter__(self) -> DownloadStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
