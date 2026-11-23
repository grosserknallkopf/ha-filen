<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/filen/brand/dark_logo.png">
  <img src="custom_components/filen/brand/logo.png" alt="Filen" height="64">
</picture>

# Filen for Home Assistant

Store your Home Assistant backups end-to-end encrypted in [Filen](https://filen.io).

**Deutsch:** Mit dieser Integration landen deine Home-Assistant-Backups direkt und Ende-zu-Ende-verschlüsselt in Filen, ohne Umweg über das Rclone-Add-on. Filen wird dazu einfach als zusätzlicher Speicherort in den Backup-Einstellungen von Home Assistant ausgewählt.

Built in the style of [ha-internxt](https://github.com/TheRealSimon42/ha-internxt), with the protocol implemented from Filen's API documentation and cross-checked against the official [filen-sdk-ts](https://github.com/FilenCloudDienste/filen-sdk-ts) and [filen-sdk-go](https://github.com/FilenCloudDienste/filen-sdk-go) SDKs. Works with the free Filen plan; no rclone, no Node.js, no add-on needed.

## Features

- Filen as a backup location in **Settings → System → Backups** (Home Assistant 2026.4 or newer)
- Streaming upload and download with flat memory use (1 MiB chunks), also for multi-GB backups on a Raspberry Pi
- Client-side AES-256-GCM encryption in Filen's own formats: backups can also be downloaded in the Filen web or desktop app
- Login with two-factor authentication; the password is used once and never stored
- Supports both Filen auth versions: 2 (PBKDF2) and 3 (Argon2id + DEK)
- Integrity check (SHA-512 + GCM auth tags) on every download, free-space check before every upload
- Old backups are deleted permanently (not to the trash), so retention frees space
- Diagnostics with e-mail and all keys redacted; English and German
- **Actions** `filen.upload` and `filen.delete`: upload files from Home Assistant (for example camera snapshots from an automation) and delete them again, to the Filen trash by default
- **Storage sensors**: used, free and total storage plus usage in percent
- **Media source**: browse your Filen folders in the media browser and play images, videos and audio, with seeking

### Example: send a snapshot to Filen when the doorbell rings

```yaml
actions:
  - action: camera.snapshot
    target:
      entity_id: camera.front_door
    data:
      filename: /media/snapshots/front_door.jpg
  - action: filen.upload
    data:
      config_entry_id: YOUR_FILEN_ENTRY_ID  # pick the account in the action editor
      filename: /media/snapshots/front_door.jpg
      destination_folder: Home Assistant Uploads/Kameras
      overwrite: true
```

Files must be in a folder Home Assistant may read, like `/media` or `/config/www`, or one listed under `allowlist_external_dirs`.

## Installation

[![Open this repository in your Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=grosserknallkopf&repository=ha-filen&category=integration)

**Marketplace (Home Assistant 2026.11 and newer):** click the button above, or add `https://github.com/grosserknallkopf/ha-filen` as a repository from a link in **Settings → Marketplace**, install **Filen**, restart Home Assistant.

**HACS (Home Assistant before 2026.11):** HACS → ⋮ → *Custom repositories* → add `https://github.com/grosserknallkopf/ha-filen` as type *Integration*, install **Filen**, restart Home Assistant.

**Manual:** copy `custom_components/filen` into `config/custom_components/` and restart Home Assistant.

## Setup

[![Add the Filen integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=filen)

1. **Settings → Devices & services → Add integration → Filen**, log in with e-mail and password (plus 2FA code if enabled). Setup creates the folder *Home Assistant Backups* and runs a short connection test (upload, download and delete a small file).
2. **Settings → System → Backups → Backup settings**: select Filen as a location.

## Good to know

- **Two layers of encryption.** Filen encrypts on your device before upload; Home Assistant additionally encrypts the backup with your backup key. Keep both your Filen password and your Home Assistant backup key safe.
- **Restore without this integration.** Download the `.tar` in the Filen web or desktop app and upload it during Home Assistant onboarding.
- **Your API key and master keys are stored in Home Assistant** (like every cloud login). Keep Home Assistant backups encrypted and your instance secured.
- **Failed logins are rate limited by Filen.** The integration never retries a rejected password on its own.
- File names are visible to Filen, file contents are not.
- Filen's API key is long-lived; there is no token refresh. If it is revoked, the integration asks you to log in again.

## Repository layout

| Path | Content |
| --- | --- |
| `custom_components/filen/` | The Home Assistant integration (config flow with 2FA and reauth, backup agent, sensors, actions, media source, diagnostics, de/en) |
| `custom_components/filen/brand/` | Icons and logos for the Home Assistant UI and Marketplace, from the official Filen press kit |
| `aiofilen/` | Async Python client for Filen: login (auth v2/v3), AES-256-GCM, streaming up- and download with byte ranges |
| `aiofilen/tests/` | Library tests against a fake Filen gateway (`pytest`) |
| `tests/` | Integration tests (`pytest-homeassistant-custom-component`) |
| `scripts/sync_lib.sh` | Copies `aiofilen` into the integration |

Run the tests with:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ./aiofilen pytest pytest-asyncio pytest-homeassistant-custom-component
pytest                  # Home Assistant integration tests
(cd aiofilen && pytest) # library tests against a fake Filen gateway
```

## License

MIT (see [NOTICE](NOTICE) for third-party code). This is a community project, not an official Filen product.