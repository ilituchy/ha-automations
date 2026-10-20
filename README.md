# NBA Fantasy Autopilot — Home Assistant app repository

A Home Assistant app that manages an ESPN fantasy basketball team: it sets your
lineup, handles IR slots, and asks before doing anything irreversible.

## Installation

Add this repository under **Settings → Apps → Repositories**:

```
https://github.com/ianlituchy/ha-nba-autopilot
```

Then install **NBA Fantasy Autopilot** from the store.

[![Open your Home Assistant instance and show the add app repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fianlituchy%2Fha-nba-autopilot)

## Apps in this repository

| App | Version | Description |
| --- | --- | --- |
| [NBA Fantasy Autopilot](./nba_autopilot/) | 0.1.0 | Automated ESPN fantasy basketball lineup management |

Full setup instructions, including how to extract your ESPN session cookies and
find your notification target and user ID, are in
[`nba_autopilot/DOCS.md`](./nba_autopilot/DOCS.md).

## Safety defaults

This app ships deliberately conservative:

- **Shadow mode is ON.** Decisions are computed and reported but never sent to
  ESPN until you turn it off. Watch it for a few days first.
- **Add/drop autonomy is OFF.** Lineup and IR changes are reversible, so they're
  automatic; dropping a player is not, so it always asks.
- **Approval taps are authorized** against a single configured Home Assistant
  user ID, so another household member can't approve a move.

## Repository layout

```
.
├── repository.yaml                      # Repository metadata for the HA store
├── nba_autopilot/
│   ├── config.yaml                      # App manifest (options + schema)
│   ├── Dockerfile                       # Pinned base image, manual io.hass.* labels
│   ├── requirements.txt                 # Pinned, wheel-only dependencies
│   ├── DOCS.md                          # User-facing setup guide
│   ├── CHANGELOG.md
│   ├── translations/en.yaml             # Option labels and descriptions
│   └── rootfs/
│       ├── app/                         # Python application (entrypoint /app/main.py)
│       └── etc/s6-overlay/s6-rc.d/      # s6-overlay v3 service definitions
├── docs/
└── tests/
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

MIT
