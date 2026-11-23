"""Client tests against a fake Filen gateway (aiohttp test server).

The fake server speaks the same envelope protocol and uses the same crypto
module, so these tests exercise the full login/upload/download plumbing.
"""

from __future__ import annotations

import hashlib
import json
import uuid as uuidlib

import aiohttp
from aiohttp import web
import pytest

from aiofilen import Credentials, FilenClient, MIB, crypto
from aiofilen.exceptions import (
    FilenAuthError,
    FilenError,
    FilenIntegrityError,
    FilenTwoFactorRequired,
)

EMAIL = "user@example.com"
PASSWORD = "correct horse battery staple"
TFA_CODE = "123456"
API_KEY = "test-api-key"


class FakeFilen:
    """Minimal in-memory Filen: one user, folders, chunked files."""

    def __init__(self, auth_version: int = 2, two_factor: bool = False) -> None:
        self.auth_version = auth_version
        self.two_factor = two_factor
        self.salt = crypto.random_hex(32) if auth_version == 3 else "test-salt"
        self.master_key, self.derived_password = crypto.derive_login_keys(
            PASSWORD, auth_version, self.salt
        )
        self.master_keys = [self.master_key]
        self.dek_encrypted: str | None = None
        self.keypair: dict | None = None
        self.base_folder = str(uuidlib.uuid4())
        self.folders: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.max_storage = 10 * 1024**3

    @property
    def meta_version(self) -> int:
        return 3 if self.auth_version == 3 else 2

    @property
    def keys(self) -> list[str]:
        if self.auth_version == 3:
            return [crypto.decrypt_metadata(self.dek_encrypted, [self.master_key])]
        return self.master_keys

    def storage_used(self) -> int:
        return sum(f["size"] for f in self.files.values())

    def encrypt(self, text: str) -> str:
        return crypto.encrypt_metadata(text, self.keys[-1], self.meta_version)

    def decrypt(self, text: str) -> str:
        return crypto.decrypt_metadata(text, self.keys)


def ok(data=None) -> web.Response:
    return web.json_response({"status": True, "message": "", "code": "", "data": data})


def err(code: str, message: str) -> web.Response:
    return web.json_response({"status": False, "message": message, "code": code})


def build_app(state: FakeFilen) -> web.Application:
    # encrypted chunks are 1 MiB + nonce + tag, above aiohttp's 1 MiB default
    app = web.Application(client_max_size=2 * MIB)

    def authorized(request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {API_KEY}"

    async def auth_info(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("email") != EMAIL:
            return err("NOT_FOUND", "User not found")
        return ok({"email": EMAIL, "authVersion": state.auth_version, "salt": state.salt, "id": 42})

    async def login(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("email") != EMAIL or body.get("password") != state.derived_password:
            return err("INVALID_CREDENTIALS", "Invalid email or password")
        if state.two_factor and body.get("twoFactorCode") != TFA_CODE:
            return err("INVALID_2FA", "Invalid 2FA code")
        return ok({"apiKey": API_KEY})

    async def user_info(request: web.Request) -> web.Response:
        return ok({
            "id": 42, "email": EMAIL, "isPremium": 1,
            "maxStorage": state.max_storage, "storageUsed": state.storage_used(),
            "avatarURL": "", "baseFolderUUID": state.base_folder,
        })

    async def base_folder(request: web.Request) -> web.Response:
        return ok({"uuid": state.base_folder})

    async def master_keys(request: web.Request) -> web.Response:
        body = await request.json()
        # The client proves it holds the current key; answer with the full list.
        crypto.decrypt_metadata(body["masterKeys"], state.master_keys)
        return ok({"keys": state.encrypt("|".join(state.master_keys))})

    async def get_dek(request: web.Request) -> web.Response:
        return ok({"dek": state.dek_encrypted})

    async def set_dek(request: web.Request) -> web.Response:
        body = await request.json()
        state.dek_encrypted = body["dek"]
        return ok()

    async def keypair_info(request: web.Request) -> web.Response:
        if state.keypair is None:
            return ok({"publicKey": None, "privateKey": None})
        return ok(state.keypair)

    async def keypair_set(request: web.Request) -> web.Response:
        body = await request.json()
        state.keypair = {"publicKey": body["publicKey"], "privateKey": body["privateKey"]}
        return ok()

    async def dir_content(request: web.Request) -> web.Response:
        body = await request.json()
        parent = body["uuid"]
        uploads = [
            {
                "uuid": f["uuid"], "metadata": f["metadata"], "rm": f["rm"],
                "timestamp": 1, "chunks": f["chunks"], "size": f["size"],
                "bucket": f["bucket"], "region": f["region"], "parent": f["parent"],
                "version": f["version"], "favorited": 0,
            }
            for f in state.files.values() if f["parent"] == parent and not f["trash"]
        ]
        folders = [
            {"uuid": f["uuid"], "name": f["name"], "parent": f["parent"],
             "color": "default", "timestamp": 1, "favorited": 0, "is_sync": 0, "is_default": 0}
            for f in state.folders.values() if f["parent"] == parent
        ]
        return ok({"uploads": uploads, "folders": folders})

    async def dir_create(request: web.Request) -> web.Response:
        body = await request.json()
        state.folders[body["uuid"]] = {
            "uuid": body["uuid"], "name": body["name"],
            "nameHashed": body["nameHashed"], "parent": body["parent"],
        }
        return ok({"uuid": body["uuid"]})

    async def dir_exists(request: web.Request) -> web.Response:
        body = await request.json()
        for folder in state.folders.values():
            if folder["parent"] == body["parent"] and folder["nameHashed"] == body["nameHashed"]:
                return ok({"exists": True, "uuid": folder["uuid"]})
        return ok({"exists": False})

    async def file_get(request: web.Request) -> web.Response:
        body = await request.json()
        file = state.files.get(body["uuid"])
        if file is None or file["trash"]:
            return err("NOT_FOUND", "File not found")
        return ok({
            "uuid": file["uuid"], "region": file["region"], "bucket": file["bucket"],
            "nameEncrypted": file["name"], "nameHashed": file["nameHashed"],
            "sizeEncrypted": file["size_enc"], "mimeEncrypted": file["mime"],
            "metadata": file["metadata"], "size": file["size"], "parent": file["parent"],
            "versioned": False, "trash": False, "version": file["version"],
        })

    async def file_trash(request: web.Request) -> web.Response:
        body = await request.json()
        state.files[body["uuid"]]["trash"] = True
        return ok()

    async def file_delete(request: web.Request) -> web.Response:
        body = await request.json()
        del state.files[body["uuid"]]
        return ok()

    async def upload_chunk(request: web.Request) -> web.Response:
        query = request.query
        data = await request.read()
        if hashlib.sha512(data).hexdigest() != query.get("hash"):
            return err("HASH_MISMATCH", "Chunk hash does not match")
        file_uuid = query["uuid"]
        pending = state.files.setdefault(file_uuid, {
            "uuid": file_uuid, "chunk_data": {}, "trash": False,
            "parent": query["parent"], "bucket": "test-bucket", "region": "test-region",
        })
        pending["chunk_data"][int(query["index"])] = data
        return ok({"bucket": "test-bucket", "region": "test-region"})

    async def upload_done(request: web.Request) -> web.Response:
        body = await request.json()
        pending = state.files.get(body["uuid"])
        if pending is None or len(pending.get("chunk_data", {})) != body["chunks"]:
            return err("MISSING_CHUNKS", "Not all chunks were uploaded")
        metadata = json.loads(state.decrypt(body["metadata"]))
        if metadata["hash"] != _plaintext_hash(pending["chunk_data"], metadata["key"], body["version"]):
            return err("HASH_MISMATCH", "File hash does not match")
        pending.update({
            "name": body["name"], "nameHashed": body["nameHashed"],
            "size_enc": body["size"], "mime": body["mime"], "metadata": body["metadata"],
            "rm": body["rm"], "version": body["version"], "size": metadata["size"],
            "chunks": body["chunks"],
        })
        return ok({"chunks": body["chunks"], "size": metadata["size"]})

    async def upload_empty(request: web.Request) -> web.Response:
        body = await request.json()
        metadata = json.loads(state.decrypt(body["metadata"]))
        state.files[body["uuid"]] = {
            "uuid": body["uuid"], "name": body["name"], "nameHashed": body["nameHashed"],
            "size_enc": body["size"], "mime": body["mime"], "metadata": body["metadata"],
            "rm": "", "version": body["version"], "size": 0, "chunks": 0,
            "chunk_data": {}, "parent": body["parent"],
            "bucket": "test-bucket", "region": "test-region", "trash": False,
        }
        return ok({"chunks": 0, "size": 0})

    async def download_chunk(request: web.Request) -> web.Response:
        file = state.files.get(request.match_info["uuid"])
        if file is None:
            return web.Response(status=404)
        data = file["chunk_data"].get(int(request.match_info["index"]))
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data)

    def guard(handler):
        async def wrapped(request: web.Request) -> web.Response:
            if not authorized(request):
                return web.json_response(
                    {"status": False, "message": "Invalid API key", "code": "UNAUTHORIZED"},
                    status=401,
                )
            return await handler(request)
        return wrapped

    routes = [
        ("POST", "/v3/auth/info", auth_info),
        ("POST", "/v3/login", login),
        ("GET", "/v3/user/info", user_info),
        ("GET", "/v3/user/baseFolder", base_folder),
        ("POST", "/v3/user/masterKeys", master_keys),
        ("GET", "/v3/user/dek", get_dek),
        ("POST", "/v3/user/dek", set_dek),
        ("GET", "/v3/user/keyPair/info", keypair_info),
        ("POST", "/v3/user/keyPair/set", keypair_set),
        ("POST", "/v3/dir/content", dir_content),
        ("POST", "/v3/dir/create", dir_create),
        ("POST", "/v3/dir/exists", dir_exists),
        ("POST", "/v3/file", file_get),
        ("POST", "/v3/file/trash", file_trash),
        ("POST", "/v3/file/delete/permanent", file_delete),
        ("POST", "/v3/upload/done", upload_done),
        ("POST", "/v3/upload/empty", upload_empty),
    ]
    for method, path, handler in routes:
        if path in ("/v3/auth/info", "/v3/login"):
            app.router.add_route(method, path, handler)  # before there is an API key
        else:
            app.router.add_route(method, path, guard(handler))
    app.router.add_post("/v3/upload", guard(upload_chunk))
    app.router.add_get("/{region}/{bucket}/{uuid}/{index}", guard(download_chunk))
    return app


def _plaintext_hash(chunks: dict[int, bytes], key: str, version: int) -> str:
    sha = hashlib.sha512()
    for index in sorted(chunks):
        sha.update(crypto.decrypt_chunk(chunks[index], key, version))
    return sha.hexdigest()


@pytest.fixture
async def server():
    state = FakeFilen()
    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield state, f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.fixture
async def client(server):
    state, base = server
    async with aiohttp.ClientSession() as session:
        yield FilenClient(
            session, gateway_url=base, ingest_urls=(base,), egest_urls=(base,)
        ), state


async def _login(client: FilenClient, state: FakeFilen) -> Credentials:
    code = TFA_CODE if state.two_factor else None
    return await client.login(EMAIL, PASSWORD, code)


@pytest.mark.parametrize("auth_version", [2, 3])
async def test_login_and_roundtrip(auth_version):
    state = FakeFilen(auth_version=auth_version)
    app = build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with aiohttp.ClientSession() as session:
            client = FilenClient(session, gateway_url=base, ingest_urls=(base,), egest_urls=(base,))
            credentials = await client.login(EMAIL, PASSWORD)
            assert credentials.auth_version == auth_version
            assert credentials.base_folder_uuid == state.base_folder
            assert credentials.private_key
            assert client.user.user_id == 42

            folder = await client.ensure_folder(client.root_folder_uuid, "Backups")
            data = b"backup bytes" * 1000
            file = await client.upload_bytes(data, folder_uuid=folder.uuid, name="b.tar")
            assert await client.download_bytes(file) == data
            assert (await client.find_file(folder.uuid, "b.tar")).uuid == file.uuid

            # restore from persisted credentials, without a new login
            client2 = FilenClient(session, gateway_url=base, ingest_urls=(base,), egest_urls=(base,))
            client2.set_credentials(Credentials.from_dict(credentials.as_dict()))
            assert await client2.download_bytes(file) == data
    finally:
        await runner.cleanup()


async def test_login_wrong_password(client):
    filen, _ = client
    with pytest.raises(FilenAuthError):
        await filen.login(EMAIL, "wrong password")


async def test_login_unknown_email(client):
    filen, _ = client
    with pytest.raises(FilenAuthError):
        await filen.login("nobody@example.com", PASSWORD)


async def test_login_two_factor(server):
    state, base = server
    state.two_factor = True
    async with aiohttp.ClientSession() as session:
        filen = FilenClient(session, gateway_url=base, ingest_urls=(base,), egest_urls=(base,))
        with pytest.raises(FilenTwoFactorRequired):
            await filen.login(EMAIL, PASSWORD)
        credentials = await filen.login(EMAIL, PASSWORD, TFA_CODE)
        assert credentials.api_key == API_KEY


async def test_multi_chunk_upload_download(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    data = bytes(range(256)) * (MIB // 256) * 2 + b"tail"  # 2 MiB + 4 bytes
    assert len(data) == 2 * MIB + 4
    progress: list[int] = []
    file = await filen.upload_bytes(data, folder_uuid=folder.uuid, name="big.tar")
    assert file.chunks == 3
    stream = await filen.download(file)
    got = b"".join([chunk async for chunk in stream])
    assert got == data


async def test_download_range(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    data = bytes(range(256)) * (MIB // 256) * 2 + b"tail"
    file = await filen.upload_bytes(data, folder_uuid=folder.uuid, name="big.bin")
    for start, end in [(0, 0), (5, 100), (MIB - 5, MIB + 5), (MIB, 2 * MIB - 1), (2 * MIB, len(data) - 1), (len(data) - 3, len(data) - 1)]:
        stream = await filen.download(file, start=start, end=end)
        got = b"".join([chunk async for chunk in stream])
        assert got == data[start : end + 1], (start, end)


async def test_download_integrity_failure(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    file = await filen.upload_bytes(b"some data", folder_uuid=folder.uuid, name="x.txt")
    stored = state.files[file.uuid]["chunk_data"][0]
    state.files[file.uuid]["chunk_data"][0] = stored[:-1] + bytes([stored[-1] ^ 0xFF])
    with pytest.raises(FilenIntegrityError):
        await filen.download_bytes(file)


async def test_empty_file(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    file = await filen.upload_bytes(b"", folder_uuid=folder.uuid, name="empty.txt")
    assert file.size == 0
    assert await filen.download_bytes(file) == b""


async def test_upload_stream_length_mismatch(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")

    async def short():
        yield b"only a little"

    with pytest.raises(FilenError):
        await filen.upload(short(), size=MIB, folder_uuid=folder.uuid, name="short.bin")


async def test_folder_paths(client):
    filen, state = client
    await _login(filen, state)
    root = filen.root_folder_uuid
    uuid = await filen.ensure_folder_path(root, "Home Assistant Uploads/Kameras")
    assert await filen.ensure_folder_path(root, "Home Assistant Uploads/Kameras") == uuid
    assert await filen.find_folder_path(root, "Home Assistant Uploads/Kameras") == uuid
    assert await filen.find_folder_path(root, "Home Assistant Uploads/Nein") is None
    folders = await filen.list_folders(uuid)
    assert folders == []


async def test_ensure_folder_is_idempotent(client):
    filen, state = client
    await _login(filen, state)
    first = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    second = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    assert first.uuid == second.uuid


async def test_trash_and_delete(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    file = await filen.upload_bytes(b"x", folder_uuid=folder.uuid, name="x.txt")
    await filen.trash_file(file.uuid)
    assert await filen.find_file(folder.uuid, "x.txt") is None
    file2 = await filen.upload_bytes(b"y", folder_uuid=folder.uuid, name="y.txt")
    await filen.delete_file(file2.uuid)
    assert await filen.find_file(folder.uuid, "y.txt") is None
    assert file2.uuid not in state.files


async def test_file_meta(client):
    filen, state = client
    await _login(filen, state)
    folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
    data = b"z" * (MIB + 10)
    file = await filen.upload_bytes(data, folder_uuid=folder.uuid, name="z.bin")
    meta = await filen.file_meta(file.uuid)
    assert meta.name == "z.bin"
    assert meta.size == len(data)
    assert meta.chunks == 2
    assert await filen.download_bytes(meta) == data


async def test_master_keys_after_password_change(server):
    state, base = server
    state.master_keys.insert(0, "an-older-master-key-from-a-password-change")
    async with aiohttp.ClientSession() as session:
        filen = FilenClient(session, gateway_url=base, ingest_urls=(base,), egest_urls=(base,))
        credentials = await filen.login(EMAIL, PASSWORD)
        assert "an-older-master-key-from-a-password-change" in credentials.master_keys
        folder = await filen.ensure_folder(filen.root_folder_uuid, "Backups")
        file = await filen.upload_bytes(b"ok", folder_uuid=folder.uuid, name="ok.txt")
        assert await filen.download_bytes(file) == b"ok"