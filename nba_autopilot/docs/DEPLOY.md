# Deploying to the Home Assistant add-on

The add-on is installed from this git repository as a local add-on store entry.
The Supervisor caches a copy of the repository, so there are **three** steps and
the order matters.

```bash
git push origin main              # 1. the Supervisor builds from the remote
# 2. POST /store/reload           --  re-pull the repository into the cache
# 3. POST /addons/<slug>/rebuild  --  build the image from the cache and restart
```

## The trap: a failed reload is a silent stale deploy

`POST /store/reload` fails intermittently, returning
`{"code": "unknown_error", "message": ""}` with no explanation. When it does,
the Supervisor's cached copy of the repository **stays at the previous commit**,
and the rebuild that follows builds *that* — happily, and reporting the same
`unknown_error` it reports on success.

So all three signals lie at once:

* the reload says it failed, which is easy to read as "nothing happened";
* the rebuild says it failed, which it always does, even when it worked;
* the add-on stops and restarts exactly as it does on a real deploy, so the
  logs show every sign of a successful deployment.

The only way to tell is to look at what the running code *does*.

Observed on 2026-11-02: a cap-pricing fix was pushed, reloaded (failed),
rebuilt, and the add-on restarted cleanly. The next run still priced a game off
the cap at **33.21** where the new code gives **0.00** — the old image, one
commit behind, running and reporting success. The commit before it had deployed
correctly minutes earlier, so the difference was entirely the reload.

## And a reload that returns success is still not enough

A reload that returns `True` has *started* the pull, not finished it. The same
fix failed to deploy a second time after a reload that succeeded, because the
rebuild went out two seconds later and read a cache still being written.

So: retry until it succeeds, **then wait**, then rebuild.

```python
for attempt in range(5):
    if (await call("/store/reload", "post"))["success"]:
        break
    await asyncio.sleep(10)
else:
    raise SystemExit("store reload never succeeded -- do not rebuild on a stale cache")
await asyncio.sleep(30)                     # let the pull land
await call(f"/addons/{SLUG}/rebuild", "post")
```

Then **check a number only the new code can produce**. Pick it before
deploying, from whatever the change is supposed to alter. `version` cannot
serve: the add-on pins `0.1.0`, so `version_latest` matches it and
`update_available` is always `false` however far behind the image is.

The standing canary is the `games cap:` line `pipeline.plan_lineup` logs on
every run that prices the cap. It carries `lambda`, the startable player-game
count and the scheduled one, so one glance says both which image is running and
whether the cap is really binding:

```
games cap: 44 cap, 0 used, 7 day(s) left, 57 startable player-game(s)
           of 91 scheduled, lambda 0.00, utilization 1.04
```

If a change does not alter that line, add one that does, deploy, and confirm it
appears before trusting anything else about the run.


## Triggering a run without waiting for 09:00

There is no run-now endpoint. Runs fire only at the `run_times` in the add-on
options, so a one-off is an options write plus a restart:

1. `GET /addons/<slug>/info`, take `options`, append a time a few minutes out.
2. `POST /addons/<slug>/options` with the **whole** object — the endpoint
   replaces it, so a partial write silently drops every other setting.
3. `POST /addons/<slug>/restart`, so the scheduler re-registers.
4. Wait for `Job "run@HH:MM" ... executed successfully` in the logs. Matching on
   `run@HH:MM` alone also matches the *registration* line and reports success
   before the run has happened.
5. Put `run_times` back and restart again.

**That options object carries `espn_s2`, `espn_swid` and `anthropic_api_key`.**
Never print it, never log it, never paste it anywhere. The add-on's own
`config` log line redacts them (`<set:292 chars>`, `<set>`); a hand-rolled
script does not.

Each run re-proposes any pending add/drop, so a run triggered this way adds a
card the user may already have. Proposals carry a six-hour TTL and expire on
their own, but two approved activations drop two players to open one roster
spot — so say which card to tap.
