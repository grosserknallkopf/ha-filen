"""Browse and play files from Filen in the Home Assistant media browser."""

from __future__ import annotations

import logging
import mimetypes
import re
from time import monotonic
from typing import override

from aiohttp import web

from homeassistant.components import http
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import FilenConfigEntry
from .aiofilen import (
    DownloadStream,
    DriveFile,
    FilenAuthError,
    FilenError,
    FilenNotFoundError,
)
from .const import DOMAIN, MEDIA_VIEW_URL

_LOGGER = logging.getLogger(__name__)

META_TTL = 300  # files can be replaced or deleted in Filen at any time
META_CACHE_SIZE = 500

MEDIA_CLASSES = {
    "image": MediaClass.IMAGE,
    "video": MediaClass.VIDEO,
    "audio": MediaClass.MUSIC,
}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the Filen media source."""
    hass.http.register_view(FilenMediaView(hass))
    return FilenMediaSource(hass)


def _mime(name: str) -> str | None:
    mime, _ = mimetypes.guess_type(name)
    return mime if mime and mime.split("/")[0] in MEDIA_CLASSES else None


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> FilenConfigEntry | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
    ):
        return None
    return entry


class FilenMediaSource(MediaSource):
    """Filen folders with images, videos and audio.

    Identifiers: "" (accounts), "<entry_id>" (root), "<entry_id>/f/<folder_uuid>/<folder name>",
    "<entry_id>/i/<file_uuid>/<file name>".
    """

    name = "Filen"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    @override
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/", 3)
        if len(parts) != 4 or parts[1] != "i":
            raise Unresolvable(f"Not a file: {item.identifier}")
        entry_id, _, file_uuid, name = parts
        mime = _mime(name)
        if mime is None or _loaded_entry(self.hass, entry_id) is None:
            raise Unresolvable(f"Cannot play {name}")
        # Only id and extension go into the URL: Home Assistant decodes the path when it
        # signs it, and characters like "#" or "?" in a file name would break the link.
        ext = "".join(re.findall(r"\.[A-Za-z0-9]{1,8}$", name)).lower()
        return PlayMedia(f"{MEDIA_VIEW_URL}/{entry_id}/{file_uuid}/media{ext}", mime)

    @override
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        entries: list[FilenConfigEntry] = self.hass.config_entries.async_loaded_entries(
            DOMAIN
        )
        if not entries:
            raise BrowseError("Filen is not set up")
        if not item.identifier:
            if len(entries) == 1:
                return await self._browse_folder(entries[0], None, "Filen")
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=None,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title="Filen",
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
                children=[self._folder_node(e, None, e.title) for e in entries],
            )
        parts = item.identifier.split("/", 3)
        entry = _loaded_entry(self.hass, parts[0])
        if entry is None:
            raise BrowseError("Filen account not loaded")
        if len(parts) >= 3 and parts[1] == "f":
            return await self._browse_folder(
                entry, parts[2], parts[3] if len(parts) == 4 else ""
            )
        return await self._browse_folder(entry, None, entry.title)

    def _folder_node(
        self, entry: FilenConfigEntry, folder_uuid: str | None, title: str
    ) -> BrowseMediaSource:
        identifier = (
            entry.entry_id
            if folder_uuid is None
            else f"{entry.entry_id}/f/{folder_uuid}/{title}"
        )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
        )

    def _file_node(
        self, entry: FilenConfigEntry, file: DriveFile, mime: str
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry.entry_id}/i/{file.uuid}/{file.name}",
            media_class=MEDIA_CLASSES[mime.split("/")[0]],
            media_content_type=mime,
            title=file.name,
            can_play=True,
            can_expand=False,
        )

    async def _browse_folder(
        self, entry: FilenConfigEntry, folder_uuid: str | None, title: str
    ) -> BrowseMediaSource:
        client = entry.runtime_data.client
        uuid = folder_uuid or client.root_folder_uuid
        try:
            files, folders = await client.list_dir(uuid)
        except FilenError as err:
            raise BrowseError(f"Filen: {err}") from err
        children = [
            self._folder_node(entry, f.uuid, f.name)
            for f in sorted(folders, key=lambda f: f.name.lower())
        ]
        for file in sorted(files, key=lambda f: f.name.lower()):
            if mime := _mime(file.name):
                children.append(self._file_node(entry, file, mime))
        node = self._folder_node(entry, folder_uuid, title)
        node.children = children
        node.children_media_class = MediaClass.DIRECTORY
        return node


class FilenMediaView(http.HomeAssistantView):
    """Stream a decrypted Filen file, with HTTP range support for seeking.

    Filen stores files in encrypted 1 MiB chunks; a range is served by
    decrypting the chunks it touches and cutting out the requested bytes.
    The media type comes from the file in Filen, never from the URL, so the
    view only serves images, videos and audio (not, for example, backups).
    """

    url = MEDIA_VIEW_URL + "/{entry_id}/{file_uuid}/{name}"
    name = "api:filen:media"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._meta: dict[tuple[str, str], tuple[DriveFile, float]] = {}

    async def head(
        self, request: web.Request, entry_id: str, file_uuid: str, name: str
    ) -> web.StreamResponse:
        return await self._serve(request, entry_id, file_uuid, head=True)

    async def get(
        self, request: web.Request, entry_id: str, file_uuid: str, name: str
    ) -> web.StreamResponse:
        return await self._serve(request, entry_id, file_uuid, head=False)

    async def _file(
        self, entry: FilenConfigEntry, file_uuid: str, *, fresh: bool = False
    ) -> DriveFile:
        key = (entry.entry_id, file_uuid)
        cached = self._meta.get(key)
        if cached and not fresh and monotonic() - cached[1] < META_TTL:
            return cached[0]
        file = await entry.runtime_data.client.file_meta(file_uuid)
        self._meta[key] = (file, monotonic())
        if len(self._meta) > META_CACHE_SIZE:
            self._meta.pop(next(iter(self._meta)))
        return file

    def _failed(
        self, entry: FilenConfigEntry, err: FilenError
    ) -> web.HTTPException:
        if isinstance(err, FilenNotFoundError):
            return web.HTTPNotFound()
        if isinstance(err, FilenAuthError):
            entry.async_start_reauth(self.hass)
        _LOGGER.warning("Filen media request failed: %s", err)
        return web.HTTPBadGateway()

    async def _serve(
        self, request: web.Request, entry_id: str, file_uuid: str, *, head: bool
    ) -> web.StreamResponse:
        entry = _loaded_entry(self.hass, entry_id)
        if entry is None:
            raise web.HTTPNotFound
        try:
            file = await self._file(entry, file_uuid)
        except FilenError as err:
            raise self._failed(entry, err) from err
        mime = _mime(file.name)
        if mime is None:
            raise web.HTTPNotFound

        try:
            start, end = _parse_range(request, file.size)
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{file.size}"}
            ) from None
        partial = (start, end) != (0, file.size - 1)
        response = web.StreamResponse(status=206 if partial else 200)
        response.content_type = mime
        response.content_length = max(end - start + 1, 0)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, max-age=3600"
        if partial:
            response.headers["Content-Range"] = f"bytes {start}-{end}/{file.size}"
        if head or file.size == 0:
            await response.prepare(request)
            return response

        stream = await self._open(entry, file, start, end)
        async with stream:
            await response.prepare(request)
            try:
                async for chunk in stream:
                    await response.write(chunk)
            except FilenError as err:
                # headers with the full length are already out: close the connection so
                # the player sees the error right away instead of waiting for bytes
                _LOGGER.warning("Filen media stream interrupted: %s", err)
                response.force_close()
                return response
            except ConnectionResetError:
                return response  # player closed the connection, e.g. while seeking
        await response.write_eof()
        return response

    async def _open(
        self, entry: FilenConfigEntry, file: DriveFile, start: int, end: int
    ) -> DownloadStream:
        client = entry.runtime_data.client
        try:
            return await client.download(file, start=start, end=end)
        except FilenNotFoundError:
            pass  # cached metadata may point to a replaced file: retry with fresh data
        except FilenError as err:
            raise self._failed(entry, err) from err
        try:
            fresh = await self._file(entry, file.uuid, fresh=True)
            if fresh.size != file.size:
                raise web.HTTPNotFound  # the file changed; let the player start over
            return await client.download(fresh, start=start, end=end)
        except FilenError as err:
            raise self._failed(entry, err) from err


def _parse_range(request: web.Request, size: int) -> tuple[int, int]:
    """Byte range of the request as (start, end) inclusive; ValueError if unsatisfiable."""
    rng = request.http_range  # raises ValueError for malformed or multiple ranges
    if rng.start is None and rng.stop is None:
        return 0, size - 1
    if rng.start is not None and rng.start < 0:  # suffix range "bytes=-N"
        return max(size + rng.start, 0), size - 1
    start = rng.start or 0
    end = size - 1 if rng.stop is None else min(rng.stop - 1, size - 1)
    if start > end or start >= size:
        raise ValueError("range not satisfiable")
    return start, end