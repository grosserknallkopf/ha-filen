# Beta testing

This beta uses Filen's official Rust SDK. It reports upload progress from bytes
actually read and caps native chunk uploads at four concurrent requests (the
SDK default was 16), to reduce burst-and-stall behavior on HA hosts and home
routers. It is published as a **pre-release** for real-backup throughput testing.

- Native bridge: `filen-sdk-native==0.1.0b5`
- Integration: `0.2.0b6`, which pins the beta wheel in `manifest.json`

PyPI treats `0.1.0b5` as a pre-release, but an exact pin (`==0.1.0b5`) is
installed by pip without `--pre`. Home Assistant installs it automatically when
the integration starts.

## Publish the beta

1. Create the PyPI project `filen-sdk-native` and configure **Trusted
   Publishing** for:
   - repository: `grosserknallkopf/ha-filen`
   - workflow: `release-native.yaml`
   - environment: `pypi`
2. In the GitHub repository, create an environment named `pypi`.
3. Push the native tag first:

   ```bash
   git tag native-v0.1.0b5
   git push origin main native-v0.1.0b5
   ```

   Wait for the workflow to build and verify all six glibc/musl wheels for
   x86_64, aarch64 and armv7, and confirm the PyPI release exists. Only then
   publish the integration tag:

   ```bash
   git tag v0.2.0b6
   git push origin v0.2.0b6
   ```

   `v0.2.0b6` is the sole GitHub beta release that HACS/Marketplace installs.
4. Install/update the integration from HACS or the Marketplace, then restart
   Home Assistant. On startup Home Assistant installs
   `filen-sdk-native==0.1.0b5` from PyPI.

If the Marketplace/HACS only offers stable integration releases, install the
`v0.2.0` tag explicitly. The native wheel itself is a pre-release, but the
integration version is a normal release.

## Test without PyPI

The release workflow also stores the built wheels as GitHub Actions artifacts.
For a quick local test on a Debian/glibc HA Container:

```bash
pip install filen_sdk_native-0.1.0b5-*.whl
```

On Home Assistant OS (Alpine/musl), use the `*-musllinux_*.whl` matching the
architecture. A manual `pip install` inside the HA container is not persistent
across Home Assistant OS updates; the PyPI route is the supported one.

## What to test

- Setup with e-mail/password and with 2FA; reauthentication after revoking the
  API key.
- Upload a multi-gigabyte backup; confirm progress rises continuously and compare
  elapsed time to b5. Then list, download, and restore it.
- Backup retention/deletion.
- Storage sensors after a refresh.
- `filen.upload` and `filen.delete` actions.
- Media browser: browse folders, play audio/video, seek in a video.
- Restart Home Assistant and confirm the stored credentials still work.

Please include the Home Assistant version, architecture (`uname -m`), libc
(HA OS vs. HA Container), and the relevant log lines in a bug report.

## Promote to stable

1. Set `native/Cargo.toml` and `native/pyproject.toml` to `0.1.0`.
2. Set `custom_components/filen/manifest.json` and `aiofilen/pyproject.toml` to
   `filen-sdk-native==0.1.0`.
3. Tag `native-v0.1.0` and the integration release, and push.
