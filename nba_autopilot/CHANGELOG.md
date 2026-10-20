# Changelog

All notable changes to this app are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Multiple leagues.** The `leagues` option takes one entry per team you
  manage (`league_id`, `team_id`, optional `label`). Each league is optimized
  independently — roster, category weights and games-played budget are
  league-local — and only the ESPN cookie and the notification channel are
  shared. A league that errors is skipped for that run; the others continue.
  Notifications name their league.
- `runs.league_id` and `journal.league_id` columns, so history and the write
  journal can be read per league. Added by an additive `ALTER TABLE` migration
  that is safe to replay, since `CREATE TABLE IF NOT EXISTS` cannot widen a
  database that already exists.

### Changed

- The app now lives in the [`ha-automations`][repo] repository alongside other
  Home Assistant automations, at `nba_autopilot/`. The store entry, slug and
  `/data` contents are unchanged; re-point the repository URL in
  **Settings → Apps → Repositories** if you added the old one.

### Removed

- `espn_league_id` and `espn_team_id`, replaced by `leagues`. They survive only
  as a development escape hatch via environment variables.

### Fixed

- **Approval taps were recorded as rejections and matched no proposal.** The
  tapped action id (`<run>:<kind>:<seq>:<VERB>`) was passed verbatim to the
  database lookup, which stores `<run>:<kind>:<seq>`, so it never matched; and
  the verb was read off the wrong field, so an **Approve** tap evaluated as not
  an approval. Action-id construction and parsing now live in one place with
  round-trip tests. Nothing irreversible could have been triggered by this — it
  failed closed — but no approval could have been granted either.
- `check_external_url` reported success whenever the `cloud` component was
  loaded, which proves nothing: on a live Nabu Casa instance `external_url` is
  `None` and `cloud` is present whether or not the remote connection is up or
  the subscription is current. It now queries `cloud/status` and checks
  `logged_in`, `active_subscription` and `remote_connected`, because the
  difference between those states is the difference between "approvals work"
  and "approvals silently do nothing".
- Notification action events are now shape-checked before parsing, and action
  ids belonging to other integrations are ignored at `debug` instead of
  being warned about — every notification action on the instance arrives on the
  same event bus.

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

[repo]: https://github.com/ilituchy/ha-automations
[Unreleased]: https://github.com/ilituchy/ha-automations/compare/nba_autopilot-v0.1.0...HEAD
[0.1.0]: https://github.com/ilituchy/ha-automations/releases/tag/nba_autopilot-v0.1.0
