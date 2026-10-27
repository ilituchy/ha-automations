"""Approval state machine and write journal. SQLite at ``/data/autopilot.db``.

Two concerns, one file because they share a transaction boundary:

* **Proposals** -- what we want to do and whether a human has blessed it.
* **Journal** -- what we actually sent to ESPN and what came back.

``PROPOSED -> APPROVED -> EXECUTING -> DONE | FAILED | EXPIRED | REJECTED``

The one transition that matters is ``APPROVED -> EXECUTING``. It is a single
guarded ``UPDATE`` and ``rowcount == 0`` means someone else already claimed it.
That is the entire defence against a double-tap on a push notification, and it
has to be done in the database rather than in Python because the notification
handler and the scheduler are different coroutines racing on the same row.

Approval never carries over: a proposal is scoped to a ``run_id`` and expires.
Yesterday's blessing cannot authorize today's move.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
EXECUTING = "EXECUTING"
DONE = "DONE"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"
#: The app considered this swap and declined it itself -- the reviewer blocked
#: it, or could not be reached and it was withheld rather than shown.
#:
#: A row rather than a line in the run notes because it *is* a decision about a
#: named pair of players, and the question it answers -- "it says nothing is
#: waiting, so what did it look at?" -- is asked of the approval queue and not
#: of a log. Terminal from birth: ``decide`` is guarded on ``PROPOSED`` and
#: ``pending`` selects it, so a row in this state can never be approved,
#: executed or swept, which is exactly the guarantee a record of a refusal
#: needs.
BLOCKED = "BLOCKED"

TERMINAL = frozenset({DONE, FAILED, EXPIRED, REJECTED, BLOCKED})

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    league_id         INTEGER,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    scoring_period    INTEGER,
    shadow_mode       INTEGER NOT NULL DEFAULT 1,
    sources_used      TEXT NOT NULL DEFAULT '{}',
    sources_missing   TEXT NOT NULL DEFAULT '{}',
    shadow_prices     TEXT NOT NULL DEFAULT '{}',
    category_weights  TEXT NOT NULL DEFAULT '{}',
    notes             TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    action_id     TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    state         TEXT NOT NULL,
    payload       TEXT NOT NULL,
    rationale     TEXT NOT NULL DEFAULT '',
    value_delta   REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT,
    finished_at   TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state);
CREATE INDEX IF NOT EXISTS idx_proposals_run   ON proposals(run_id);

CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    league_id       INTEGER,
    proposal_id     INTEGER REFERENCES proposals(id),
    kind            TEXT NOT NULL,
    request         TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    http_status     INTEGER,
    transaction_id  TEXT,
    proposed_date   INTEGER,
    response        TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_journal_outcome ON journal(outcome);

-- Games-played baselines, one row per (team, matchup period).
--
-- ESPN publishes season-to-date team games played (``valuesByStat["42"]``) but
-- the per-matchup-period figure the cap is actually enforced against
-- (``cumulativeScore.statBySlot``) came back ``null`` on every matchup row of
-- both live leagues. So the weekly number is reconstructed as a difference of
-- two season-to-date readings, which requires remembering the one taken at the
-- start of the week -- there is no endpoint that will tell us later.
--
-- ``is_baseline`` records whether the reading was actually taken on the first
-- scoring period of the week. A row written mid-week is kept (it is still
-- evidence, and it is what the next week's delta is *not* allowed to use) but
-- flagged, because subtracting a Thursday reading from a Sunday one
-- under-counts the week and under-counting a hard cap is how you arrive at
-- Sunday with no legal lineup.
CREATE TABLE IF NOT EXISTS gp_snapshots (
    league_id       INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,
    matchup_period  INTEGER NOT NULL,
    scoring_period  INTEGER NOT NULL,
    season_gp       INTEGER NOT NULL,
    is_baseline     INTEGER NOT NULL DEFAULT 0,
    taken_at        TEXT    NOT NULL,
    PRIMARY KEY (league_id, team_id, matchup_period)
);

-- This week's head-to-head as of the last run, for the status page.
--
-- One row per league, overwritten every run -- the exact opposite of
-- ``gp_snapshots``, where the earliest reading is the only useful one. Here
-- the only useful reading is the latest: it is a live score, and yesterday's
-- is not evidence of anything.
--
-- Written by every run and read by the status page as its floor, not as its
-- normal path: the page asks ESPN itself on every load and lands here only
-- when that read fails. ``updated_at`` is therefore load-bearing and is shown
-- -- a score from this morning presented as live is worse than no score -- and
-- two of the columns could not be live even in principle, ``games_cap`` being
-- a season-long league setting and ``my_games_today`` depending on the lineup
-- the run that wrote it had just set.
CREATE TABLE IF NOT EXISTS scoreboards (
    league_id       INTEGER PRIMARY KEY,
    run_id          TEXT,
    updated_at      TEXT    NOT NULL,
    matchup_period  INTEGER,
    scoring_period  INTEGER,
    my_team_id      INTEGER,
    my_name         TEXT    NOT NULL DEFAULT '',
    my_abbrev       TEXT    NOT NULL DEFAULT '',
    my_logo         TEXT    NOT NULL DEFAULT '',
    my_points       REAL,
    opp_team_id     INTEGER,
    opp_name        TEXT    NOT NULL DEFAULT '',
    opp_abbrev      TEXT    NOT NULL DEFAULT '',
    opp_logo        TEXT    NOT NULL DEFAULT '',
    opp_points      REAL,
    my_games_used   INTEGER,
    opp_games_used  INTEGER,
    my_games_today  INTEGER,
    games_cap       INTEGER
);

-- What the model believed, one row per player per run.
--
-- This table exists because of an asymmetry: **actuals are permanent and
-- forecasts are perishable.** ESPN will hand back any past day's lineups and
-- scores on request -- ``scoringPeriodId`` rewinds both -- so the question
-- "what happened" can be answered at any time, for any day, forever. But the
-- projection is split ``10``, which ESPN *overwrites* as the season moves.
-- Nothing anywhere records what the number was on the morning a lineup was
-- set, and once the day is gone that reading cannot be recovered from any
-- endpoint. Every run that does not write here is a day of evidence lost for
-- good, which is why this is a write the run does unconditionally rather than
-- a feature of a reporting page.
--
-- ``projected`` is ``value.conditional_value`` -- projected fantasy points
-- *given that he suits up*, before the cap shadow price is applied. That is
-- deliberately the same conditioning as the actual it will be compared
-- against: a player with a score played, so his actual is conditional too.
-- Multiplying by ``p_play`` here would compare an expectation against a
-- realization and read as a permanent negative bias. ``p_play`` is stored
-- beside it so the availability model can be scored separately, which is a
-- different question with a different answer.
--
-- ``lam`` is on ``runs`` and not repeated here: it is one number per run.
--
-- Keyed on the run and not on the day, so a second run in one day inserts
-- rather than overwrites. Reconciliation takes the newest run per period --
-- an earlier run's lineup was superseded and is not what the day was played
-- with, but it is still the honest record of what was believed at that hour.
CREATE TABLE IF NOT EXISTS forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL REFERENCES runs(run_id),
    league_id       INTEGER NOT NULL,
    scoring_period  INTEGER NOT NULL,
    player_id       INTEGER NOT NULL,
    name            TEXT    NOT NULL DEFAULT '',
    slot            INTEGER,
    started         INTEGER NOT NULL DEFAULT 0,
    projected       REAL,
    p_play          REAL,
    status          TEXT    NOT NULL DEFAULT '',
    status_source   TEXT    NOT NULL DEFAULT '',
    taken_at        TEXT    NOT NULL,
    UNIQUE (run_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_day
    ON forecasts(league_id, scoring_period);

-- The fitted opponent-defence table, cached because fitting it means a 25 MB
-- public fetch and the answer moves over weeks, not hours. Persisted rather
-- than held in memory because the add-on's normal mode is one process per run:
-- an in-process TTL would refetch on every single wake-up and never once get
-- a cache hit.
--
-- ``scope`` is a fingerprint of the league's scoring coefficients, not a
-- league id. The fit is a function of those coefficients and nothing else, so
-- two leagues that score alike share one row and one fetch, and a league that
-- changes its scoring mid-season gets a new scope instead of silently reading
-- a table fitted against the old points.
CREATE TABLE IF NOT EXISTS defense (
    scope       TEXT    PRIMARY KEY,
    seasons     TEXT    NOT NULL DEFAULT '',
    games       INTEGER NOT NULL DEFAULT 0,
    table_json  TEXT    NOT NULL,
    fitted_at   TEXT    NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Proposal:
    id: int
    run_id: str
    action_id: str
    kind: str
    state: str
    payload: Mapping[str, Any]
    rationale: str
    value_delta: float
    expires_at: datetime

    @property
    def requires_approval(self) -> bool:
        return self.kind in ("ADD", "DROP", "ADD_DROP")


class Store:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.environ.get("AUTOPILOT_DB", "/data/autopilot.db")
        parent = os.path.dirname(self.path)
        if parent and parent != ":memory:":
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # WAL so the ingress status page can read while a run writes.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add columns that ``CREATE TABLE IF NOT EXISTS`` cannot add.

        An existing database keeps its original shape forever under that DDL,
        so a new column is silently absent and every write naming it fails at
        runtime instead of at boot. Each step is additive and nullable, which
        is what makes replaying them unconditionally safe.
        """
        wanted = {
            "runs": [("league_id", "INTEGER")],
            "journal": [("league_id", "INTEGER")],
        }
        for table, columns in wanted.items():
            have = {
                r["name"]
                for r in self.db.execute("PRAGMA table_info(%s)" % table)
            }
            for name, decl in columns:
                if name not in have:
                    log.info("migrating: adding %s.%s", table, name)
                    self.db.execute(
                        "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl)
                    )

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # -- runs --------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        scoring_period: int,
        shadow_mode: bool,
        league_id: Optional[int] = None,
    ) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO runs (run_id, league_id, started_at, scoring_period, "
                "shadow_mode) VALUES (?,?,?,?,?)",
                (
                    run_id,
                    None if league_id is None else int(league_id),
                    _now(),
                    int(scoring_period),
                    1 if shadow_mode else 0,
                ),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        sources_used: Mapping[str, str],
        sources_missing: Mapping[str, str],
        shadow_prices: Mapping[int, float],
        category_weights: Mapping[int, float],
        notes: Sequence[str] = (),
    ) -> None:
        """Persist the duals, not just the decisions.

        When a move looks wrong the question is always "what did it think the
        margin was". Storing ``lambda_s`` and ``w_k`` is what makes that
        answerable a week later instead of unreproducible.
        """
        with self._lock, self.db:
            self.db.execute(
                "UPDATE runs SET finished_at=?, sources_used=?, sources_missing=?, "
                "shadow_prices=?, category_weights=?, notes=? WHERE run_id=?",
                (
                    _now(),
                    json.dumps(dict(sources_used)),
                    json.dumps(dict(sources_missing)),
                    json.dumps({str(k): v for k, v in shadow_prices.items()}),
                    json.dumps({str(k): v for k, v in category_weights.items()}),
                    json.dumps(list(notes)),
                    run_id,
                ),
            )

    # -- games-played baselines --------------------------------------------

    def record_gp_snapshot(
        self,
        *,
        league_id: int,
        team_id: int,
        matchup_period: int,
        scoring_period: int,
        season_gp: int,
        is_baseline: bool,
    ) -> None:
        """Remember season-to-date games played for a matchup period.

        First write for a ``(league, team, matchup period)`` wins and later
        calls are no-ops. That is deliberate and it is the whole point of the
        table: the useful reading is the *earliest* one in the week, and a run
        later the same week must not be allowed to overwrite it with a number
        that already includes games we are trying to count.
        """
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO gp_snapshots (league_id, team_id, "
                "matchup_period, scoring_period, season_gp, is_baseline, taken_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    int(league_id),
                    int(team_id),
                    int(matchup_period),
                    int(scoring_period),
                    int(season_gp),
                    1 if is_baseline else 0,
                    _now(),
                ),
            )

    def gp_snapshot(
        self, league_id: int, team_id: int, matchup_period: int
    ) -> Optional[Mapping[str, Any]]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM gp_snapshots WHERE league_id=? AND team_id=? "
                "AND matchup_period=?",
                (int(league_id), int(team_id), int(matchup_period)),
            ).fetchone()
        return dict(row) if row else None

    # -- scoreboard --------------------------------------------------------

    #: Written and read by name so the page and the run cannot drift apart on
    #: column order, and so adding a field is one edit in two places rather
    #: than three in four.
    SCOREBOARD_FIELDS = (
        "run_id", "matchup_period", "scoring_period",
        "my_team_id", "my_name", "my_abbrev", "my_logo", "my_points",
        "opp_team_id", "opp_name", "opp_abbrev", "opp_logo", "opp_points",
        "my_games_used", "opp_games_used", "my_games_today", "games_cap",
    )

    def record_scoreboard(self, league_id: int, **fields: Any) -> None:
        """Replace this league's cached head-to-head with the current one.

        Last write wins, unlike :meth:`record_gp_snapshot`. A live score has
        no historical value and a stale one is actively misleading, so there is
        one row per league and it is the newest reading.

        Unknown keys are refused rather than ignored: a typo'd field name that
        silently did nothing would show up as a scoreboard that never updates
        one of its numbers, which is a long way to debug from the symptom.
        """
        bad = set(fields) - set(self.SCOREBOARD_FIELDS)
        if bad:
            raise ValueError("not scoreboard columns: %s" % ", ".join(sorted(bad)))
        cols = ["league_id", "updated_at"] + list(fields)
        with self._lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO scoreboards (%s) VALUES (%s)" % (
                    ", ".join(cols), ", ".join("?" * len(cols)),
                ),
                [int(league_id), _now()] + list(fields.values()),
            )

    def scoreboards(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM scoreboards").fetchall()
        return [dict(r) for r in rows]

    # -- forecasts ---------------------------------------------------------

    #: Columns :meth:`record_forecasts` accepts per row. Named rather than
    #: inferred from the first row so that a caller who omits a field on some
    #: players still produces uniform rows, and so a typo is a refusal.
    FORECAST_FIELDS = (
        "player_id", "name", "slot", "started", "projected", "p_play",
        "status", "status_source",
    )

    def record_forecasts(
        self,
        run_id: str,
        *,
        league_id: int,
        scoring_period: int,
        rows: Iterable[Mapping[str, Any]],
    ) -> int:
        """Store what this run projected, one row per player. Returns the count.

        One transaction for the whole roster: a half-written day is worse than
        an unwritten one, because the reconciliation would read it as a
        complete lineup with players missing and score the decision against a
        roster that never existed.
        """
        batch = []
        for row in rows:
            bad = set(row) - set(self.FORECAST_FIELDS)
            if bad:
                raise ValueError(
                    "not forecast columns: %s" % ", ".join(sorted(bad))
                )
            batch.append([row.get(f) for f in self.FORECAST_FIELDS])
        if not batch:
            return 0
        cols = ("run_id", "league_id", "scoring_period", "taken_at") \
            + self.FORECAST_FIELDS
        head = [run_id, int(league_id), int(scoring_period), _now()]
        with self._lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO forecasts (%s) VALUES (%s)" % (
                    ", ".join(cols), ", ".join("?" * len(cols)),
                ),
                [head + b for b in batch],
            )
        return len(batch)

    def save_defense(
        self,
        scope: str,
        *,
        team: Mapping[int, float],
        centre: Mapping[int, float],
        seasons: Sequence[int],
        games: int,
    ) -> None:
        """Store a fitted opponent-defence table under *scope*.

        Keys are stringified on the way in because JSON object keys are
        strings and a silent ``{"1": ...}`` round-tripping back as the string
        ``"1"`` would make every lookup miss and every multiplier 1.0 -- a
        failure that looks exactly like "no fitted table yet".
        :meth:`load_defense` casts them back.
        """
        blob = json.dumps({
            "team": {str(int(k)): float(v) for k, v in team.items()},
            "centre": {str(int(k)): float(v) for k, v in centre.items()},
        }, sort_keys=True)
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO defense "
                "(scope, seasons, games, table_json, fitted_at) "
                "VALUES (?,?,?,?,?)",
                (str(scope), ",".join(str(int(x)) for x in seasons),
                 int(games), blob, _now()),
            )
            self.db.commit()

    def load_defense(
        self, scope: str, max_age: timedelta
    ) -> Optional[Dict[str, Any]]:
        """The cached table for *scope*, or ``None`` if absent or too old.

        ``None`` for an unparseable row as well as a missing one. A table that
        cannot be read is not a reason to fail a run -- the caller's fallback
        is "no opponent adjustment", which is what the app did before this
        existed and is always a safe answer.
        """
        with self._lock:
            row = self.db.execute(
                "SELECT seasons, games, table_json, fitted_at FROM defense "
                "WHERE scope=?", (str(scope),),
            ).fetchone()
        if row is None:
            return None
        try:
            fitted_at = datetime.fromisoformat(row["fitted_at"])
            blob = json.loads(row["table_json"])
            team = {int(k): float(v) for k, v in (blob.get("team") or {}).items()}
            centre = {int(k): float(v) for k, v in (blob.get("centre") or {}).items()}
        except (TypeError, ValueError, KeyError):
            log.warning("cached opponent-defence table for scope %s is "
                        "unreadable; refitting", scope)
            return None
        if datetime.now(timezone.utc) - fitted_at > max_age:
            return None
        return {"team": team, "centre": centre, "games": int(row["games"]),
                "seasons": tuple(int(x) for x in str(row["seasons"]).split(",")
                                 if x.strip()),
                "fitted_at": fitted_at}

    def forecast_days(self, league_id: int) -> List[int]:
        """Scoring periods this league has a forecast for, oldest first."""
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT scoring_period FROM forecasts "
                "WHERE league_id=? ORDER BY scoring_period",
                (int(league_id),),
            ).fetchall()
        return [int(r["scoring_period"]) for r in rows]

    def forecasts_for(
        self, league_id: int, scoring_period: int
    ) -> List[Dict[str, Any]]:
        """The **newest** run's forecast for one day.

        Newest rather than all of them: two runs in a day are two different
        beliefs, and averaging them would score a lineup nobody played. The
        last one is the one the day was played with.
        """
        with self._lock:
            latest = self.db.execute(
                "SELECT run_id FROM forecasts WHERE league_id=? AND "
                "scoring_period=? ORDER BY id DESC LIMIT 1",
                (int(league_id), int(scoring_period)),
            ).fetchone()
            if latest is None:
                return []
            rows = self.db.execute(
                "SELECT * FROM forecasts WHERE run_id=? AND league_id=? "
                "AND scoring_period=? ORDER BY started DESC, projected DESC",
                (latest["run_id"], int(league_id), int(scoring_period)),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- proposals ---------------------------------------------------------

    def propose(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        rationale: str = "",
        value_delta: float = 0.0,
        ttl: timedelta = timedelta(hours=6),
        auto_approve: bool = False,
        declined: bool = False,
    ) -> Proposal:
        """Record an intended move.

        ``auto_approve`` is for the autonomous classes (lineup, IR). It still
        creates a row -- autonomy changes who approves, never whether there is
        an audit trail.

        ``declined`` records a move the app decided against *itself*, which
        never becomes a question for anybody: it is written straight into
        :data:`BLOCKED`, already finished. The ``expires_at`` it carries is
        now, which is meaningless for a terminal row and is written only
        because the column is ``NOT NULL`` -- nothing sweeps it, because
        ``expire_stale`` only touches ``PROPOSED``.
        """
        if declined and auto_approve:
            raise ValueError("a declined move cannot also be auto-approved")
        action_id = "%s:%s:%d" % (run_id, kind, _next_seq(self.db, run_id, kind))
        expires = datetime.now(timezone.utc) + (timedelta(0) if declined else ttl)
        state = BLOCKED if declined else (APPROVED if auto_approve else PROPOSED)
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO proposals (run_id, action_id, kind, state, payload, "
                "rationale, value_delta, created_at, expires_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    action_id,
                    kind,
                    state,
                    json.dumps(dict(payload)),
                    rationale,
                    float(value_delta),
                    _now(),
                    expires.isoformat(),
                    _now() if declined else None,
                ),
            )
            pid = int(cur.lastrowid)
        return Proposal(
            id=pid,
            run_id=run_id,
            action_id=action_id,
            kind=kind,
            state=state,
            payload=dict(payload),
            rationale=rationale,
            value_delta=value_delta,
            expires_at=expires,
        )

    def decide(self, action_id: str, approve: bool, user_id: str) -> Optional[Proposal]:
        """Apply a human decision from a notification tap.

        Guarded on ``state=PROPOSED``, so a tap on a *stale* notification --
        one whose proposal already ran, expired, or was rejected -- is a no-op
        returning ``None`` rather than a resurrection. HA registers notification
        action ids globally, which is precisely why ``action_id`` embeds the
        ``run_id`` and why this check cannot be skipped.
        """
        new_state = APPROVED if approve else REJECTED
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE proposals SET state=?, decided_at=?, decided_by=? "
                "WHERE action_id=? AND state=?",
                (new_state, _now(), user_id, action_id, PROPOSED),
            )
            if cur.rowcount == 0:
                log.warning(
                    "ignoring decision on %s: not in %s state (stale notification?)",
                    action_id,
                    PROPOSED,
                )
                return None
        return self.get(action_id)

    def claim_for_execution(self, proposal_id: int) -> bool:
        """``APPROVED -> EXECUTING``, atomically. The double-tap guard.

        Returns True if *this* caller won the race. Must be the only path to
        executing a proposal: any code that checks state and then executes in
        two steps has a window in which both the notification handler and the
        scheduler can pass the check.
        """
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE proposals SET state=? WHERE id=? AND state=?",
                (EXECUTING, int(proposal_id), APPROVED),
            )
            return cur.rowcount == 1

    def complete(self, proposal_id: int, ok: bool, error: str = "") -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE proposals SET state=?, finished_at=?, error=? WHERE id=?",
                (DONE if ok else FAILED, _now(), error, int(proposal_id)),
            )

    def expire_stale(self) -> int:
        """Sweep. Run on a 15-minute timer.

        Only ``PROPOSED`` rows expire. An ``EXECUTING`` row that outlives its
        TTL is a stuck write, not an expired approval, and silently expiring it
        would hide exactly the failure we most want to see.
        """
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE proposals SET state=?, finished_at=? "
                "WHERE state=? AND expires_at < ?",
                (EXPIRED, _now(), PROPOSED, _now()),
            )
            return cur.rowcount

    def get(self, action_id: str) -> Optional[Proposal]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM proposals WHERE action_id=?", (action_id,)
            ).fetchone()
        return _row_to_proposal(row) if row else None

    def pending(self, run_id: Optional[str] = None) -> List[Proposal]:
        q = "SELECT * FROM proposals WHERE state=?"
        args: List[Any] = [PROPOSED]
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [_row_to_proposal(r) for r in rows]

    def approved(self, run_id: Optional[str] = None) -> List[Proposal]:
        q = "SELECT * FROM proposals WHERE state=?"
        args: List[Any] = [APPROVED]
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        with self._lock:
            rows = self.db.execute(q + " ORDER BY id", args).fetchall()
        return [_row_to_proposal(r) for r in rows]

    def query(self, sql: str, args: Sequence[Any] = ()) -> List[Mapping[str, Any]]:
        """Read-only escape hatch, locked, for views this class should not own.

        The ingress page needs joins and orderings that are presentation
        decisions, not state-machine ones, and growing a bespoke method here for
        each of them would put the layout of a web page into the file that owns
        the approval invariants. What it must *not* do is touch ``self.db``
        directly: a run executes on a worker thread, so an unlocked cursor would
        interleave with its writes on the one shared connection.

        Rows are copied to plain dicts so nothing holding a cursor escapes the
        lock.
        """
        with self._lock:
            rows = self.db.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def stuck_writes(self) -> List[Mapping[str, Any]]:
        """Indeterminate journal entries awaiting reconciliation.

        Anything here means a POST timed out and we do not know whether ESPN
        applied it. Must be resolved by reading ESPN before the next write of
        the same kind.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM journal WHERE outcome='INDETERMINATE' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- journal (the interface espn/write.py depends on) ------------------

    def record_attempt(
        self, kind: str, body: Mapping[str, Any], *, run_id: Optional[str] = None,
        proposal_id: Optional[int] = None
    ) -> int:
        """Insert *before* the request. Returns the journal id.

        Ordering is the point: the row exists before the socket is written, so
        a crash mid-POST still leaves evidence. A write with no journal row is
        not a thing that can happen.
        """
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT INTO journal (run_id, proposal_id, kind, request, sent_at, outcome) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, proposal_id, kind, json.dumps(dict(body)), _now(), "SENT"),
            )
            return int(cur.lastrowid)

    def record_success(self, journal_id: Optional[int], result: Any) -> None:
        if journal_id is None:
            return
        with self._lock, self.db:
            self.db.execute(
                "UPDATE journal SET outcome='OK', transaction_id=?, proposed_date=?, "
                "response=? WHERE id=?",
                (
                    getattr(result, "transaction_id", None),
                    getattr(result, "proposed_date", None),
                    json.dumps(getattr(result, "raw", None) or {}),
                    int(journal_id),
                ),
            )

    def record_failure(
        self, journal_id: Optional[int], status: int, text: str
    ) -> None:
        if journal_id is None:
            return
        with self._lock, self.db:
            self.db.execute(
                "UPDATE journal SET outcome='REJECTED', http_status=?, response=? "
                "WHERE id=?",
                (int(status), text, int(journal_id)),
            )

    def record_indeterminate(self, journal_id: Optional[int], error: str) -> None:
        if journal_id is None:
            return
        with self._lock, self.db:
            self.db.execute(
                "UPDATE journal SET outcome='INDETERMINATE', error=? WHERE id=?",
                (error, int(journal_id)),
            )

    def resolve_indeterminate(self, journal_id: int, applied: bool, note: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE journal SET outcome=?, error=? WHERE id=?",
                ("OK" if applied else "NOT_APPLIED", note, int(journal_id)),
            )

    def record_shadow(self, kind: str, body: Mapping[str, Any]) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO journal (kind, request, sent_at, outcome) VALUES (?,?,?,?)",
                (kind, json.dumps(dict(body)), _now(), "SHADOW"),
            )


def _next_seq(db: sqlite3.Connection, run_id: str, kind: str) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM proposals WHERE run_id=? AND kind=?",
        (run_id, kind),
    ).fetchone()
    return int(row["n"]) + 1


def _row_to_proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        action_id=str(row["action_id"]),
        kind=str(row["kind"]),
        state=str(row["state"]),
        payload=json.loads(row["payload"]),
        rationale=str(row["rationale"] or ""),
        value_delta=float(row["value_delta"] or 0.0),
        expires_at=datetime.fromisoformat(row["expires_at"]),
    )
