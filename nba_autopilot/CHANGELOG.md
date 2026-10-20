# Changelog

All notable changes to this app are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-20

Initial release.

### Added

- ESPN fantasy basketball roster evaluation on a configurable daily schedule
  (`run_times`, interpreted in `timezone`).
- Automatic lineup optimization (`autonomy_lineup`) and IR slot management
  (`autonomy_ir`), both on by default because both are reversible.
- Approval workflow for add/drop moves: a companion-app notification with
  action buttons, authorized against `ha_owner_user_id`, plus a task on
  `ha_todo_entity`.
- `shadow_mode`, on by default, which computes and reports every decision
  without sending anything to ESPN.
- `never_drop` safety list and `max_faab_bid` cap (0-1000).
- Ingress status UI on internal port 8099; no host port is published.
- Persistent run history in SQLite at `/data/autopilot.db`.

### Notes

- `autonomy_add_drop` defaults to `false` and is expected to stay that way for
  most users; dropping a player cannot be undone.
- `espn_s2` and `espn_swid` are stored in plaintext by the Supervisor and are
  included in Home Assistant backups. Enable backup encryption. See DOCS.md.
- Built on `ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.06.1` with
  the base image pinned directly in the `Dockerfile`. There is intentionally no
  `build.yaml`: it is deprecated, and since Supervisor 2026.04.0 `BUILD_FROM`
  is only injected when a build file exists.
- No numpy/scipy; the solver is pure Python.

[Unreleased]: https://github.com/ianlituchy/ha-nba-autopilot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ianlituchy/ha-nba-autopilot/releases/tag/v0.1.0
