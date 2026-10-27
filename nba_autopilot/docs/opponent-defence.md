# Opponent defence: what was measured, and what it is worth

Every number in `rootfs/app/sources/defense.py` came from here. This file is
the working, so that a future reader can tell which constants are measurements
and which are judgement calls — and can re-run the measurement when the
archive grows.

## The archive

ESPN's fantasy API serves a **public, unauthenticated** per-game actuals feed:

```
GET lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/<season>/players
    ?view=kona_player_info
    X-Fantasy-Filter: {"players":{"limit":3000,"offset":0}}
```

Without the filter header the response defaults to **50 players**. With it,
~1,080 — every player ESPN carries that season — at about 25 MB.

Each player's `stats` array contains one row per game played, identified by
`statSourceId == 0` (actual, not projection) and `statSplitTypeId == 5` (one
scoring period). Each row carries:

| field | why it matters |
|---|---|
| `seasonId` | **the response mixes two seasons** — see below |
| `scoringPeriodId` | the day, joinable to `proTeamSchedules_wl` |
| `proTeamId` | the team he played that game *for*, so trades resolve |
| `externalId` | the real ESPN game id |
| `stats` | stat ids 0–44, including `4`/`5` (OREB/DREB) and `37`/`38`/`39` |

Two traps, both of which produced wrong numbers before being caught:

1. **`seasons/<Y>/players` returns season Y *and* season Y−1.** `seasons/2027`
   on 2026-10-26 returned 1,033 rows of the live season and all 26,460 of
   2025-26. Keying rows by the season you requested rather than by each row's
   own `seasonId` double-counts one season and resolves the other against the
   wrong calendar — 13,128 games got the wrong opponent that way.
2. **`proTeamSchedules_wl` contains no season id at all.** Its only top-level
   keys are `display` and `settings`; there is no `seasonId` on the response,
   on `settings`, or on any game. The season has to be supplied by the caller.

Coverage pulled 2026-10-26: **131,986 player-games**, five complete seasons
(2021-22 … 2025-26) plus the live one, every opponent resolved.

`stats.nba.com` was tried first and is unusable from this host: the connection
never completes. `nba.com` and `cdn.nba.com` both return 403 to bot detection.
ESPN's `site.api.espn.com` box scores work and go back years, but the cheap
per-player gamelog endpoint carries only `totalRebounds` — no offensive and
defensive split — so it cannot price a league that scores the two separately.

## Finding 1 — the bonus counters are cumulative

Of **149** triple-doubles in 2024-25, **149** also carry stat 37. ESPN counts a
triple-double as a double-double as well and pays both coefficients.

`modelled_bonus_rates` originally read `P(exactly n)` off its convolution,
which dropped the 4.0 on every triple-double. The subtler half: split `00`
reports the observed rates cumulatively too, so the model and the observed
rate it gets blended with were measuring different quantities. Fixed to a tail
sum, `P(at least n)`.

## Finding 2 — the 25% offensive-rebound default is right

`OREB_SHARE_DEFAULT` was a guess. League-wide actual share over 26,164
player-games: **0.2519**.

## Finding 3 — the double-double model is sound, and the archive is not a
## better prior for it

Calibration against 1,774 player-seasons of ≥40 games, predicted vs actual
double-double rate:

| predicted band | n | predicted | actual |
|---|---|---|---|
| 0.00–0.05 | 1210 | 0.007 | 0.017 |
| 0.05–0.15 | 247 | 0.093 | 0.096 |
| 0.15–0.30 | 155 | 0.210 | 0.188 |
| 0.30–0.50 | 87 | 0.394 | 0.379 |
| 0.50–0.75 | 62 | 0.596 | 0.582 |
| 0.75–1.00 | 13 | 0.818 | 0.837 |
| **overall** | **1774** | **0.082** | **0.086** |

Worth −0.02 fantasy points a game league-wide. Within noise.

The obvious idea — use the player's *actual* rate from last season instead of
modelling it — is **worse**, and not marginally:

| predictor | MAE | RMSE |
|---|---|---|
| the independence model | **0.0228** | **0.0364** |
| last season's actual rate | 0.0483 | 0.0827 |
| best blend (20% archive) | 0.0226 | 0.0362 |

The model wins because it reads *this* season's projected line, which already
reflects a role change, a trade or an extra six minutes a night. Last season's
rate cannot see any of that. The 20% blend is a 0.5% RMSE gain and was not
taken — it is not worth a moving part.

The model does under-rate centres (bias −0.0240, about −0.10 fp/game) and
over-rate high-usage guards, which is the positive correlation between points
and rebounds that `modelled_bonus_rates` already documents as a known flaw.
Too small to chase.

## Finding 4 — team defence is real; the five-position split is not

Method: fantasy points per game under the league's own coefficients, divided by
a **leave-one-out** baseline (the player's average over his *other* games that
season). Players need ≥20 games and a baseline ≥10 fp/g. Reliability by
splitting each season into odd and even scoring periods, fitting both halves
independently, correlating the cells, Spearman-Brown corrected.

| | split-half reliability | year over year |
|---|---|---|
| **team defence** (30 cells) | 0.75 – 0.90 (mean **0.84**) | **0.43 – 0.62** |
| 5-way PG/SG/SF/PF/C (150 cells) | mean 0.25 | 0.15 |
| 3-way G/F/C | mean 0.46 | 0.23 |
| 2-way big/small | mean 0.50 | 0.25 |
| **2-way C / not-C** | mean **0.59** | **0.35** |

The five-position split looks like noise because it mostly is — but every one
of the eight most extreme cells was a centre. Collapsing to the single
centre axis more than doubles reliability. Rim protection is a structural team
property; "soft against shooting guards specifically" is not.

Re-measured with centre defined as **eligibility at the `C` slot**, which is
the only test available at run time: team 0.838, centre interaction 0.558.
Almost free, so the fit uses the definition the application can actually read
back.

Effect size, pooled and shrunk: team multiplier sd **0.034 – 0.039**, range
0.915 – 1.092. Centre adjustment range −6.6% to +5.4%. The spread depends
slightly on which definition of centre and how many seasons: `tools/`'s
reproducible four-season, C-slot-eligibility fit gives sd 0.0394, the
five-season `defaultPositionId` fit gives 0.034.

## Finding 5 — what it is worth, which is not much

Out of sample, fitting on one half of each season and testing on the other:

| | MAE | RMSE |
|---|---|---|
| no adjustment | 9.818 | 12.548 |
| team only | 9.795 | 12.522 |
| team + centre | 9.793 | 12.519 |

Fit on season Y and test on Y+1 — the realistic October case — is weaker
still: between −0.21% and +0.06% on MAE, i.e. sometimes worse.

Lineup simulation, 40,000 trials, choosing 10 of a random 13 from one day's
players, fit on the first half and tested on the second:

| variant | fp/day (95% CI) | fp/week |
|---|---|---|
| all matchups | **+0.178** [+0.111, +0.244] | **+1.24** |
| **only days it changed the lineup** | **+1.026** [+0.643, +1.408] | +7.18 |

Those are `tools/measure_defence.py`'s numbers — four seasons, centre defined
as C-slot eligibility, which is what production uses and what a reader will
reproduce. The earlier five-season `defaultPositionId` run of the same
simulation gave +0.096 fp/day (+0.67 a week) and +0.715 on changed days; the
C-slot definition scores better because it is a broader and far
better-populated notion of "big man" than ESPN's single position field.

Either way: positive, significant, and small. The adjustment averages **0.81
fp against a mean per-game error of 9.8** — a correction of a few percent
fighting 38% noise. It changes the lineup on about **17%** of days, and that is
where all of the value is.

An earlier run of the same simulation at 400 trials returned −0.155 ± 0.272
and the sign flipped once it was properly powered. Worth recording: at that
sample size the simulation could not distinguish a small gain from a small
loss, and reporting the point estimate would have been reporting noise.

For scale, in the same units: pricing rebounds at all — the bug fixed the week
before — was worth **+31 and +55.5 points a week**. This is roughly **25 to 45
times smaller**, and it is wired in because it is nearly free once the archive
exists, not because it will decide a matchup.

## Reproducing

`tools/archive.py` pulls the seasons and builds the SQLite table;
`tools/measure_defence.py` runs the reliability, year-over-year and lineup
simulations above. Neither is imported by the add-on and neither needs
credentials.
