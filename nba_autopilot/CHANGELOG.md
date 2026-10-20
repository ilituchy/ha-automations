# Changelog

All notable changes to this app are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Multiple leagues.** The `leagues` option takes one entry per team you
  manage (`league_id`, `team_id`, optional `label`). Each league is optimized
  independently — roster, player values and games-played budget are
  league-local — and only the ESPN cookie and the notification channel are
  shared. A league that errors is skipped for that run; the others continue.
  Notifications name their league.
- `runs.league_id` and `journal.league_id` columns, so history and the write
  journal can be read per league. Added by an additive `ALTER TABLE` migration
  that is safe to replay, since `CREATE TABLE IF NOT EXISTS` cannot widen a
  database that already exists.
- **Points-league scoring.** A player-game is now valued as the dot product of
  his projected line against the league's `scoringItems`. The categories model
  is unchanged and still present; the league type is read from ESPN's own
  `scoringType` rather than configured. See ADR-010.
- Per-league `season` in the `leagues` option, and a validity check on it. A
  wrong season is the one setting that fails *silently* — ESPN serves a
  finished season happily, with final standings — so the season in use for each
  league is now reported in the run record. Leave it at `0` to derive it from
  the date. Note that ESPN labels a season by the year it ends in: 2026-27 is
  `2027`.
- `tools/espn_probe.py`, read-only league reconnaissance. It builds the read
  client, so it physically cannot write, and it takes cookies from a `0600`
  file rather than from `argv`.

### Changed

- **The games-played cap is modelled as one pooled weekly budget** rather than
  a per-slot season-long one, which is what two live leagues actually report:
  a single entry whose value scales with starter count (40 and 44 per matchup
  period against 10 and 11 starting slots). One shadow price instead of one per
  slot. The cap binds harder than the old reading implied — a deep-bench league
  has roughly twice as many startable player-games in a week as it has budget —
  so `utilization_ratio` is surfaced to explain deliberate benching. See
  ADR-011.
- Under a binding cap, the start/bench threshold now compares the value
  **conditional on the player playing** rather than the availability-weighted
  value. The cap counts games *played*, so availability cancels out of the
  comparison: a questionable star and a healthy role player with the same
  expected line are not the same bet, and the star is the better one.
- The app now lives in the [`ha-automations`][repo] repository alongside other
  Home Assistant automations, at `nba_autopilot/`. The store entry, slug and
  `/data` contents are unchanged; re-point the repository URL in
  **Settings → Apps → Repositories** if you added the old one.

### Removed

- `espn_league_id` and `espn_team_id`, replaced by `leagues`. They survive only
  as a development escape hatch via environment variables.

### Fixed

- **Four league-settings fields were parsed wrongly**, all found by the first
  read of a real league:
  - `lineupSlotStatLimits` is `{slot: {statId, limitValue}}`, not
    `{slot: int}`. Reading it as an int raised `TypeError`.
  - the waiver mode was read from `waiverProcessDays`, which is a *list* of
    weekday names, so both leagues reported their mode as `['SUNDAY']`.
  - the FAAB budget was taken from `acquisitionBudget` without checking
    `isUsingAcquisitionBudget`. Both leagues send a budget of 100 while running
    traditional waiver priority, so the app would have tried to bid FAAB in a
    league that has none.
  - the roster lock mode was read from `lineupLockTimeType`; ESPN spells it
    `lineupLocktimeType`, so every league reported `UNKNOWN`. Both leagues are
    `INDIVIDUAL_GAME` — the strictest mode, where each player locks at his own
    tip-off rather than at the day's first game.
- Named the last two unmapped scoring stats (quadruple-double and team win), so
  no live scoring item is unknown. A league that scores on a *non-additive*
  stat — a percentage or a per-game average — is now refused outright instead
  of being valued by a sum that does not apply to it.
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
