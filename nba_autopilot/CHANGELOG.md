# Changelog

All notable changes to this app are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Every write was addressed to yesterday for the first half of the morning.**
  The run of 2026-10-23 lost both leagues' entire lineup to this:

  ```
  ESPN rejected LINEUP (409): Transaction type can only be executed in the
  current scoring period
  ```

  ESPN's two services disagree about what day it is. `status.latestScoringPeriod`
  is the latest period it has *results* for, and it lags the period the league
  is actually in until it rolls over around midday Eastern — on 2026-10-22 the
  09:00 run read period 2 and the 09:54 run read 3, with no NBA game having
  finished in between. The write service does not lag: it accepts the current
  period and answers `TRAN_INVALID_SCORINGPERIOD_NOT_CURRENT` to every other
  one. The 09:00 runs had been sending a stale number for days and nobody
  noticed, because `set_lineup` sends nothing when the lineup is already
  optimal — the bug could only bite on a morning that actually had a move.

  The date is now the authority. The season calendar already knows which
  period a date is, in the same US Eastern the periods are keyed in, and it is
  cross-checked against ESPN every run; `latestScoringPeriod` is the fallback
  for when there is no trustworthy calendar or when today is out of season,
  which is the offseason and not a disagreement about anything. The correction
  happens once, in `Autopilot.settings_now`, because the same field drives the
  optimizer, the budget horizon, the games-used baseline and every write, and
  the only way those can be made to agree is to fix it before any of them read
  it. It is applied in `main` and not in `read` or `solve`: the read layer's
  contract is ESPN verbatim, and the solver is pure and has no clock.

  ESPN's own number is kept beside it in `espn_latest_scoring_period`, because
  `SeasonCalendar.verify` is asking whether our week model reproduces ESPN's
  `currentMatchupPeriod` and ESPN's two numbers lag *together* — pairing a
  corrected period with a lagging matchup would report a disagreement at every
  week boundary that was ours and not ESPN's. When the two differ the run says
  so in its notes, which is also the only place a calendar that had genuinely
  drifted would show up.

  The approve path recomputes rather than reading the period off the journalled
  payload: a swap can be approved from a notification hours — or a night's
  sleep — after the run that proposed it, and the period that was current then
  is exactly the one ESPN now refuses.

- **One player ESPN would not keep on IR stopped the lineup being set at all.**
  The run of 2026-10-22 produced this, on both leagues, before it had set a
  single starter:

  ```
  ERROR pipeline: rejected target lineup: AJ Dybantsa is not IR-eligible per ESPN
  ERROR pipeline: rejected target lineup: Anthony Black is not IR-eligible per ESPN
  ```

  Both men were sitting in IR slots that ESPN had since declared them
  ineligible for. There was no active spot to move them into — both rosters
  were full — so the target left them where they were, the validator vetoed the
  target for containing an illegal IR occupancy, and the whole day's lineup was
  discarded. Ten starting slots nobody was arguing about went unset because of
  one man in a slot we had not touched. Every run would have done the same
  thing, silently, until somebody read the log.

  The validator and the write preflight now refuse to *create* an illegal IR
  occupancy but tolerate one ESPN already has: `set_lineup` sends only the
  differences, so a player left exactly where he is generates no item and
  cannot be what a rejection is about. Refusing to make a bad state is right;
  refusing to work around one is how it got worse.

  Which man stays on IR is now also our decision rather than the matching's.
  Left to itself the assignment seats the returnee and strands whichever player
  it values least — possibly one sitting legally on the bench, who then fails
  validation for real. The cheapest returnee is held back explicitly, and the
  rest of the lineup is optimized around him.

  He is reported upward by id, because the fix is an add/drop and not a lineup
  move: freeing a spot means dropping somebody, which the lineup layer may not
  do. The surprise in the live data, worth recording because it would mislead
  anyone debugging from the injury status: both stranded men were **OUT**, not
  healthy. ESPN reported `injuredReserveEligible` false for a rookie who had
  not yet debuted, however hurt he was — so the trigger is ESPN's eligibility
  flag and nothing else. See ADR-023.

### Changed

- **The status page was rebuilt around the decision instead of the data.**
  *"it looks crappy. make it more modern and sleak"* — fairly. Every line on
  the page was grey text at some opacity, a proposal's reasoning ran four
  times the height of the move it explained, and eight runs' worth of notes
  and reviewer text sat expanded below the approve buttons, so the thing the
  page exists for was the smallest element on it.

  A swap now reads as what it is: the man arriving above the man leaving, each
  with a coloured pip, the projected gain as a pill beside the league, and the
  two buttons weighted so Approve is the filled one and Reject is quiet. The
  rationale goes behind *Show details* in full — the reviewer's caveat is
  always the last paragraph and is the part most worth reading, so it is
  disclosed rather than truncated. Status chips at the top
  answer the two questions the page is opened with: is it really writing to
  ESPN, and does it want something from me.

  Dark mode now actually works. The page named Home Assistant's own CSS
  variables and relied on them for the theme, but ingress serves it in an
  iframe and custom properties do not cross a document boundary — the
  fallbacks were what rendered, always light, inside a dark frontend. The HA
  variables are still named first so a theme can drive the page if one ever
  reaches us; the fallbacks are now a pair of token sets selected by
  `prefers-color-scheme`, which the iframe does inherit.

  Still no JavaScript, no template engine, no static-file mount, and every URL
  still relative. The disclosures are `<details>`.

- **A run now opens on what it changed, not on how it worked it out.**
  *"what it should show is clearly indicating each move that was automatically
  made today"*. A run's body led with its working notes — games-used
  arithmetic, shadow prices, every player it declined to drop and why — a
  dozen lines of them, with the lineup it had actually set compressed into one
  semicolon-joined sentence somewhere above. The moves are now listed one per
  row, each with the player's headshot, the slot he left and the slot he was
  put in, and the reason underneath; the notes moved behind a **Logs (n)**
  dropdown.

  A run is no longer a disclosure either — *"this should be shown, just logs
  in the dropdown"*. Collapsing the whole run was the right fix for a body
  that was a dozen notes long, but once the notes had their own control the
  body was a few moves, and hiding those made the page's one answer the
  thing hardest to reach. Every run now shows what it changed on the surface
  of its card.

  Lineup writes journal a `moves` array for this, since the page cannot
  recover the per-move detail from the target lineup: the difference is
  against the roster as it was, which is not journalled. Runs recorded before
  that array existed are parsed back out of their one-line rationale, so the
  history reads the same way — only without the faces, which need the player
  id. A rationale that does not parse cleanly keeps its paragraph rather than
  showing a half-read list as if it were the whole move.

  The headshots come from ESPN's public CDN (`a.espncdn.com`), with no cookies
  and no identifiers of ours attached. It does mean the browser showing this
  page tells ESPN which players are on it. A player ESPN has no photo for
  degrades to an empty disc: the image is a CSS background rather than an
  `<img>`, so there is no broken-image icon and no script needed to handle
  one.

- **Each league gets its own column of runs.** *"can we put dynasty and the
  other league side by side two columns rather than one combined centered
  column"*. The runs were one list in time order, and since both leagues run
  within a second of each other the list simply alternated between them: every
  card had to be read to find out whose it was. They are now grouped under a
  heading per league, side by side on a wide screen and stacked on a narrow
  one, with the league named once instead of once a card — which leaves the
  run itself led by when it ran, the thing that actually tells two of the same
  league's runs apart.

  Column order follows the configured order of the leagues rather than
  recency, so it does not change under the reader between refreshes.

- **Swaps awaiting approval are in those columns too, and nothing explains
  itself before it is asked to.** *"needs approval should be in the sepearate
  columns please. and ALL the subtext should be hidden in show details which
  includes the calculations and reasoning"*. Two things:

  The approval queue was a single centred list while the runs beneath it were
  two columns, so a waiting swap sat above neither league in particular and
  had to name its own league on every card. It is now the same grid in the
  same configured order, a column per league — including a league with nothing
  waiting, which gets a *Nothing waiting* card rather than silently letting
  the other league's swap slide under its heading. The league name comes off
  the card with it, and the kind takes that slot.

  And the rationale now discloses whole. It kept a clamped three-line lead
  outside the control, which meant every card opened with the first third of
  its own arithmetic — shadow prices, the games-used figure, the matchup score
  — above the two buttons it exists to offer. Closed, a card is the swap and
  the decision; the pricing and the reviewer's caveat are one tap away and
  unabridged when they arrive.

  The header band is capped at reading width but no longer centred within the
  wide wrapper: with both lists now full-width grids, a title and status chips
  floating 190px to the right of the first column read as a different page
  laid on top of this one.

### Added

- **An approved add/drop clears its card instead of sending a second push.**
  Tapping Approve produced a confirmation notification for a decision the user
  had just made by hand — *"i get a notification when i accept the add drop
  request. dont need that"* — while the card he tapped stayed on the lock
  screen still offering Approve and Reject for a swap that had already
  executed. The card is now taken down and nothing is posted in its place. The
  lineup change that follows the swap is still announced, because that part he
  did not do.

  The tag is built in one place now (`bridge.addrop_tag`). The teardown and the
  card disagreed about how to spell it — the teardown used the run id where the
  card used the league id and the proposal id — so it posted an unrelated
  second notification and left the original buttons live.

- **A swap the reviewer blocks no longer uses up one of the run's moves.**
  `addrop_max_moves` counts swaps that reach the user, and a vetoed swap is not
  one. The first live run with the review switched on spent all three of its
  moves on candidates the reviewer refused — correctly, in all three cases —
  and ended with an empty list, never pricing a fourth it might have approved.
  The funnel now keeps looking past a veto, bounded instead by a separate
  budget of five candidates shown to the reviewer per league per run, since
  each review costs a request and several seconds. Blocking all five is a
  normal outcome and is recorded as one, with a note that tells it apart from
  "the pool had nothing in it" — the two look identical from outside and call
  for opposite responses. See ADR-022.

  Fixes a related defect in the conditional-pricing line, which was derived
  from the attempt number. A blocked candidate is never applied to the
  simulated roster, so the swap found after a rejection is still priced against
  the real roster — yet the first card could claim its number assumed a swap
  "above it" that the reviewer had thrown away. It now counts approved swaps.

  And a veto is no longer reported as configuration. The vetoed name used to be
  appended to the `never_drop` list passed down to the solver, so a league with
  an empty `never_drop` was told *"not droppable: Aaron Gordon (never_drop),
  Karl-Anthony Towns (never_drop), Walker Kessler (never_drop)"* — sending the
  user to look for a setting he had never made. The reviewer's own refusals now
  travel separately and say so.

- **A run can offer up to three swaps, each approved separately.** New
  `addrop_max_moves` option (1–5, default 3). Previously a run that found three
  worthwhile moves showed the best one and left the other two until tomorrow,
  which in a four-day matchup period is a quarter of the week. Each swap is its
  own notification, its own row and its own tap — there is no basket approval,
  because a drop is irreversible. The notification tag is now per proposal;
  iOS treats the tag as an identity, so a single league-wide tag meant the
  third card replaced the first two on the lock screen.

  The list is re-priced rather than ranked: who should go out depends on who is
  on the roster, so `_consider_addrops` works out the best swap, advances a
  simulated roster and pool through it, and asks again. Ranking against a fixed
  roster would have produced three cards naming three different pickups and the
  same single man going out, of which exactly one could be approved. Every
  entry after the first says on the card that its number assumes the ones above
  it. The acquisition counter advances with the list, so the league's own limit
  caps the real total, and a man picked up earlier in a run is never offered as
  a drop later in it (`just_added`, reported as its own reason rather than as a
  `never_drop` entry it is not).

  The review runs per swap, and its two refusals now mean opposite things: a
  judgment retires that pair and the run carries on looking, while an
  unreachable reviewer stops the search, since every remaining swap would be
  withheld for the same non-reason. See ADR-021.
- **An acquisition rebalances the lineup immediately.** A pickup on your bench
  is worth nothing, and the day's lineup was written earlier in the run against
  a roster that did not contain him. So the moment a swap actually lands — in
  the autonomy path, or hours later when you tap Approve — the roster is
  re-read from ESPN, the lineup is re-planned against it and written, and you
  get the swap notification first and the lineup change second. Waiting for the
  next scheduled run would deliver the points the swap was approved for after
  the games it was meant to win. A pending waiver claim, a shadow run and a
  refused write all mean the roster did not change, and none of them rebalance;
  `autonomy_lineup` is honoured, because approving a swap says who should be on
  the roster, not who should be in the starting five. See ADR-020.
- `plan_from_roster` and `LineupWork`, so the roster-to-lineup chain has one
  call site rather than two. `plan_lineup` takes eight arguments that each
  change the answer; a second hand-assembled copy would be eight chances to
  compute a different lineup for the same roster, and both would look valid.
- **Every proposed swap is reviewed before you see it.** Set
  `anthropic_api_key` and `judge.py` shows each fully-priced add/drop to Claude
  first — both players' projections, games left, ownership share, draft
  position, ESPN's preseason rank, the beat reporters' own injury wording, the
  matchup score and the model's full reasoning — and asks whether the
  statistical case survives contact with what those numbers cannot encode. It
  can only **block**: it never picks a player, never proposes a move and never
  ranks the pool, so a rejection is checkable against the case it was shown.
  When it blocks something it says why, in its own words, in that run's notes.
  This exists because the week-surplus model is blind outside the current
  scoring period by design, which makes a first-round pick on a three-day
  injury worth exactly zero and therefore the cheapest man on the roster to
  give away — observed twice in two runs on a live roster. See ADR-019.
- Four **market readings** on every player — `percent_owned`, `draft_adp`,
  `draft_rank` and ESPN's `seasonOutlook` — carried from the wire through to
  the decision layer and priced by **nothing**. Ownership share is thousands of
  other owners' answer to the exact question a drop asks, and it is not
  denominated in fantasy points, so it informs the review and never enters the
  arithmetic. A test fails if that changes. `None` rather than `0.0` when ESPN
  omits a field: an unowned player and an unreported one must not read alike.
- The review **fails closed but not off**. With no key configured there is no
  gate and nothing changes. With a key configured, a swap the reviewer could
  not be reached about is withheld rather than shown unexamined — a timed-out
  request is not permission to make an irreversible, publicly attributed move
  — and "blocked on the merits" stays distinguishable from "could not be
  reached" in the notes, so a week of API outages does not read as a week of
  good judgment.
- **Availability now reads the beat reports, not just the status field.**
  `sources/espn_injuries.py` fetches ESPN's NBA injuries page and uses it in
  two tiers. Tier 1 is the structured `status`, which turns out to have only
  two values in practice — `Out` and `Day-To-Day` — and that second bucket was
  hiding the whole decision: on the live page it covered players listed
  probable, questionable, doubtful, *and* players already ruled out for that
  night. Treating them alike means a `p_play` of 0.75 for a man who will not
  play. Tier 2 recovers the distinction from `shortComment`, which is the beat
  reporter's own attributed, timestamped sentence that ESPN bundles into the
  feed ("Hawkins (illness) is out for Wednesday's game against the Raptors,
  K.C. Johnson of Chicago Sports Network reports"). No scraping and no
  language model: the wording is already structured enough for a regex, and
  every refinement records the phrase it matched so it can be audited in the
  run notes. Measured against the 80 live notes on 2026-10-21 it refined 25 of
  28 `Day-To-Day` entries. The three it did not — practice updates carrying no
  game-level verdict — fall back to the coarse status rather than guess.
- A **ceiling rule** on that refinement: a comment may only improve a note up
  to what its structured status allows (`Day-To-Day` tops out at `PROBABLE`,
  `Out` at `OUT`). Being on the injuries page at all means ESPN does not
  consider the player cleared, so no amount of optimistic wording can promote
  a note to `ACTIVE`, and a comment reading "probable" beside a structured
  `Out` is a mixed or stale note rather than permission to start someone.
  Clamps are recorded on the note instead of applied silently.
- The join needs **no crosswalk**: ESPN's fantasy `playerId` and its site
  `athlete` id are the same number (verified 2026-10-21 — Mouhamed Gueye is
  4712863 in both the fantasy player list and the injuries page). Players
  whose links carry no id fall back to a normalised-name lookup and are listed
  explicitly, per the contract's unmatched-list rule, rather than dropped.
- The source **holds no cookies**. It builds its own client rather than
  accepting the shared one, which is a deliberate inversion of this package's
  convention: the fantasy session cookies live in the same process, and making
  it impossible to hand them in is the only way to guarantee `espn_s2`/`SWID`
  never reach a public endpoint.
- `run_league` reports the page in `sources_used`/`sources_missing` and writes
  every disagreement between ESPN's fantasy `injuryStatus` and the injuries
  page into the run notes. It **reports** the disagreement rather than
  resolving it: `p_play_for` still honours an override only when it is *worse*
  than ESPN's reading, so a stale note can bench a player but never start one.
  The page is cached for 15 minutes — long enough for every league in a cycle
  to share one fetch, short enough that a 6:40pm scratch still lands before a
  7pm tip.
- `tests/fixtures/espn_injuries.json`, eight real notes trimmed from that live
  page, so these tests fail when ESPN's feed changes rather than when an
  invented example stops parsing. The fetch tests pin `base.utcnow`, because
  staleness is measured against the wall clock and an unpinned fixture would
  pass today and start failing on its own in a couple of days.

- **The ingress status page actually exists**, in the sidebar, listing every
  proposal waiting on a decision with its rationale next to an Approve and a
  Reject button, plus the last eight runs and their notes. A notification is a
  one-shot thing — miss it, swipe it away, or read it on a locked screen and
  the approval is gone until the proposal expires six hours later. This page is
  always there and survives being ignored. It calls the same
  `Store.decide` → `execute_approved` pair a notification tap calls, so the
  double-decision guard, the stale-proposal guard and the audit trail are
  shared rather than duplicated; and it mirrors the tap's owner check, so
  another Home Assistant user on the same instance cannot drop a player.
  Decisions from the page are attributed to the Home Assistant user who made
  them. Every URL it emits is relative, because ingress serves the app under a
  rotating `/api/hassio_ingress/<token>/` prefix, and everything interpolated
  into it is HTML-escaped — rationale text carries ESPN player names today and
  LLM output later, and the page renders inside the authenticated frontend.
- `Store.query`, a locked read-only helper, so the page never touches the
  shared SQLite connection directly. Runs execute on a worker thread, so an
  unlocked cursor would interleave with their writes.

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

- **Tapping a notification landed on the home dashboard, again.** The deep
  link was *constructed* as `/hassio/ingress/<slug>`, which is only a frontend
  route while an ingress add-on has no sidebar panel of its own. This one
  declares `panel_title`, so Supervisor registers a top-level panel instead and
  the route is `/<slug>` — `get_panels` on the Green returns `url_path` of
  `d0cfb82c_nba_autopilot` and no `/hassio/ingress/...` route at all. Handed a
  path matching no route, the companion app falls back to the default dashboard
  without an error, which is exactly how the previous hardcoded
  `/lovelace/fantasy` failed. The route is now *looked up* in the panel
  registry that builds the sidebar, matched on `config.addon` against the
  Supervisor's own slug; the constructed ingress path is kept only for the case
  where no panel claims the add-on, where it is genuinely correct. An
  unreadable registry omits the `url` rather than guessing. Reported from the
  phone both times, because the tests had never asserted against what the
  frontend says the route is — now they do.
- **An empty approval queue no longer looks like a malfunction.** Tapping a
  "2 move(s)" notification and finding "Nothing waiting" reads as a broken
  page. Lineup and IR moves are applied without asking, so the empty state now
  says so and points at Recent runs, where they are.
- `sources_used["espn-injuries"]` now records **how many rostered players were
  found on the page**, not just how many notes the page carried. Observed
  on-device 2026-10-21: 80 notes, 61 refined, and exactly 2 of 24 rostered
  players listed — both already `OUT` in ESPN's fantasy feed, so the run
  produced no injury notes at all. Without the join count in the record, that
  silence is indistinguishable from the join matching nobody because the two id
  spaces drifted apart.
- **No scheduled run could ever have executed.** The daily job was a `lambda`
  calling `asyncio.create_task`, and `AsyncIOScheduler` awaits a job on the
  event loop only when the job *is* a coroutine function — anything else it
  hands to a worker thread, where `create_task` raised `RuntimeError: no
  running event loop`. Startup logged "scheduled daily run at 09:00" and looked
  healthy; the failure only existed at the moment of firing. Found by pointing
  the live instance at a run time two minutes out instead of waiting for
  morning. The job is now an `async def` with a test asserting that predicate
  directly, plus a `misfire_grace_time` of an hour — the one-second default
  would discard a whole day's run if the loop were busy at 09:00.
- **The app refused to start**, because the s6 run script still required the
  `espn_league_id` and `espn_team_id` options that `leagues` replaced — so a
  correctly configured app exited 1 with *"Required option 'espn_league_id' is
  not set"*, naming a setting that no longer exists in the Configuration tab.
  Found on the first real install. The guard now covers only the scalar
  options; `leagues` is checked by `_parse_leagues`, which can say *which*
  entry is wrong, and which `bashio::config.has_value` could not have checked
  anyway — it sees an empty list as the string `"[]"` and passes it.
- **Nothing was listening on the ingress port.** `config.yaml` declared
  `ingress: true` and `ingress_port: 8099`, the 0.1.0 notes listed an "ingress
  status UI", `fastapi` and `uvicorn` shipped in the image, and the approval
  notification's deep link resolved to the app's ingress path — but no server
  was ever started, so following that link opened the Home Assistant app on its
  home page. The notification tap was not the weakest approval surface; it was
  the only one.
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
