"""End-to-end check against a real Filen account.

Usage:
    FILEN_EMAIL=you@example.com FILEN_PASSWORD='...' python poc/poc.py [--2fa 123456]

Logs in, creates a test folder, uploads a probe file, downloads and compares it,
prints the storage usage and deletes everything it created. Nothing is kept.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aiofilen", "src"))

import aiohttp  # noqa: E402

from aiofilen import FilenClient  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--2fa", dest="tfa", default=None, help="current TOTP code")
    args = parser.parse_args()
    email = os.environ.get("FILEN_EMAIL")
    password = os.environ.get("FILEN_PASSWORD")
    if not email or not password:
        print("Set FILEN_EMAIL and FILEN_PASSWORD first.")
        return 2

    async with aiohttp.ClientSession() as session:
        client = FilenClient(session)
        print(f"Logging in as {email} ...")
        credentials = await client.login(email, password, args.tfa)
        print(f"  auth version {credentials.auth_version}, "
              f"{len(credentials.master_keys)} master key(s)")

        user = await client.user_info()
        print(f"Storage: {user.storage_used / 1e9:.2f} / {user.max_storage / 1e9:.2f} GB")

        folder = await client.ensure_folder(client.root_folder_uuid, "ha-filen PoC")
        probe = secrets.token_bytes(3 * 1024 * 1024 + 17)  # spans several chunks
        print("Uploading 3 MiB probe ...")
        file = await client.upload_bytes(probe, folder_uuid=folder.uuid, name="probe.bin")
        print(f"  {file.chunks} chunks, bucket {file.bucket}, region {file.region}")

        print("Downloading and comparing ...")
        got = await client.download_bytes(file)
        if got != probe:
            print("MISMATCH — download differs from upload!")
            return 1
        print("  OK, byte-identical")

        print("Testing a byte range (seeking) ...")
        stream = await client.download(file, start=1024 * 1024 - 5, end=2 * 1024 * 1024 + 5)
        part = b"".join([chunk async for chunk in stream])
        assert part == probe[1024 * 1024 - 5 : 2 * 1024 * 1024 + 6]
        print("  OK")

        print("Cleaning up ...")
        await client.delete_file(file.uuid)
        print("Done. Everything works.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))