# Changelog

All notable changes to this app are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Where the game actually is, on every roster row.** A tip-off time that has
  passed is the one thing on a row that is certainly no longer true. Once a
  game starts it is replaced by `@NYK · Q2 1:16 · 25-20` — his team's score
  first — and the minutes he has played appear under his points as `17m`.
  Also `Half`, `End Q3`, `OT 2:11` and `Final`. On the lineup editor and the
  matchup sheet both.

  12.0 in six minutes and 12.0 in thirty-six are the same cell and opposite
  facts, which is what the minutes are there to settle. They come off the same
  ESPN stat row the day's points already came from, so they cost no request:
  `_day_points` became `_day_row` and the caller reads two fields instead of
  one.

  The quarter and the score come from ESPN's public NBA scoreboard at
  `site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard` — a
  different host from the fantasy API, no cookie, nothing private on it. The
  two disagree about what a team is *called* (the scoreboard says `NY` and
  `SA` where the fantasy API says `NYK` and `SAS`) and agree about its **id**,
  so the join is the id and the abbreviation is never read. Cached for 25
  seconds, short because a game clock is the one thing on these pages that is
  wrong the moment it is stale; read only for today, because ESPN answers for
  the current slate and asking it about Thursday would file tonight's clock
  under Thursday's games. A scoreboard that cannot be read costs the clock and
  nothing else.

  A live game also beats an injury tag on the line, but only once he has
  minutes on the board: "Day-to-day" is a guess about today that minutes have
  answered, while a man who is out should keep saying so as his team plays
  without him.

- **The player card.** Clicking a name now opens his card over the page you
  were on, instead of sending you to ESPN: season and trailing 7/15/30-day
  averages, a game log of every game he has played with the fixture and what
  he scored, and Rotowire's recent notes with ESPN's "Spin" on each.

  The numbers are ESPN's own `appliedTotal` and `appliedAverage` under the
  league's scoring, not recomputed here, so the card can be held up against
  ESPN's own and agree with it — including the rounding, which is half-up
  rather than Python's half-to-even (`"%.1f" % 7.25` is `7.2` here and `7.3`
  there).

  Fantasy points lead both tables rather than closing them as they do on
  ESPN. Eight numeric columns do not fit a phone, so the table scrolls, and
  ESPN's order puts the one column it is read for off the right edge. Pinning
  it there with `position: sticky` was tried first and is worse: the pinned
  cell sits over whichever column is beneath it and the numbers show through
  around its edges.

  Built on two endpoints. `view=kona_playercard` on the league returns the
  aggregate splits **and no per-game rows at all** — 7 stat blocks instead of
  88 — unless the `X-Fantasy-Filter` carries
  `filterStatsForTopScoringPeriodIds`; neither `filterIds` alone nor the
  `scoringPeriodId` query parameter is enough, and the failure is a 200 with a
  silently empty game log. The filter was read off ESPN's own request by
  hooking `XMLHttpRequest` and opening the card, not guessed. News comes from
  the public, cookie-free
  `site.api.espn.com/apis/fantasy/v3/games/fba/news/players`, whose `v3` shape
  nests the feed one level deeper than `v2` did.

  The dialog is a progressive enhancement over a real page at
  `player?league=…&id=…`: no JavaScript, an unsupported `<dialog>`, a failed
  fetch or a modified click all fall through to navigating there.

### Fixed

- **The games-played cap was priced against the wrong number, and it was
  benching the best players on the roster.** Games used this matchup period is
  read two ways: `statBySlot`, the counter ESPN enforces the cap against, and
  a reconstruction from the season-to-date total minus a baseline snapshot.
  When they disagreed the larger won. The argument for that was asymmetry —
  over-counting costs a marginal start, under-counting costs a legal lineup —
  and it assumed the two readings were equally plausible. They are not. The
  reconstruction rests on a baseline snapshot taken at the right moment *and*
  on this app's **derived** matchup-period boundaries, derived because ESPN's
  own `matchupPeriods` map is self-referential and demonstrably not what ESPN
  is using.

  2026-10-28 was the first day the two could differ — before that,
  matchup-to-date and season-to-date are the same number by construction — and
  the reconstruction lost in both leagues at once: `statBySlot` said 10 and 8,
  the delta said 25 and 18. Counting the lineups of every completed scoring
  period of the matchup period by hand gave 10 and 8.

  The cost was not marginal. Pricing a 44-game cap as 19 games from exhaustion
  when 34 remained put the shadow price at 32.8 points a game, which benched
  Keyonte George (31.4 projected, **44.6 actual** over his four games), benched
  Ace Bailey (29.6 projected, **36.0 actual**), left a UTIL slot empty and left
  three starting slots occupied by players with no game or ruled out. With the
  enforced counter the price falls to 25.9 and all of them start.

  `statBySlot` now wins any disagreement. The reconstruction is still computed,
  logged and reported, because a reconstruction that disagrees is how this was
  found.

- **The player card's edges.** Three things on a phone, all of them the card
  looking broken where it was only being cut off.

  The facts beside the headshot were an even grid, so every column was as wide
  as the widest label and a manager called "Brendan Bridges" wrapped onto a
  line of its own inside a column with room to spare either side of it. They
  are a wrapping strip now, each fact as wide as it needs to be. The close
  button's width was reserved down the whole hero rather than only beside the
  name, which was costing that strip a column.

  Both stat tables and the dialog body cut their content off flush at an edge
  with nothing to say it scrolls — a sentence sliced in half at the bottom of
  the dialog, a column sliced down the middle at the right of the table. Both
  now carry scroll shadows, drawn the CSS-only way: a pair of cover layers
  attached to the content (`local`) sit over a pair of fixed shadows and
  travel off them as you scroll, so the shadow appears only on the side that
  has more to show.

  And the dialog now says `position: fixed` itself. It is what a current
  browser's UA stylesheet already gives an open modal, but an older WebView
  says `absolute`, which anchors the card to the top of the *document*: open
  one from halfway down the matchup page there and it is above the screen.

- **Every player name linked to the same page.** A name opened the league's
  player *list* narrowed to his NBA team, which was the closest thing found
  the first time this was looked at. Two things were wrong with it. The page
  is a list, not a player — he was one of about fifteen rows on it. And three
  of the four places a name is rendered never passed the NBA team the filter
  needed, so those names fell back to the unfiltered list of every player in
  the league: the add/drop queue, the history, and both legs of every swap all
  pointed at one identical URL.

  A name now opens `playernews?leagueId=&playerId=`, which is a page about
  that one man — his injury status and ESPN's note and "Spin" on each piece of
  news about him. That route was listed as existing in the previous note and
  never tried with an id on it; it does read one. Verified by loading
  `playerId=1966` and `playerId=3133628` and confirming each renders exactly
  one headshot and that it is the right man's, and that dropping the parameter
  gives the whole league's news feed instead.

  The card with position rank and percent rostered still has no URL, and this
  time that was established by opening it rather than by inference: in ESPN's
  own tables a player's name is an `<a>` carrying no `href`, and `location.href`
  does not change when the card opens. `playercard`, `player/card` and `player`
  all render ESPN's "Page not found". ESPN's own card links out to
  `espn.com/nba/player/stats` for the stat line.

- **A triple-double was not being paid as a double-double.** ESPN's bonus
  counters are cumulative, not mutually exclusive: of the **149** triple-doubles
  in the 2024-25 season archive, **149** also carry stat 37. The model read
  `P(exactly n)` off its convolution, so a triple-double collected the 8.0 and
  not the 4.0 that comes with it.

  The subtler half of the same bug: split `00` reports the *observed* rates
  cumulatively too, so `derive_scored_stats` was blending a modelled
  `P(exactly 2)` against an observed `P(at least 2)` — two different
  quantities. Now a tail sum, `P(at least n)`, on both sides.

  Found by validating the model against five seasons of real box scores rather
  than by reasoning about it. The same check confirmed the model is otherwise
  well calibrated — predicted double-double rate 0.082 against an actual
  0.086 over 1,774 player-seasons — and that `OREB_SHARE_DEFAULT = 0.25`,
  which was a guess, is very nearly exact: the league-wide offensive rebound
  share is **0.2519**.

### Added

- **Opponent defence in the projection.** A player's projected line is now
  scaled by who he faces that day: one multiplier per NBA team, plus an extra
  adjustment for centre-eligible players only.

  Measured against **131,986 player-games** over five seasons, pulled from a
  **public, unauthenticated** ESPN endpoint. Full working in
  `docs/opponent-defence.md`. Team defence is a real and persistent property
  — split-half reliability **0.75–0.90**, year over year **r = 0.43–0.61** —
  but splitting it across all five positions is noise (reliability 0.25). The
  signal collapses onto a single centre axis, where reliability is **0.56**.
  So the fit deliberately refuses to model the other four positions.

  Worth **+1.2 fantasy points a week** out of sample, nearly all of it on the
  ~17% of days where it actually reorders a lineup. Recorded plainly because
  the rebound fix above was worth 30–55 a week in the same units: this is
  25–45 times smaller and is included because it is nearly free, not because
  it matters much.

  An unresolved opponent, an unseen team, or a fit that could not be obtained
  all give exactly 1.0 — the behaviour the app had before this existed. The
  multiplier is applied to the raw line *before* the derived stats are
  computed, so a centre facing good rim protection also gets a lower
  double-double chance rather than only lower rebounds. It deliberately does
  not touch percentages, minutes, or games played: a hard opponent does not
  change how many games a player appears in, and `GP_STAT` feeds the
  games-played budget.

- **A local archive of ESPN per-game actuals.** `tools/archive.py` pulls whole
  seasons into SQLite through the same parser the add-on uses, and
  `tools/measure_defence.py` re-runs every measurement behind the opponent
  model. Neither is imported by the add-on and neither needs credentials. One
  request returns ~1,080 players with a row per game played — including the
  offensive/defensive rebound split and the real double-double counters, none
  of which ESPN's *projection* carries.

  Two traps are documented and handled: `seasons/<Y>/players` returns season Y
  **and** Y−1 in one response, and `proTeamSchedules_wl` contains no season id
  anywhere, so the season has to be supplied by the caller. Getting either
  wrong produces a plausible-looking archive with the wrong opponent on tens
  of thousands of games.

- `defense` table in the state database, caching the fitted table for a week.
  Persisted rather than held in memory because the add-on's normal mode is one
  process per run, where an in-process cache would refetch 25 MB on every
  wake-up and never once hit.

- **Every rebound in both leagues was worth exactly zero.** The projection and
  the scoring were in different stat spaces and nothing bridged them.

  ESPN's season projection (split `10`) publishes total `REB` (stat 6). Both
  live leagues score `OREB` (4) at 1.5 and `DREB` (5) at 1.0 and put **no
  coefficient on the total at all**. `PointsValue.conditional_value` sums
  `coeff * mean.get(stat, 0.0)` over the scored stats, so both components found
  nothing and contributed nothing, for every player, on every run since the app
  was built. The same hole swallowed double-doubles, which both leagues price
  at 4.0/8.0/15.0 and ESPN projects for nobody.

  The bug was never in the value model, which was correctly summing what it was
  handed. It was that the mean had holes in it exactly where the league's money
  was. `solve/projections.derive_scored_stats` is the bridge: it splits
  projected rebounds by the player's own offensive share (his actuals where he
  has any, a league-average 25% otherwise) and estimates the double-double rate
  from his projected line, shrinking toward his observed rate as games
  accumulate.

  Found by replaying week 1 of the 2026-27 season against the actual box
  scores, which is worth recording because nothing about the symptom pointed
  here. What showed up first was that the best legal lineup each day would have
  scored **9% more** than what was started, and that the same three names kept
  appearing on the bench behind guards who outscored nobody: Walker Kessler
  (benched for 51.5, then 60.5), Donovan Clingan, Zach Edey. Kessler was priced
  at 19.94 fantasy points a game against 10.8 projected rebounds, and returned
  41.3.

  The residuals say why a near-zero *average* error hid it. Actual minus
  projected over week 1 came to +3.0 and -2.6 fantasy points a game across the
  two rosters — unremarkable — and split by rebounding into **+10.4 for players
  above seven rebounds a game against -1.7 for everyone else** in the worse
  league. A uniform bias cancels out of a ranking. A bias that tracks position
  is the one kind a lineup optimizer cannot absorb, because ranking is its
  entire job.

  Replaying the same six days through the fixed code, holding projections,
  eligibility and the games-played cap constant so that pricing rebounds is the
  only change:

  | | actually started | before | after | hindsight oracle |
  |---|---|---|---|---|
  | Chairman's Fantasy | 1135.5 | 1212.5 | **1243.5** | 1252.0 |
  | Dynassty | 1392.0 | 1414.5 | **1470.0** | 1527.0 |

  **+31.0 and +55.5 fantasy points a week**, closing 93% and 41% of the gap to
  a lineup that knew the scores in advance.

  One consequence worth stating plainly: the add/drop layer had proposed
  **dropping Walker Kessler**, and the AI reviewer blocked it. The gate was
  doing its job, and it was covering for an arithmetic hole — which is the
  wrong division of labour. A language model's sense of who a player is should
  not be the thing standing between a missing term in a dot product and an
  irreversible transaction.

  A rate is not a count, so `variance_from_mean` now gives the bonus stats
  Bernoulli variance `p(1-p)` rather than `PHI * mu`, which on a 0.3
  double-double rate would have claimed 0.48 — more than the 0.25 a Bernoulli
  can reach at all. Inert today, since the risk term needs a matchup state
  nothing supplies, and wrong in the direction that would have penalized
  rebounders again when it goes live.

  A league scoring both total rebounds *and* the split is now refused by
  `build_value_model` rather than paid twice for the same rebound. Neither live
  league does this; the check is the difference between a loud failure and a
  plausible-looking wrong ranking if a third league is configured differently.

### Added

- **A `forecasts` table: what the model believed, one row per player per run.**
  The projection it used, the availability it assumed, and whether it started
  him.

  Nothing reads it yet, and it is written unconditionally anyway, because of an
  asymmetry that decides the whole design: **actuals are permanent and
  forecasts are perishable.** ESPN will replay any past day's lineups and box
  scores on request — `scoringPeriodId` rewinds both — so "what happened" can
  be reconstructed at any time, for any day. But the projection is split `10`,
  which ESPN *overwrites* as the season moves. Nothing anywhere recorded what
  the number was on the morning a lineup was set, and once the day passes that
  reading cannot be recovered from any endpoint. Every run that does not write
  here is a day of evidence gone for good.

  `projected` is `conditional_value` — fantasy points *given that he suits up*,
  before the cap shadow price. Deliberately the same conditioning as the actual
  it will be compared against, since a player with a box score played.
  Multiplying by `p_play` here would compare an expectation against a
  realization and read forever after as a negative bias. `p_play` is stored
  beside it, because how well the availability model does is a different
  question with a different answer.

  Keyed on the run rather than the day, so a second run in a day inserts rather
  than overwrites; reconciliation takes the newest run per period, since an
  earlier lineup was superseded and is not what the day was played with.
  Wrapped whole and swallowing everything, like the scoreboard write and for
  the same reason: it runs before the ESPN write, and bookkeeping may not cost
  a run the part that matters.

  The rebound bug above was found by doing this reconciliation by hand, before
  the table existed. That is the argument for the table.

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

- **Player names open the fantasy player list, not ESPN's NBA page.** *"the
  player link doesnt load the fantasy pl,ayer sctreen... it loads their stats
  which isnt as helpful"*. Fairly: tapping a name mid-decision is asking "what
  has he been scoring and does anybody else want him", and the NBA page
  answers neither.

  A name now goes to that league's fantasy player list, narrowed to his own
  NBA team — about fifteen rows with him among them, carrying fantasy points,
  percent rostered and projections. Every name beside a roster knows its
  league, so this is every name on the matchup sheet, the lineup editor, the
  approval queue and the run log.

  It is not his own fantasy page because **ESPN does not have one**. Checked
  three ways on 2026-10-26 rather than assumed:

  * the fantasy app is a Next.js app, so its routes are its page bundles, and
    the only ones under `/basketball` are `league`, `league/settings`, `team`,
    `boxscore`, `recentactivity`, `playernews` and `players/add`. `playercard`
    and `player` fall through to the ESPN homepage — in football too, so the
    familiar `fantasy.espn.com/football/playercard` is also not a thing;
  * `players/add` reads exactly `leagueId`, `seasonId`, `status`, `proTeamId`,
    `lineupSlot`, `playing`, `view`, `replayDate` and `previousSeasonId` off
    the query string. A `playerId` on it is silently ignored;
  * the legacy `games.espn.com/fba/playercard?playerId=` 302s to a generic
    landing page and drops the id.

  `status=ALL` and not ESPN's default: the default availability filter is
  "available", and most names on this page are players we already roster —
  exactly the ones it hides. A player whose pro team is unknown is not
  filtered to team `0`, which is ESPN's "no team" and would list nobody.

  A name with no player id behind it stays unlinked, as before. The league
  list is not a substitute: without the id there is no pro team either, so
  nothing would narrow it, and "this player" would resolve to "some players".


- **The status page is now three bands of the same league columns.**
  *"this should show two cards on each row saying no updates for each league.
  next for matchip score they should be wider and matching each column at the
  top as well. so each column is score card, then approvals, then the rest"*.

  The page is read down a column, not across a row — the score, the swap it
  argues for and the run that applied it are one league's story — and two of
  the three bands were not honouring that:

  * The scoreboards sat *beside* the masthead, which used dead space but
    squeezed them into half the width. At that size they were narrower than
    the columns underneath and landed over the gutter between them. They are
    now their own full-width band under the title, sharing one
    `grid-template-columns` rule with the lists below rather than a second
    rule that happens to agree: `.leagues, .boards`. A league whose score
    could not be read holds its cell with an empty `<div>`, because
    collapsing it would slide the next league's panel over the first league's
    approvals — a misalignment with no visible cause.
  * An empty approval queue was one full-width card. That answers "is
    anything waiting" but not "is anything waiting *in this league*", which
    with two leagues is the question actually being asked. The per-league
    grid now renders whether or not anything is pending, so the answer is two
    cards and the columns keep their positions. Recent runs does the same,
    for the same reason: one league having not yet run must not shift the
    other league's column.

  The sentence explaining *why* the queue is empty — only add/drop asks a
  human, so a notification saying "2 move(s)" with nothing to approve reads as
  a broken page — moved out of the card and under the heading as a lede. Said
  once it reads as how the thing works; said twice, once per column, it reads
  as boilerplate.

  Two breakpoints went away with the margins they were turning off. The gap
  between stacked columns was `margin-top` on the second one, cancelled at
  `min-width: 1000px` — but `auto-fit` starts pairing the columns at about
  690px, so between those two widths the right-hand column sat 18px lower
  than the left. It is a grid `row-gap` now, which applies between rows and
  only between rows, and so cannot disagree with the template about how many
  rows there are.

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

- **The swaps it did not put to you: a dropdown under each league's approval
  column.** *"id like a way to view rejected add drop reuqests (hidden
  somewhere in logs or xsomething, like when it says Nothing waiting for add
  drop it should have a drop down where i can see what it considered"*, then
  *"no i dont care about what I did. i care about the ones you and the ai
  judge decided to not surface"*.

  So only the app's own refusals: a swap the owner rejected and one that
  expired unanswered are his own decisions, already known, and the run log has
  them. Two outcomes remain, kept apart because they are different events --
  *Reviewer said no* is the gate working, *Review unavailable* is a degraded
  run, and folding them together would make a week of outages look like a week
  of good judgment. That is the same distinction `judge.Verdict.errored`
  exists upstream to preserve, and it now rides in the stored payload so the
  page never has to read it back out of prose.

  Refusals from before this landed were never rows: the only account of them
  is the sentence the run wrote in its own notes. Those are parsed back and
  listed, so the feature is useful on the day it ships rather than in a week
  -- 26 of them on the live instance, with the reviewer's full reasoning. A
  run that recorded rows never has its notes read, so nothing is listed twice
  and the fallback retires itself as rows accumulate. A recovered entry is one
  line with no headshots and no links, because that is all the note kept; it
  is shown as the smaller record it is rather than as a degraded card.

  Prose is a bad thing to parse and a worse thing to depend on silently, so a
  test asserts that the live reviewer path still writes sentences the parser
  matches. The failure mode of the wording moving is otherwise a dropdown that
  quietly goes empty.

  The cap is per league and not over the list. A global one would let the
  busier league crowd the other out of its own column, leaving a league with a
  fortnight of history answering "nothing".

  "Nothing waiting" is true and unsatisfying on its own. It cannot tell a
  quiet week from one where the reviewer turned down six swaps a day, and
  those call for opposite responses. The dropdown names both players, says
  why, and puts the reviewer's own words behind the same *Show details*
  control the live cards use. Closed by default, and not rendered at all for a
  league that withheld nothing.

  A blocked swap used to leave only a line of prose in a run's notes, so it
  was not retrievable as *a swap* at all. It is now a proposal row in a new
  state, `BLOCKED`. That is safe for one specific reason: every transition out
  of a proposal is a guarded `UPDATE` on the state it starts from, so a row
  born terminal can never be approved, executed or swept — and the tests that
  matter are the ones that try all three anyway.

  `_consider_addrops` still writes nothing. It returns what it turned down
  alongside what it chose, and the caller — which holds the `run_id` and owns
  every write in a run — records it. That separation is why a pool fetch that
  fails costs the run nothing it had already earned, and it was worth
  preserving over the shorter change of writing from inside the search.
  Recording is wrapped whole and swallows everything: it is a record of
  something that did *not* happen and must not take down something that did.


- **Previous matchups, and the week as a strip of days.** *"id love to be able
  to toggle back to previous matchups"*.

  Arrows beside the week number step back through finished matchups — a
  different opponent each week, each with its own final score, its own
  games-used and its own day-by-day. They stop at the week being played:
  ESPN's schedule carries every *future* fixture as a real row with a real
  opponent and 0.0–0.0 on it, so an unbounded arrow walks into seventeen
  matchups that have not happened and all look alike.

  The pair of day arrows underneath is gone, replaced by the whole week as a
  row of chips, each carrying what **both** sides scored that day. The arrows
  made a reader page blindly through six days to find the one that decided the
  week; the strip puts it on one line — *we lost it on Thursday* is visible
  before anything is tapped, and Thursday is one tap away. The numbers are
  free: `pointsByScoringPeriod` is on the same matchup row the totals already
  come from. A day ESPN has published nothing for shows a dash rather than a
  zero, the same distinction the per-player column makes.

  A finished week now reads in the past tense — `FINAL`, and *Won by 105.0* or
  *Lost by 31.5* instead of *Ahead* or *Behind*. `MatchupScore.outcome`
  normalizes this where it can be done correctly and nowhere else: ESPN
  reports `winner` as `HOME`/`AWAY`, which means nothing without knowing which
  side of the row we are on, and the row is the only thing that knows. Probed
  on 2026-10-26, one live league won week 1 from the away side and the other
  lost week 1 from the away side — exactly the pair a naive reading gets
  backwards for one of them.

  The URL still carries one parameter, the scoring period, and the week is
  derived from it. Two would be two things that can contradict each other. A
  day outside the season, or past the week being played, is clamped onto a
  real day of a real week rather than refused — a kept link should land
  somewhere, and a hand-edited one should not be able to address a read to
  somebody else's fixture.


- **A matchup sheet behind every scoreboard panel, and a Team tab that edits
  the lineup.** *"when we click on one of the matchup cards it should take us
  to this... second is a tab on the top called Team that just shows you your
  team... this is where you can drag and drop players and manually update and
  save"*.

  The panel on the overview answers *what is the score* and raises the
  question it cannot settle: *who is still to play*. Tapping it now opens the
  matchup — both lineups paired slot by slot, each player's fixture, tip-off
  and points for the day, and the day's subtotal for each side. Arrows either
  side of the date walk the days of the matchup week, and no further: the
  score in the header is the *week's*, and an arrow that stepped outside it
  would put a day's rows under a total that excludes them.

  The Team tab is the first place in this app where a person moves a player
  rather than the optimizer. Drag by the handle to swap two players; or tap
  the slot pill, which is a `<select>`, and pick. The pill is the control and
  the drag is a gesture that writes to it — so the page works with no script
  at all, under a keyboard, and under a screen reader, and what was dragged
  and what gets posted cannot disagree. Pointer events rather than HTML5
  drag-and-drop, because `dragstart` does not fire for touch and this page is
  read on a phone.

  Nothing is validated twice. `EspnWrite.set_lineup` already refuses a partial
  map, a phantom player, an ineligible slot, an over-full slot, a locked
  player being moved and a roster that would change size, and it is the path
  every scheduled lineup goes through; the page posts a target and reports
  what came back. A refusal is shown in ESPN's own words above a roster
  re-read from ESPN — not the form that was posted, because after a refusal
  those are different and the useful one is what ESPN has.

  Two findings from probing a live league on 2026-10-24, both of which the
  obvious implementation gets wrong:

  * `scoringPeriodId` on `mRoster` really does rewind the lineup — the same
    request for period 4 and period 5 returned different `lineupSlotId`s for
    the same players, and the played day came back with everyone locked. That
    is what makes the day arrows show the lineup that was actually fielded.
  * **a day's points are not `playerPoolEntry.appliedStatTotal`.** That field
    is populated for a period with no games played, carrying the *previous*
    day's number: on the probe it claimed 29.5 for a player who had not
    tipped off. The real figure is the `statSourceId: 0` /
    `statSplitTypeId: 5` row for that exact period, and **its absence is the
    answer** on a day not yet played. So `DayEntry.points` is `Optional`, a
    missing row renders as a dash and a `0.0` renders as `0.0` — one means he
    has not played, the other that he played and did nothing.

  New on the read side: `EspnRead.day_rosters`, which gets every team's
  lineup and that day's scores in one request (so a head-to-head costs the
  same as one team), and `EspnRead.pro_games_by_period`, which keeps the pro
  schedule whole instead of reducing it to a count — the opponent, the side
  and the tip-off were already in the payload `pro_team_scoring_periods`
  parses, and that is where "@DAL 5:00 pm" comes from.

  That payload now has one fetch between its three parsers
  (`EspnRead.pro_schedule_raw`). Adding a third reader of a several-megabyte,
  season-static response made it obvious that each of them had been fetching
  it: the first page load printed three identical request lines in the log.

  Saving is behind the same owner check as the approve buttons, which now say
  so in words that are true of both: other members of the household cannot
  bench anybody.


- **Every lineup move now says what the player has actually been scoring, and
  how much of it counted.** Prompted by a fair question: Walker Kessler scored
  51 on 2026-10-22 and was benched the next morning for Myles Turner, who had
  scored 24. The move was right — ESPN projects Turner at 28.4 against
  Kessler's 19.9 on this league's own scoring, and one game is not evidence —
  but the note on the page said only `value 19.9 vs lambda 0.0`, which gives
  the reader no way to tell a number that was weighed from one that was never
  read. It now says:

  ```
  value 19.9 vs lambda 0.0; last-15 51.5 from 1 game, too few to count
  (needs 10); p_play 0.94: ACTIVE (espn), ESPN projects 75/82 games
  ```

  The form figure is priced through the same value model as the projection, so
  the two are in the same units and comparable; a raw sum of ESPN's stat line
  would invite exactly the wrong comparison. It is stated *especially* when the
  weight is zero, which is the entire point — "too few to count" is a
  defensible answer, silence is not.

  No change to how lineups are chosen. `MIN_GAMES_FOR_FORM` stays at 10 and the
  blend weight stays `games/82` capped at half. Worth recording why, since the
  obvious reaction to the above is to lower the gate: with the gate removed
  entirely, one game of Kessler at 51.5 moves him from 19.9 to 20.3 against
  Turner's 28.4, so it would not have changed the lineup either. It takes about
  19 games of that split before form flips the call, which is roughly the right
  amount of evidence to ask for.

  `Projection` gained `form_mean` / `form_games` / `form_weight`, carried for
  the rationale and read by nothing in the optimizer, which still sees `mean`
  and nothing else. `recent_form` and `form_weight` were split out of
  `per_game_mean` so the gate has one definition and the page can report what
  was discounted without recomputing it.

- **This week's score sits at the top of the status page.** *"i need to know
  the current score, how many players have played, and how many will have been
  played after today."* One panel per league in the space beside the title,
  which was empty: both team names and logos, both totals with only the
  leader's in full-strength ink, each side's games against the weekly cap as
  ESPN itself counts them, the gap computed rather than left to the reader, and
  the projection for tonight — `12 of 30 games` now, `16 games after today`.

  **The score is read from ESPN on every page load**, not recovered from the
  last run. It was cached-only first, on the reasoning that the page had no
  credentials and no event loop — *"there has to be a way to live pull"* —
  and both halves of that were wrong. Every `League` carries its own
  authenticated reader, the handler is already `async`, and one
  `matchup_score()` call is a single HTTP request that returns both teams'
  points, names, abbreviations, logos and games-used. Leagues are read
  concurrently in a worker thread, since `EspnRead` wraps a synchronous
  `httpx.Client` and calling it on the loop would stall every other request
  behind it.

  The reads are memoised for 25 seconds, so holding down refresh — or the
  redirect that follows every Approve — is not a request apiece. A *failed*
  read is remembered for 10, because without that an expired cookie would make
  every single load sit through the 4-second timeout before showing anything.

  Nothing in that path may stop the page rendering: the Approve button is on
  it. A timeout, a dead cookie, a league with no matchup row this period — each
  degrades to the row the last run cached, and the panel header says which one
  you are looking at (`● LIVE` against `as of 9:54 am`). A score that *looks*
  live and is four hours old is the exact failure that label exists to prevent.

  Two fields still come from the run's cached row, because they are not on the
  score response and are not worth extra requests to rebuild: `games_cap`, a
  league setting fixed for the season, and `my_games_today`, which needs the
  lineup *and* the pro schedule — and the run that wrote it also wrote the
  lineup, so it stays true for the rest of the day.

  That cache is a new `scoreboards` table: one row per league, overwritten
  every run. The opposite of `gp_snapshots`, where the earliest reading is the
  only useful one. The row is written before the lineup is planned and before
  the two paths that abort, because those are exactly the mornings the owner
  most wants to open the page and see where he stands — and
  `_record_scoreboard` swallows every exception, since a cosmetic panel must
  never cost the run its lineup write.

  The run's own read costs nothing extra either. The names, abbreviations and
  logos all ride on the `mTeam` view the matchup read already asks for, and the
  opponent's `cumulativeScore.statBySlot` turns out to sit on the same matchup
  row as ours, so both sides' games-used come off the one response. The read
  was hoisted out of `_consider_addrops` into `run_league` rather than
  duplicated — `EspnRead._get` does no caching, so a second call would have
  been a second round trip on every run.

  "Games after today" counts under the same two conditions ESPN does: a
  starting slot, and an NBA team playing on this scoring period. It measures
  the lineup *about to be written* rather than the one ESPN currently has,
  because by the time anyone reads the page the write has happened. With no pro
  schedule to check against it says nothing rather than implying a quiet night,
  and a league with no `lineupSlotStatLimits` gets `12 games` rather than an
  invented denominator.

  A team logo is not an ESPN asset the way the player headshots are. A third of
  the live ones are `logoType: CUSTOM_VALID` — an arbitrary address a league
  member typed in, observed pointing at imgflip, twitter and ifunny. So the
  crest accepts `https://` only, refusing `data:`, `javascript:` and relative
  values outright rather than escaping them and hoping, and falls back to the
  team's initials. The document's own URL contains the ingress session token,
  so the page now sends `<meta name=referrer content=no-referrer>` and each
  image repeats it — which also closes a pre-existing leak, since the headshot
  requests had been handing that URL to `a.espncdn.com` all along.

- **Player names link to their ESPN pages.** *"i need to look deeper
  sometimes."* Every name on the page names a real player, and the numeric id
  needed to build the URL was already in hand at both render sites because the
  headshot needs it. New tab, because the page it leaves is the one with the
  Approve button on it, and `rel=noopener noreferrer` so the tab keeps no
  handle on the authenticated frontend it was opened from. The id goes through
  `int()` rather than `_e()` — the only safe thing to put in a URL is a number
  — and a row with no parseable id renders the bare name rather than a link to
  nowhere, which is what every move journalled before today looks like. The
  link keeps the colour of the name it replaced; twenty blue names down a
  column read as a navigation list, and the thing being looked at is the move.

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
