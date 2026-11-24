# filen-sdk-native

Python bindings for the official [Filen Rust SDK](https://github.com/FilenCloudDienste/filen-rs),
used by the [ha-filen](https://github.com/grosserknallkopf/ha-filen) Home Assistant
integration.

The crate exposes a small UniFFI surface (`SdkClient`, credentials, drive
records, and streaming source/sink callbacks). Everything else — login, key
derivation, encryption, metadata, upload/download, range reads — is implemented
by `filen-sdk-rs`.

## Build

```bash
python -m pip install maturin
maturin build --release
```

The SDK is pinned to a specific `filen-rs` commit and requires its Rust nightly
toolchain (`nightly-2026-02-20`). Prebuilt wheels are published to PyPI by the
repository's release workflow for the Home Assistant architectures (x86_64,
aarch64, armv7; glibc and musl).

## License

AGPL-3.0-only. The linked `filen-sdk-rs` is AGPL-3.0; see the repository root
`LICENSE` and `NOTICE`.
