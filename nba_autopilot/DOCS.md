# NBA Fantasy Autopilot

Automated ESPN fantasy basketball lineup management, running as a Home Assistant
app. It evaluates your roster on a schedule, sets your lineup, manages IR slots,
and asks for approval — via a notification with action buttons — before doing
anything irreversible.

## Installation

1. In Home Assistant, go to **Settings → Apps → Repositories** (the three-dot
   menu in the top right → **Repositories**).
2. Add `https://github.com/ilituchy/ha-automations` and close the dialog.
3. Find **NBA Fantasy Autopilot** in the store and click **Install**.
4. Fill in the **Configuration** tab (see below) and **Save**.
5. Start the app, then open it from the sidebar to see the status UI.

Leave **Shadow mode** on for the first few days. The app will tell you exactly
what it *would* have done without touching your ESPN team.

## Configuration

| Option | Required | Default | Notes |
| --- | --- | --- | --- |
| `leagues` | yes | — | One entry per team you manage, see below |
| `espn_s2` | yes | — | Session cookie, see below |
| `espn_swid` | yes | — | Session cookie, see below |
| `ha_notify_device` | yes | — | e.g. `mobile_app_ians_iphone` |
| `ha_owner_user_id` | yes | — | Authorizes approval taps |
| `ha_todo_entity` | no | `todo.fantasy_autopilot` | Reserved; nothing is filed here yet |
| `timezone` | no | `America/Los_Angeles` | IANA name |
| `run_times` | no | `["09:00"]` | Local `HH:MM`, one or more |
| `autonomy_lineup` | no | `true` | Start/bench without asking |
| `autonomy_ir` | no | `true` | Move IR-eligible players without asking |
| `autonomy_add_drop` | no | `false` | Keep off; drops are irreversible |
| `never_drop` | no | `[]` | Full player names |
| `max_faab_bid` | no | `0` | 0–1000, whole dollars |
| `addrop_margin_pct` | no | `12` | How much better a pickup must be, on quality alone |
| `addrop_churn_floor` | no | `3` | Smallest points gain worth a permanent move |
| `addrop_max_moves` | no | `3` | 1–5. Swaps one run may offer, each approved separately |
| `anthropic_api_key` | no | — | Turns on the add/drop review, see below |
| `ai_judge_model` | no | `claude-sonnet-4-6` | Which model reviews swaps |
| `shadow_mode` | no | `true` | Compute + notify, never POST to ESPN |
| `log_level` | no | `info` | `trace`…`fatal` |

### A note on the autonomy switches

`autonomy_lineup` and `autonomy_ir` default to **on** because both are
reversible — if the app benches the wrong player you just move them back.

`autonomy_add_drop` defaults to **off** and should stay off unless you really
mean it. Dropping a player is irreversible, and in an active league someone else
can claim them within seconds. With it off you still get every suggested
add/drop as a notification with an **Approve** button, so nothing is lost except
the few seconds it takes you to tap.

### How add/drop decides

Two things have to be true before a swap is proposed, and they do different
jobs.

**It has to gain points this week.** A pickup is worth the points he would
actually score *for you*, which is not his per-game average. On a night when
more of your players have games than you have starting slots, a newcomer takes
the place of whoever would otherwise have started, so he is only worth the
difference. On a light night you cannot fill the lineup at all, a slot is free,
and his whole line is gain — which is why games played drives this more than
quality does. A 30-point player whose games all fall on your busiest nights can
be worth less than a 15-point player on a night you'd otherwise start nobody.
The games-played cap is priced in the same way: in a league where you are
nowhere near the cap an extra game is free upside, and in one where the cap is
binding, starting a mediocre pickup costs you a game you'd rather spend on
someone better later in the week.

`addrop_churn_floor` is the smallest gain worth making a permanent move for.
Every roster has some swap available that gains a fraction of a point; taking
all of them would spend a whole week's acquisitions on noise. The floor is
**waived automatically** when the move would by itself erase the deficit in
your current matchup — a 2-point gain is noise in general and is not noise when
you are losing by one.

**And it has to have a reason** — either of:

- *Quality*: the pickup is at least `addrop_margin_pct` better per game than the
  player going out.
- *Games*: the pickup simply has more games left this week that you could start
  him in. "Could start him in" and not "are on the NBA schedule": a player out
  for the season still has games on the schedule and none you can use.

Either reason is enough, and the points gain is required regardless — so a
reason can never license a move that loses you points, and the quality bar can
never block free ones.

A pickup is only proposed without a matching drop when there is genuinely
somewhere to put him. An **empty IR slot does not count** — ESPN will only let a
player into one if his own injury designation qualifies him, and someone worth
picking up never does. So a roster whose active slots are all taken gets a
paired add/drop or nothing, even when the IR slots are empty. For the same
reason the player going out has to be one holding an active slot: dropping
someone off IR frees an IR slot and leaves the pickup with nowhere to go, so
when the roster is full he is left out of the running, and the status page lists
him under that run's notes with the reason.

The matchup score is read every run and shown in the notes, but it is only ever
used to *waive* the floor, never to raise it. ESPN publishes no projected final
for basketball, so a lead you can see accumulated is not evidence the week is
safe — there is no opponent projection behind it.

### Several swaps in one run

One run can offer up to `addrop_max_moves` swaps — three by default. Each
arrives as its own notification with its own **Approve** button, and you can
take any of them, in any order, or none. There is no "approve all": a drop is
irreversible, so every one of them is a separate question.

The list runs **best first**, and each entry after the first is priced as if the
ones above it go through. That is not a presentation choice. Who should go out
depends on who is on your roster, so the "second-best swap" against your roster
as it stands today is usually the same pickup paired with the same man going out
— three notifications of which exactly one could ever be approved. Instead the
app works out the best swap, then re-prices against the roster *that* swap would
leave behind, and so on.

The consequence is that the numbers on the lower cards assume company, and they
say so: *"Priced as the second move of this run, assuming the swap above it is
approved."* Approving them out of order is fine — nothing breaks, you just get a
slightly different gain than the card quoted. Approving only the bottom one is
also fine, and is the case where the quoted number is least accurate.

Two things cap the real total below `addrop_max_moves`:

- **Your league's acquisition limit.** The app counts the swaps it has already
  offered this run against your remaining acquisitions for the matchup period,
  so it will not hand you four cards in a week that had two left.
- **Running out of worthwhile moves**, which is the usual reason. Only a player
  who can actually get into your starting lineup earns anything, and each
  pickup raises the bar for the next one — after two acquisitions the good slots
  are held by the acquisitions. The status page's notes for the run say where
  the list stopped and why.

A swap the reviewer blocks does **not** count against `addrop_max_moves`. The
option limits how many swaps you are *asked about*, and a vetoed swap is not a
swap — if it counted, a run whose three best candidates were all rightly refused
would stop with nothing to show you while a fourth the reviewer would have
approved sat unexamined. So the app keeps looking past a veto.

What stops it instead is the review budget: **one run shows the reviewer at most
five candidates per league.** Each review costs an API request and a few
seconds, so a league where everything deserves refusing must not walk the whole
free-agent pool to arrive at the same empty list. When a run ends this way the
notes say so — *"stopped after 5 candidate(s): the reviewer blocked 5 of them,
and 5 is the most one run will ask it about"* — which is worth telling apart
from "there was nothing to find", because the two call for opposite responses
from you.

**Five blocked out of five is a normal outcome, not a fault.** It is the
reviewer doing the thing it was added to do.

Nobody the app picks up in a run is offered as a drop later in the same run.

### The add/drop review

Everything above prices the rest of the current scoring week and nothing else.
That is deliberate and it is what makes "how many points does this gain" a
question with an answer — but it means a player who will not suit up again
before Sunday is worth exactly zero to it, whether he is out for the season or
out until Friday. Zero is the cheapest thing on your roster, so he is the first
one it offers to give away. This happened on a live roster: a first-overall
rookie sitting on IR with a short-term injury, proposed as the drop on two runs
in a row.

Put an Anthropic API key in `anthropic_api_key` and every proposed swap goes to
Claude before it reaches you. It is shown both players' projections, their games
left, how much of ESPN disagrees with the valuation (the share of leagues
rostering each of them, and where each was drafted), ESPN's own injury reporting
including the beat reporters' words, your current matchup score, and the
model's full reasoning. Then it is asked one question: does the statistical case
hold up against what you know that these numbers cannot encode.

It can only **block** a swap. It never picks a player, never proposes a move and
never ranks the pool, so a rejection is always checkable against the numbers it
was shown and an approval adds no claim of its own. When it blocks something it
says why, in its own words, on the status page under that run's notes. It can
also approve with a caveat, which is appended to the move's rationale rather
than replacing it — the computed numbers stay on the card.

Leave the key empty and nothing changes: swaps are proposed exactly as they were
before. Fill it in and a swap the reviewer could not be reached about is **not**
shown — a timed-out HTTP request is not permission to make an irreversible,
publicly attributed move unexamined. Those two cases read differently in the
notes, so a week of API outages does not look like a week of good judgment.

Every swap in a run is reviewed separately, and the two kinds of refusal behave
differently. A swap the reviewer **judges** badly is dropped and the run carries
on looking for others — neither of its two players is put forward again that
run, since the verdict is prose and there is no telling which of them it
objected to. A reviewer that **cannot be reached** stops the search entirely:
every remaining swap would be withheld for the same non-reason, so there is
nothing to be gained by asking again.

Cost is a handful of requests per run, billed to your own Anthropic account.
`ai_judge_model` overrides the model if you want a different speed/judgment
trade.

### After a swap goes through

A pickup who sits on your bench is worth nothing. The lineup for the day was
already written earlier in the run, against a roster that did not contain him —
so the moment an add/drop actually lands, the roster is re-read from ESPN, the
lineup is re-optimized against it, and the new one is written. You get two
notifications in order: the swap, then the lineup change that followed from it.

This happens straight away rather than on the next scheduled run, because the
next run can be up to a day later and tonight's games are not.

Three things mean the roster did not really change, and none of them rebalance:

- a **waiver claim**, which is pending until ESPN processes it overnight — he is
  not yours yet and the drop has not happened either, so the lineup already
  standing is the right one for the roster you actually have. It gets rebalanced
  on the run after the claim clears;
- **shadow mode**, where the swap never left the add-on;
- a write ESPN refused.

It respects `autonomy_lineup`. Approving a swap says who should be on your
roster, not who should be in your starting five — if you have lineup autonomy
off, the log says what it would have done and your lineup is left alone.

If the roster cannot be re-read, or no valid lineup can be computed afterwards,
you get a notification saying so. That case is worth looking at by hand: the
lineup currently set is one for a roster that no longer exists.

## Configuring your leagues

Open each league on `fantasy.espn.com` in a desktop browser, click through to
**your own team**, and look at the URL:

```
https://fantasy.espn.com/basketball/team?leagueId=123456789&teamId=4
                                                  ^^^^^^^^^        ^
                                                  league_id        team_id
```

One `leagues` entry per team. In YAML mode the Configuration tab looks like:

```yaml
leagues:
  - league_id: 123456789
    team_id: 4
    label: Dynasty
    keeper: true
  - league_id: 987654321
    team_id: 7
    label: Work league
```

`label` is optional and is only used in notification text. Set it anyway if you
manage more than one team — a prompt that reads *"drop Smith"* is unactionable
when two rosters are in play, whereas *"Dynasty: drop Smith"* is not.

`keeper` says the league keeps players between seasons, and defaults to `false`.
It is asked here rather than read from ESPN because ESPN's own keeper flag
describes a draft mechanic and reads identically in a dynasty league and a
redraft one — only you know which roster you are building for next year.

It changes one decision. In a redraft league a player out for the season is a
free roster spot, so he is the obvious drop. In a keeper league he is an asset,
and the app will not propose dropping him however little he does for you this
week; it goes after your genuinely weakest players instead — the bottom quarter
of the roster by per-game value — and says which ones those were on the approval
card. The default is `false` because the two mistakes are not equally bad:
treating a keeper league as redraft proposes a drop you can look at and reject,
while treating a redraft league as keeper quietly protects dead weight all
season and never mentions it.

**Each league is optimized on its own.** Category weights, the games-played
budget and the roster are league-local; nothing is pooled across leagues except
the ESPN cookie and the notification channel. A league whose roster fetch fails
is skipped for that run and the others still go ahead.

**Make sure `team_id` is the team you own, in that league.** The app verifies
ownership against your `SWID` before every write and refuses to proceed if it
doesn't match, because ESPN will otherwise honour an edit to a leaguemate's
roster with a cheerful `200 EXECUTED` and attribute it to you in the public
activity feed. For the same reason, listing one league twice is rejected
outright rather than deduplicated: one of the two `team_id` values would be a
team you don't own, and the app will not guess which.

## Extracting the `espn_s2` and `SWID` cookies

Private leagues require a logged-in session. ESPN has no API tokens, so the app
reuses your browser's session cookies.

> **Read this first.** These two cookies are equivalent to being logged into
> your ESPN account. Home Assistant stores app options in **plaintext** on disk
> and **includes them in backups**. Before you paste them in, turn on backup
> encryption: **Settings → System → Backups → ⋮ → Encryption**. Don't post your
> backups, your `.storage` folder, or full debug logs anywhere public. If a
> cookie leaks, log out of ESPN everywhere to invalidate it.

### Chrome / Edge / Brave

1. Log in at `fantasy.espn.com` and open your league.
2. Press <kbd>F12</kbd> to open DevTools.
3. Go to **Application** → in the left sidebar, **Storage → Cookies** →
   `https://fantasy.espn.com`.
4. Find the row named **`espn_s2`** and copy its **Value**. It's a long
   percent-encoded string (~300 characters, containing `%2F` etc.). Copy it
   exactly, with no leading or trailing spaces, and do not URL-decode it.
5. Find the row named **`SWID`** and copy its value. It looks like
   `{1A2B3C4D-5E6F-7890-ABCD-EF1234567890}` — **include the curly braces**.

### Firefox

1. <kbd>F12</kbd> → **Storage** tab → **Cookies** → `https://fantasy.espn.com`.
2. Copy the `espn_s2` and `SWID` values as above. Double-click a value to select
   the whole thing.

### Safari

1. Enable **Develop** menu: **Safari → Settings → Advanced → Show features for
   web developers**.
2. **Develop → Show Web Inspector** → **Storage** → **Cookies**.
3. Copy `espn_s2` and `SWID`.

These cookies expire after roughly a year, and sooner if you explicitly log out
of ESPN. When they expire the app will notify you that re-authentication is
needed; repeat the steps above and update the two options.

## Finding your `mobile_app_*` notification target

The Home Assistant companion app registers one notify action per device.

1. Make sure the companion app is installed and logged in on the phone.
2. Go to **Developer tools → Actions** (older versions: **Services**).
3. Type `notify.` in the **Action** picker and look at the list — you'll see
   entries like `notify.mobile_app_ians_iphone`.
4. Enter the part **after** `notify.` into `ha_notify_device`, e.g.
   `mobile_app_ians_iphone`.

Test it before relying on it: in **Developer tools → Actions**, pick your
`notify.mobile_app_…` action, switch to YAML mode and run

```yaml
action: notify.mobile_app_ians_iphone
data:
  title: Autopilot test
  message: If you can read this, the notify target is correct.
```

If no notification arrives, the slug is wrong or notifications are disabled for
the companion app on that device.

## Finding your Home Assistant user ID

Approval taps are checked against this ID so that only you can approve a move.

1. Go to **Settings → People** and click your own user.
2. Look at the browser's address bar:

   ```
   https://homeassistant.local:8123/config/person/edit/a1b2c3d4e5f67890abcdef1234567890
                                                       ^ this is your user ID
   ```

3. Copy that 32-character hex string into `ha_owner_user_id`.

Alternative: **Developer tools → Template** and render

```jinja
{{ user_id }}
```

while logged in as yourself.

Note this is the **user** ID, not the **person** entity id — it's hex, not
something like `person.ian`.

## The status page

Open the add-on and you get three views.

**Overview** is the one it starts on: this week's score for each league, then
anything waiting on you, then what the recent runs did. Everything is in
league columns — score at the top, approvals under it, runs under those — so a
column reads top to bottom as one league's story.

**Tapping a score** opens that league's matchup: both lineups slot by slot,
each player's opponent, tip-off time and points for the day, and the day's
subtotal for each side. A dash in the points column means ESPN has published
nothing for that player that day — he has not tipped off, or is not playing. A
`0.0` means he played and scored nothing.

Under the score card is the week, one chip per day, each showing what both
sides scored that day. Tap one to see that day's lineups. The arrows beside
the week number go back through **previous matchups** — a different opponent
each week, with its own final score and its own day-by-day — and stop at the
week being played, because ESPN's schedule carries every future fixture as a
real row with 0.0–0.0 on it and there is nothing to see there. A finished week
says `FINAL` and *Won by* or *Lost by* rather than *Ahead* or *Behind*.

Under each league's approval column is a dropdown of **the swaps it did not
put to you** — add/drop moves the app found, weighed and withheld, with both
players, why, and the reviewer's own words behind a *Show details* control.
Two things land there:

* *Reviewer said no* — the review gate judged the swap and blocked it;
* *Review unavailable* — the reviewer could not be reached, so the swap was
  withheld rather than shown. A degraded run rather than a verdict, and
  coloured differently for that reason.

Swaps **you** rejected and ones that expired unanswered are not here. They
were your decisions, and the run log has them.

It is closed by default and is not rendered at all for a league that withheld
nothing, so a quiet week costs you nothing. That is the point of it: "Nothing
waiting" on its own cannot tell a quiet week from one where the reviewer
turned down six swaps a day, and those call for opposite responses.

Runs from before 2026-10-26 did not record withheld swaps as rows — the only
account of them is the line the run wrote in its own notes — so those lines
are read back and listed too. A recovered entry has the two names and the
verdict but no headshots and no links, because that is all the note kept.

**Team** is your roster, and the one place in this app where *you* move
players rather than the optimizer. Drag a player by the handle on the left
onto another player to swap their slots, or tap the slot pill and pick one.
Nothing is sent to ESPN until you press **Save lineup**; the bar at the bottom
counts what you have changed.

A few things the page will not let you do, because ESPN would reject the whole
write and take the rest of the lineup down with it:

* move a player whose game has already started — his handle is greyed out and
  his slot pill is disabled;
* put a player in a slot he is not eligible for — the menu only offers slots
  he can take, and a drag onto an illegal row turns red;
* put two players in one slot — a swap is a swap, so the counts cannot change.

If ESPN refuses a save anyway, the reason appears in its own words above the
roster and nothing has been changed. The roster you are then looking at is
re-read from ESPN, not the form you posted — so what you see is what ESPN has.

Only the Home Assistant user in `ha_owner_user_id` can save a lineup or
approve a swap, which is the same check the notification buttons use.

**Player names open his card.** Not ESPN's — this app's own, rendered over
whatever page you were on, so reading up on a player does not cost you the
page with the Approve button on it. It shows:

* his season and trailing 7/15/30-day averages, with **fantasy points per
  game** as the first column;
* a **game log** of every game he has played, each with the fixture and what
  he scored in your league's scoring;
* **recent news** — Rotowire's note on each game or injury, with ESPN's "Spin"
  on it, which is the half of a start/sit this app does not work out for
  itself.

The numbers are ESPN's own applied totals under your league's scoring rules,
not recomputed here, so the card can be held up against ESPN's and agree with
it to the last decimal place.

It is a real page as well as a dialog (`player?league=…&id=…`), so a
middle-click opens it in a tab and it still works with JavaScript off. A name
whose league is not recorded — a run journaled before that column existed —
falls back to his page on espn.com, since a card is league-scoped and *"who
has him"* is half of what it answers.

This exists because **ESPN's own card has no URL**. It is client state: in
ESPN's pages a player's name is an `<a>` with no `href`, and the address bar
does not move when the card opens. `playercard`, `player/card` and `player`
all render "Page not found", and the old `games.espn.com/fba/playercard`
redirect drops the id on the way. The two endpoints behind it are
`kona_playercard` on the league (which returns the aggregate splits **and an
empty game log** unless the request carries
`filterStatsForTopScoringPeriodIds`) and the public, cookie-free
`site.api.espn.com/apis/fantasy/v3/games/fba/news/players`.

Lineups you set by hand are written through the same path the scheduled runs
use, so they are journalled the same way. The next scheduled run will
re-optimize and may move players back; if you want a change to stick, turn
`autonomy_lineup` off.

## How a run works

At each time in `run_times`, the app works through your `leagues` one at a time.
For each one:

1. It pulls that team's roster plus projections.
2. It computes an optimal lineup and a list of candidate moves.
3. Moves allowed by your autonomy settings are applied directly (unless
   `shadow_mode` is on).
4. Anything else becomes a notification with **Approve** / **Reject** buttons,
   naming the league, and is listed on the status page until it is decided.

A league that errors out is logged and skipped; the remaining leagues still run.
Results are written to the status UI and to SQLite at `/data/autopilot.db`,
which is private to the app and included in backups. Every run row and journal
row records which league it belonged to.

## How it scores a player

Projections come from ESPN's own season projection, unblended until there is
enough real history to blend (10 games), and are then dotted against your
league's `scoringItems`. Two things your leagues pay for are **not** in what
ESPN projects, and the app fills them in itself:

- **Offensive and defensive rebounds.** Both of your leagues score `OREB` at
  1.5 and `DREB` at 1.0 and put no value on total rebounds. ESPN projects only
  the total. The app splits it, using each player's own offensive-rebound share
  from his actual stats where he has any and a league-average 25% otherwise.
- **Double-doubles.** Worth 4.0, 8.0 and 15.0 in your leagues, and ESPN
  projects none of them. The app estimates the rate from the player's projected
  line, then shifts toward his own observed rate as games accumulate — a third
  of the way after three games, two thirds after twenty.

Before this was found, both gaps silently resolved to zero, so every rebound in
both leagues was worth nothing and rebounders were priced at a fraction of what
they returned. Replaying the first week of the season against the real box
scores, the fix is worth roughly **30–55 fantasy points a week per league**.

If a league scores *both* total rebounds and the offensive/defensive split, the
app refuses to value it rather than paying twice for the same rebound.

## Who he is playing

A projection is nudged up or down by the team the player faces that day. Two
numbers per NBA team: an overall multiplier, and an extra adjustment that
applies only to centre-eligible players.

Both were measured against **131,986 player-games** across five seasons of
ESPN's own per-game actuals. The write-up is in
[`docs/opponent-defence.md`](docs/opponent-defence.md); the short version:

- **Team defence is real.** Split a season's games into odd and even days, fit
  each half separately, and the two halves agree at a reliability of
  **0.75–0.90**. Last season predicts this season at **r ≈ 0.43–0.61**, which
  is what makes the number usable in October before the current season has any
  games in it.
- **Splitting it by all five positions is mostly noise** — reliability 0.25.
  The signal lives on one axis: *centres*. Rim protection is a structural team
  property. "Soft against shooting guards specifically" is not.
- **It is small.** The spread is about ±3.5%, up to ±9% on a centre at the
  extremes. Out of sample it is worth roughly **+1.2 fantasy points a week**,
  nearly all of it on the ~17% of days where it actually reorders the lineup.
  The rebound fix above was worth 30–55 a week; this is 25–45 times smaller.

So it is wired in because it is almost free once the archive exists, not
because it will decide a matchup. A player whose opponent cannot be resolved,
or whose team is not in the fitted table, gets exactly 1.0 — no adjustment in
either direction.

The fit is cached in the database for a week and refreshed from a **public**
ESPN endpoint that needs no cookie, so a dead `espn_s2` cannot take this with
it. If the fit cannot be obtained at all the run proceeds without any opponent
adjustment, which is what the app did before this existed.

What it deliberately does **not** do is notice that a player had a big game
against this opponent a fortnight ago. One game against one team is a single
observation against a per-game error of about 10 fantasy points, and the
apparent effect is almost always "this team is bad at guarding his position"
rather than anything about him. The first is measurable over hundreds of games
and is what the table above holds. The second is folklore.

## What it believed, and what happened

Every run writes one row per player to a `forecasts` table: the projection it
used, the availability it assumed, and whether it started him.

Nothing reads this yet. It is recorded because the numbers cannot be recovered
later — ESPN overwrites its season projection as the season moves, so the only
chance to capture what a lineup was set on is the morning it is set. The
actuals keep indefinitely: ESPN will replay any past day's lineups and box
scores on request. So "what happened" can be reconstructed whenever you like,
and "what did we think would happen" cannot.

This is what makes the model checkable against results rather than against
itself. The rebound bug above was found exactly this way, by hand, before the
table existed.

## Troubleshooting

**App won't start, log says a required option is not set.** One of the required
options is empty. Open the Configuration tab and fill it in. The default
`leagues` entry ships as `league_id: 0, team_id: 0` as a placeholder and is
rejected on purpose — replace both numbers with your own.

**App won't start, log says a league is configured twice.** You have two
`leagues` entries with the same `league_id`. Delete the one whose `team_id`
isn't your team. See *Configuring your leagues* above for why this isn't
silently merged.

**`401` or `403` from ESPN.** The cookies are wrong or expired. Re-extract both
`espn_s2` and `SWID`. The most common mistakes are omitting the braces around
`SWID`, URL-decoding `espn_s2`, or copying from a browser profile that isn't
logged in.

**Approval taps do nothing.** `ha_owner_user_id` doesn't match the user tapping.
Confirm it with the `{{ user_id }}` template above while logged in as yourself.

**Nothing happens at the scheduled time.** Check `timezone` is the IANA name
(`America/Los_Angeles`, not `PST`) and that `run_times` entries are 24-hour
`HH:MM`.

**It says it did something but ESPN is unchanged.** `shadow_mode` is still on.
That's the point of shadow mode — turn it off to go live.

**Swaps stopped being proposed after I added an API key.** Look at the run notes
on the status page. A swap withheld because the reviewer *could not be reached*
says so and quotes the error — an invalid key reads as `401`, an unknown model
name as `404`. That is the fail-closed path working: the key being present is
what tells the app swaps must be reviewed, so an unreachable reviewer blocks
rather than waves them through. Clear `anthropic_api_key` to go back to
unreviewed proposals.

**I approved an add and he is still on my bench.** Either the acquisition was a
waiver claim, which does not take effect until ESPN processes it overnight, or
`autonomy_lineup` is off, in which case the log says what it would have started
him over. Both cases sort themselves out on the next run — the first
automatically, the second once you move him or turn lineup autonomy on.

**I only ever get one add/drop suggestion.** That is usually the correct answer
rather than a setting: only a player who can get into your starting lineup gains
you anything, and each pickup raises the bar for the next one. Check the run's
notes on the status page — they say where the list stopped, whether it was the
pool, the acquisition limit, or the reviewer. `addrop_max_moves` is the ceiling,
not a target.

**I got no add/drop suggestions at all, and the notes say the reviewer blocked
every one.** Working as intended. The arithmetic proposes on this week's points
alone, which makes a star having a quiet week look like the cheapest man on your
roster to give away; the reviewer is the part that knows who he is. A run where
all five candidates were refused is a run where the app wanted to make five bad
trades and was stopped. Read the verdicts on the status page — if you disagree
with one, add that player to `never_drop` so neither layer raises him again.

**I got three suggestions and approved the last one, and the gain was smaller
than the card said.** Expected, and the card says so. Each entry after the first
is priced as if the ones above it go through; taking only the bottom one prices
it against a roster that includes the players the other two would have replaced.
Nothing is broken — the swap is still a gain, just a different one.

To get more detail, set `log_level` to `debug` and check the app's **Log** tab.
Cookie values stay redacted at every log level.
