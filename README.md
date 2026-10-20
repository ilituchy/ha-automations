# ha-automations

A personal Home Assistant app repository. Each app here is a self-contained
automation that was too stateful, too scheduled, or too safety-critical to
express as a YAML automation.

## Installation

Add this repository under **Settings → Apps → Repositories**:

```
https://github.com/ilituchy/ha-automations
```

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Filituchy%2Fha-automations)

Then install the app you want from the store. Apps are independent: installing
one does not pull in the others.

## Apps

| App | Version | Description |
| --- | --- | --- |
| [NBA Fantasy Autopilot](./nba_autopilot/) | 0.1.0 | Manages one or more ESPN fantasy basketball teams: sets lineups, handles IR, asks before anything irreversible |

Per-app setup lives in that app's `DOCS.md` — for the autopilot, that includes
extracting ESPN session cookies and finding your notification target and user
ID: [`nba_autopilot/DOCS.md`](./nba_autopilot/DOCS.md).

## Layout

Standard HA app-repository shape: `repository.yaml` at the root, one directory
per app, each directory self-contained.

```
.
├── repository.yaml                      # Repository metadata for the HA store
├── tools/                               # Shared dev tooling (not shipped in any image)
│   ├── hactl.py                         # Stdlib-only HA REST + WebSocket client
│   └── verify_approval.py               # End-to-end check of the approval round-trip
└── nba_autopilot/
    ├── config.yaml                      # App manifest (options + schema)
    ├── Dockerfile                       # Pinned base image, manual io.hass.* labels
    ├── requirements.txt                 # Pinned, wheel-only dependencies
    ├── DOCS.md                          # User-facing setup guide
    ├── CHANGELOG.md
    ├── docs/                            # Design docs and ADRs
    ├── tests/
    ├── translations/en.yaml             # Option labels and descriptions
    └── rootfs/
        ├── app/                         # Python application (entrypoint /app/main.py)
        └── etc/s6-overlay/s6-rc.d/      # s6-overlay v3 service definitions
```

### `tools/`

Repository-level, not app-level, because they talk to Home Assistant rather
than to any one app. Both are stdlib-only — `hactl.py` includes a small RFC 6455
WebSocket client, because the Supervisor API is only reachable over WebSocket
with a long-lived token and because verifying an approval tap requires receiving
a live event.

Instance details stay out of the repository: the token is read from
`~/.ha_token` (mode `600`, never from argv — argv is visible in `ps`), and the
base URL comes from `$HA_URL`, then `~/.ha_url`, then `homeassistant.local:8123`.

```sh
tools/hactl.py ping                      # version, user id, admin status
tools/hactl.py states todo               # filter entities by substring
tools/hactl.py ws '{"type":"cloud/status"}'
tools/hactl.py listen mobile_app_notification_action 120
```

## Packaging notes

A few things here intentionally depart from most add-on tutorials, which are now
out of date:

- **The base image is pinned literally** in the `Dockerfile`. Since Supervisor
  **2026.04.0**, `BUILD_FROM` is only passed as a build argument when a build
  file is present, so the widely-copied `ARG BUILD_FROM` / `FROM ${BUILD_FROM}`
  pattern fails with *"empty or invalid base image name"* in a repository like
  this one that has no `build.yaml`.
- **There is no `build.yaml`.** It's deprecated; the Supervisor still reads it
  but logs a warning. Base image, build args and labels live in the `Dockerfile`.
- **`io.hass.*` labels are written by hand**, because the legacy builder used to
  inject `io.hass.type`, `name`, `description` and `url` and no longer does.
  (`io.hass.version`, `io.hass.arch` and `io.hass.type` *are* still passed by
  Supervisor; the hand-written ones are additive and harmless.)
- Multi-arch works through the base image's OCI manifest list (amd64 + arm64)
  plus `docker buildx --platform`; no per-architecture Dockerfiles are needed.
- Local development copies of an app go in the **`local_apps`** folder (mounted
  at `/local_apps`), formerly `addons`.

Current developer documentation lives at
<https://contributors.home-assistant.io/apps/>.

Home Assistant renamed "add-ons" to "apps" in release 2026.2. Slugs, folder
names, image names and the `/addons` API endpoints are unchanged for backwards
compatibility.

## License

MIT — see [LICENSE](./LICENSE).
