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

Cost is a handful of requests per run, billed to your own Anthropic account.
`ai_judge_model` overrides the model if you want a different speed/judgment
trade.

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

To get more detail, set `log_level` to `debug` and check the app's **Log** tab.
Cookie values stay redacted at every log level.
