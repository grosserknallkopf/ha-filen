"""Tests for the media source: browse, resolve and stream with ranges."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.media_source import (
    URI_SCHEME,
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.filen.const import DOMAIN

from .common import MockConfigEntry
from .conftest import FakeClient


async def test_media_browse_resolve_and_stream(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: FakeClient,
    tmp_path: Path,
    hass_client,
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    video = tmp_path / "clip.mp4"
    video.write_bytes(bytes(range(256)) * 400)
    (tmp_path / "notes.pdf").write_bytes(b"%PDF")
    await hass.services.async_call(
        DOMAIN,
        "upload",
        {
            "config_entry_id": loaded.entry_id,
            "filename": [str(video), str(tmp_path / "notes.pdf")],
            "destination_folder": "Medien",
        },
        blocking=True,
    )

    root = await async_browse_media(hass, f"{URI_SCHEME}{DOMAIN}")
    folder = next(c for c in root.children if c.title == "Medien")
    assert folder.can_expand
    browsed = await async_browse_media(hass, folder.media_content_id)
    assert [c.title for c in browsed.children] == ["clip.mp4"]  # non-media files hidden
    assert browsed.title == "Medien"

    item = browsed.children[0]
    resolved = await async_resolve_media(hass, item.media_content_id, None)
    assert resolved.mime_type == "video/mp4"

    client = await hass_client()
    full = await client.get(resolved.url)
    assert full.status == 200
    assert await full.read() == video.read_bytes()
    part = await client.get(resolved.url, headers={"Range": "bytes=1000-1099"})
    assert part.status == 206
    assert part.headers["Content-Range"] == f"bytes 1000-1099/{video.stat().st_size}"
    assert await part.read() == video.read_bytes()[1000:1100]
    tail = await client.get(resolved.url, headers={"Range": "bytes=-10"})
    assert await tail.read() == video.read_bytes()[-10:]
    bad = await client.get(resolved.url, headers={"Range": "bytes=999999-"})
    assert bad.status == 416
    missing = await client.get(
        resolved.url.replace(item.identifier.split("/")[2], "nope")
    )
    assert missing.status == 404