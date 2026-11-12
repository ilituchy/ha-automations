# dynasty_advisor

Trade and add/drop recommendations for a Sleeper dynasty football league.
The successor to `nba_autopilot`; see
[`../nba_autopilot/docs/PORTING-TO-SLEEPER-NFL.md`](../nba_autopilot/docs/PORTING-TO-SLEEPER-NFL.md).

**It recommends, it never acts.** Sleeper's public API is read-only (verified
2026-11-11: the docs say so, and every documented endpoint is a GET), so every
output is something a human carries out in the Sleeper app.

## Scope — the owner's words

> "i dont need help setting lineups. i more so need add drop recs, and ideally,
> the best part, trade recs, we should be looking for ideal trade targets. so
> make sure you see my draft picks i can trade in the dynasty league."

> "analyze each users previous trade history, so we understand who is likely
> to accept and if so, we can get a better deal. might be more aggressive and
> unfair with people who accept more trades."

> "make sure to target losing teams too because they will sell for a rebuild"

Where each lives:

| Requirement | Module |
|---|---|
| Tradable picks, exact ownership | `league.py` — defaults + `traded_picks`, self-checked against every completed draft |
| Trade targets | `trades.py` — exhaustive 1-2 for 1-2 search per team |
| Trade history -> who accepts | `history.py` (all seasons via `previous_league_id`), `profiles.py` |
| More aggressive with frequent accepters | `ManagerProfile.edge` sets how far the opening offer leans our way |
| Losing teams sell | `teams.py` modes (seller / bubble / middle / contender); sellers judge trades as rebuilders |
| Add/drop | `adddrop.py` — IR moves first, open spots, then swaps that clear a margin |
| Player cards (tap a name) | `cards.py` — bio, values, schedule + projections, stats and game log in league scoring, news |

Lineup optimisation exists only as a measuring tool (`lineup.py`), never as advice.

## Running

```bash
python3 -m advisor            # reads ~/.config/dynasty_advisor/config.json
python3 -m advisor --strategy rebuild -v
python3 -m advisor --offline  # cached responses only
```

Config (kept **outside** this public repo — league ids identify a real team):

```json
{"username": "<sleeper username>", "league_id": "<league id>", "strategy": "auto"}
```

In Home Assistant the app runs as an ingress page (`advisor/web.py`) with a
**Run now** button, plus scheduled runs that push a summary to your phone
(`advisor/notify.py`; see DOCS.md). Deploying follows
`../nba_autopilot/docs/DEPLOY.md`; the canary is the `schedule:` log line.

`strategy`: `auto` (default) follows your team's own mode — contender ->
`contend`, seller -> `rebuild`, else `balanced`.

Output goes to `~/.local/share/dynasty_advisor/`: `reports/YYYY-MM-DD-HHMMSS.md`
(plus a `.json` view for the page), `players.json` (the player-card index), a
JSON journal per run in `runs/` (the inputs behind every idea), a daily
FantasyCalc snapshot in `values/`, and the HTTP cache. Standard library only;
Python 3.9+.

```bash
cd tests && python3 -m unittest
```

## How a trade idea is scored

All values are FantasyCalc units, **over replacement** (minus the median value
of the best `n_teams` free agents), so two bench players never add up to one
starter and 4th-round picks count for roughly nothing.

* **mine** = strategy-weighted, **age-adjusted** dynasty change + change in my
  best rest-of-season lineup. The age curve (`assets.age_curve`: peak QB 30,
  RB 25, WR 27, TE 28; +4%/yr younger up to +20%, -6 to -12%/yr older up to
  -70%) applies in every strategy, scaled 0.6 / 0.9 / 1.3 for contend /
  balanced / rebuild. Contend weights dynasty 0.8 and lineup 0.5.
* **Dynasty guardrails** (`TradeSearch.acceptable_for_me`), every strategy:
  no idea may lose more than 5% of the package in age-adjusted dynasty value;
  a *core* player (young for his position, FantasyCalc value rising) is
  offered only for a return with no veterans and no dynasty loss; players in
  the `untouchable` option are never offered.
* **theirs** = the same rule from their seat: a seller judges as a rebuilder
  (veterans down, youth/picks up, ignores its lineup); a contender judges on
  its lineup.
* **opening** = best package for me that the manager plausibly accepts given
  their history; **ceiling** = the most I can give and still come out ahead.
* Packages are depth-discounted (1.0, 0.8, 0.65, ...): one great player beats
  two good ones.

## Known limits

* **Rejected offers are invisible.** Only completed trades come back from the
  API, so manager "softness" is built from accepts, never from a rate.
* **Past trades are judged at today's values** (FantasyCalc has no history
  endpoint), so "overpaid" is hindsight and only nudges softness. Daily
  snapshots started 2026-11-11 will make this exact for future trades.
* FantasyCalc's endpoint is the one its website uses, not a published API. The
  loader refuses to run on an empty or tiny response instead of silently
  valuing everyone at zero.
* K and DEF are unvalued and excluded from trades and add/drop.
* No LLM reviewer yet (porting doc §4: a model that can only veto).
* `practice_participation` was empty for every player on a Wednesday — not used
  until it is seen populated.
