# aiofilen

Async Python facade around the official Filen Rust SDK, built for the
[ha-filen](../README.md) Home Assistant integration.

- Login with auth version 2 (PBKDF2) and 3 (Argon2id + DEK), two-factor
  authentication, and restoration from stored credentials
- Client-side encryption, name hashing and metadata handling by
  [filen-sdk-rs](https://github.com/FilenCloudDienste/filen-rs), so files stay
  compatible with the Filen web and desktop apps
- Streaming upload and download with flat memory use (1 MiB chunks), byte
  ranges for seeking, and the SDK's integrity check on full downloads
- Folder traversal by path, trash and permanent delete

The native bridge is the `filen-sdk-native` package in [`../native`](../native).
Both are AGPL-3.0-only because `filen-sdk-rs` is AGPL-3.0.

```python
from aiofilen import FilenClient

client = FilenClient()
credentials = await client.login("you@example.com", "password", tfa_code="123456")
# persist credentials.as_dict() ...

folder = await client.ensure_folder(client.root_folder_uuid, "Backups")
file = await client.upload_bytes(b"hello", folder_uuid=folder.uuid, name="hello.txt")
assert await client.download_bytes(file) == b"hello"
```

Community project, not an official Filen product.
