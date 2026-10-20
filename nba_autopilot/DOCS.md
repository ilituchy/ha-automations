# NBA Fantasy Autopilot

Automated ESPN fantasy basketball lineup management, running as a Home Assistant
app. It evaluates your roster on a schedule, sets your lineup, manages IR slots,
and asks for approval — via a notification with action buttons — before doing
anything irreversible.

## Installation

1. In Home Assistant, go to **Settings → Apps → Repositories** (the three-dot
   menu in the top right → **Repositories**).
2. Add `https://github.com/ianlituchy/ha-nba-autopilot` and close the dialog.
3. Find **NBA Fantasy Autopilot** in the store and click **Install**.
4. Fill in the **Configuration** tab (see below) and **Save**.
5. Start the app, then open it from the sidebar to see the status UI.

Leave **Shadow mode** on for the first few days. The app will tell you exactly
what it *would* have done without touching your ESPN team.

## Configuration

| Option | Required | Default | Notes |
| --- | --- | --- | --- |
| `espn_league_id` | yes | — | Numeric league ID |
| `espn_team_id` | yes | — | Your team's ID within the league |
| `espn_s2` | yes | — | Session cookie, see below |
| `espn_swid` | yes | — | Session cookie, see below |
| `ha_notify_device` | yes | — | e.g. `mobile_app_ians_iphone` |
| `ha_owner_user_id` | yes | — | Authorizes approval taps |
| `ha_todo_entity` | no | `todo.fantasy_autopilot` | Where manual tasks are filed |
| `timezone` | no | `America/Los_Angeles` | IANA name |
| `run_times` | no | `["09:00"]` | Local `HH:MM`, one or more |
| `autonomy_lineup` | no | `true` | Start/bench without asking |
| `autonomy_ir` | no | `true` | Move IR-eligible players without asking |
| `autonomy_add_drop` | no | `false` | Keep off; drops are irreversible |
| `never_drop` | no | `[]` | Full player names |
| `max_faab_bid` | no | `0` | 0–1000, whole dollars |
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

## Finding your league and team IDs

Open your league on `fantasy.espn.com` in a desktop browser and look at the URL:

```
https://fantasy.espn.com/basketball/team?leagueId=123456789&teamId=4
                                                   ^^^^^^^^^        ^
                                                   league ID        team ID
```

`espn_league_id` is `123456789` and `espn_team_id` is `4` in that example.

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

1. At each time in `run_times`, the app pulls your roster plus projections.
2. It computes an optimal lineup and a list of candidate moves.
3. Moves allowed by your autonomy settings are applied directly (unless
   `shadow_mode` is on).
4. Anything else becomes a notification with **Approve** / **Dismiss** buttons,
   and a task on `ha_todo_entity`.
5. Results are written to the status UI and to SQLite at `/data/autopilot.db`,
   which is private to the app and included in backups.

## Troubleshooting

**App won't start, log says a required option is not set.** One of the six
required options is empty. Open the Configuration tab and fill it in.

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

To get more detail, set `log_level` to `debug` and check the app's **Log** tab.
Cookie values stay redacted at every log level.
