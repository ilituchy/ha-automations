# Changelog

All notable changes to this app are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The solver runs.** `run_league()` is wired end to end: projections →
  per-player value → games-played shadow price → assignment → proposal →
  journal → notification. Previously it read the league and stopped, recording
  `"read-only run: solver pipeline not yet wired"`. `shadow_mode` is still the
  default and is enforced inside `EspnWrite`, so a run computes and reports a
  lineup without sending anything to ESPN. Verified against both live leagues.
- `pipeline.py` — assembles the day's decision as a max-weight matching over
  every eligible (player, slot) pair. Emits a `LineupPlan` whose `target` is
  **total** (one slot per rostered player), plus the moves, `λ`, the
  utilization ratio and the notes explaining them. See ADR-014.
- `solve/calendar.py` — Monday–Sunday matchup periods derived from the NBA
  calendar, with `verify()` cross-checking the model against ESPN's own
  `currentMatchupPeriod` on every run. See ADR-012.
- `usage.py` and the `gp_snapshots` table — games used this matchup period,
  reconstructed as a difference of season-to-date readings against a baseline
  captured on the week's first day, cross-checked against `statBySlot` where
  ESPN populates it. See ADR-013.
- The run record now reports the matchup-period window and game-day count, the
  games-used reading and its provenance, `λ`, the utilization ratio, and any
  rostered player ESPN gives **no NBA team** (`proTeamId 0`) — who is
  unstartable all week and invisible to the cap, which is correct but worth
  saying out loud. Two players were in that state on the first live run.
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

- **The app refused to start**, because the s6 run script still required the
  `espn_league_id` and `espn_team_id` options that `leagues` replaced — so a
  correctly configured app exited 1 with *"Required option 'espn_league_id' is
  not set"*, naming a setting that no longer exists in the Configuration tab.
  Found on the first real install. The guard now covers only the scalar
  options; `leagues` is checked by `_parse_leagues`, which can say *which*
  entry is wrong, and which `bashio::config.has_value` could not have checked
  anyway — it sees an empty list as the string `"[]"` and passes it.
- **The add/drop and cancel write paths were all three wrong**, and are now
  verified end to end against a throwaway ESPN league rather than against the
  documented grammar. Every item below was found by a real request on the first
  attempt to use it.
  - **The transaction `type` was chosen from the waiver flag**, so a drop was
    sent as `FREEAGENT` and refused with `TRAN_INVALID_ITEM_TYPE_COUNT`. ESPN
    gates the permitted `items` against `type`: a lone `DROP` needs `ROSTER`,
    and `ROSTER` in turn cannot carry an `ADD`. This would have broken *every*
    add/drop the app made. See ADR-015.
  - **`cancel()` addressed the claim by `id`**, which ESPN discards and
    replaces — the target belongs in `relatedTransactionId`, because a cancel
    is a *new* transaction rather than a mutation of the old one. The 409 read
    `"Transaction with ID null could not be found"`, which was ESPN describing
    its own null field, not ours. `type` and `scoringPeriodId` are mandatory
    too and are now read off the live claim; `cancel()` also looks the claim up
    first, so cancelling something that is not pending no longer reaches the
    network.
  - **An instant add of a player on waivers** is now blocked in preflight with
    a message naming the fix, instead of being sent and rejected.
- **A dropped player stays in the roster read**, keeping his old
  `lineupSlotId`, and `roster()` returned him. `len(entries)` is not a roster
  count. Left unfixed this would have assigned a lineup slot to someone no
  longer on the team — which ESPN rejects, losing the whole day's lineup — and
  made the exact-capacity matching infeasible for a reason nothing logged would
  explain. Exclusion now requires positive evidence of departure, and is logged
  per player. See ADR-016.
- **`free_agents()` could not distinguish a free agent from a player on
  waivers**, because `RosterEntry` had nowhere to put it. This is per-player,
  not per-league: one live league had all 300 available players on waivers
  while the other had 299 free agents and one on waivers. `acquisition_status`
  and `must_claim_on_waivers` now carry it. See ADR-017.
- **`scheduleSettings.matchupPeriods` was being trusted.** Both live leagues
  send a self-referential map — all 22 entries map period *n* to `[n]` — which
  read literally says every matchup period is one day long. That would have
  priced the games-played cap over a one-day horizon and spent the whole weekly
  budget by Tuesday, with a plausible-looking shadow price the whole time. The
  degenerate shape is now detected at the parse boundary, logged once, and
  discarded in favour of the derived calendar. See ADR-012.
- Dates are now produced in exactly one place, in one timezone.
  `scoring_period_map()` derived them in UTC — which puts a scoring period on
  the wrong day for every evening game on the US west coast — and
  `pro_team_game_days()` derived them a second, inconsistent way. Both are
  replaced by `scoring_period_dates()` in US Eastern.
- `player_projections` sent a `limit` without a sort, which ESPN rejects with
  `400 FILTER_LIMIT_MISSING_SORT`.
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
