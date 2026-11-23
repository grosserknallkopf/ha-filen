# aiofilen

Async Python client for [Filen](https://filen.io), built for the
[ha-filen](../README.md) Home Assistant integration.

- Login with auth version 2 (PBKDF2) and 3 (Argon2id + DEK), two-factor authentication
- Client-side AES-256-GCM encryption in Filen's own formats (`002`/`003` metadata,
  chunked file data), so files are readable in the Filen web and desktop apps
- Streaming upload and download with flat memory use (1 MiB chunks), byte ranges
  for seeking, SHA-512 integrity check on full downloads
- Folder traversal by path, trash and permanent delete

The protocol is ported from the official
[filen-sdk-ts](https://github.com/FilenCloudDienste/filen-sdk-ts) and
[filen-sdk-go](https://github.com/FilenCloudDienste/filen-sdk-go) (both MIT).

```python
import aiohttp
from aiofilen import FilenClient

async with aiohttp.ClientSession() as session:
    client = FilenClient(session)
    credentials = await client.login("you@example.com", "password", tfa_code="123456")
    # persist credentials.as_dict() ...
    folder = await client.ensure_folder(client.root_folder_uuid, "Backups")
    file = await client.upload_bytes(b"hello", folder_uuid=folder.uuid, name="hello.txt")
    assert await client.download_bytes(file) == b"hello"
```

MIT license. Community project, not an official Filen product.