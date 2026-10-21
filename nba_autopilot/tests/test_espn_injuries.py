"""ESPN injuries page: structured status, beat-report refinement, ceiling rule.

Every case below comes from the real page captured on 2026-10-21
(``tests/fixtures/espn_injuries.json``), so a failure here is evidence that
ESPN's feed changed rather than that a made-up example stopped parsing.

The three ``Day-To-Day`` cases are the reason tier 2 exists. ESPN's structured
field says the same thing about all of them -- Miller is probable, Swain is
doubtful, Hawkins has already been ruled out for the night -- and only the beat
reporter's sentence separates a 0.9 from a 0.0.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rootfs",
        "app",
    ),
)

from models import P_PLAY_PRIOR  # noqa: E402
from sources import espn_injuries  # noqa: E402
from sources.base import SourceError  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "espn_injuries.json"
)


def load_fixture():
    with open(FIXTURE) as fh:
        return json.load(fh)


class AthleteIdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.notes, self.unidentified = espn_injuries.parse(load_fixture())
        self.by_name = {n.full_name: n for n in self.notes}

    def test_id_comes_from_the_player_card_link(self):
        # The note carries no athlete id of its own; it only exists inside
        # .../nba/player/_/id/4712863/mouhamed-gueye.
        self.assertEqual(self.by_name["Mouhamed Gueye"].athlete_id, "4712863")

    def test_missing_links_yield_none_not_a_fabricated_id(self):
        self.assertIsNone(self.by_name["Collin Sexton"].athlete_id)

    def test_unidentified_players_are_listed_explicitly(self):
        # CONTRACTS.md section 4: the unmatched list is explicit, and the note
        # itself is still returned so it can join by name instead.
        self.assertEqual(self.unidentified, ["Collin Sexton"])
        self.assertIn("Collin Sexton", self.by_name)

    def test_unidentified_players_are_still_reachable_by_name(self):
        from sources.crosswalk import normalize_name

        by_name = espn_injuries.index_by_name(self.notes)
        self.assertIn(normalize_name("Collin Sexton"), by_name)
        self.assertNotIn("4712863", {})  # sanity: id index is separate
        self.assertIn("4712863", espn_injuries.index_by_athlete_id(self.notes))

    def test_athlete_id_handles_a_reshaped_links_block(self):
        self.assertIsNone(espn_injuries.athlete_id({}))
        self.assertIsNone(espn_injuries.athlete_id({"links": None}))
        self.assertIsNone(espn_injuries.athlete_id({"links": [{"href": ""}]}))
        self.assertIsNone(
            espn_injuries.athlete_id({"links": [{"href": "https://espn.com/nba/"}]})
        )


class CommentClassifierTest(unittest.TestCase):
    """The tier-2 rules, read off the real sentences."""

    def test_day_to_day_splits_three_ways(self):
        cases = {
            "Miller is probable for Wednesday's game against the Raptors with a "
            "right adductor strain.": "PROBABLE",
            "Swain (illness) is officially doubtful for Wednesday's season "
            "opener in Toronto.": "DOUBTFUL",
            "Hawkins (illness) is out for Wednesday's game against the Raptors, "
            "K.C. Johnson of Chicago Sports Network reports.": "OUT",
        }
        for comment, expected in cases.items():
            bucket, phrase = espn_injuries.classify_comment(comment)
            self.assertEqual(bucket, expected, comment[:40])
            self.assertTrue(phrase, "a match must record the phrase it matched")

    def test_practice_notes_carry_no_verdict(self):
        # Falling back to the coarse status is correct here. Guessing is not.
        for comment in (
            "Head coach Kenny Atkinson said Tuesday that Merrill (elbow) went "
            "through a full practice, per Dan Labbe of cleveland.com.",
            "Jones (hamstring) participated in Sunday's practice, Adam Mares of "
            "TheDNVR.com reports.",
            "The Rockets converted Phillips' deal to a two-way contract Monday, "
            "per Michael Scotto of HoopsHype.",
        ):
            self.assertEqual(
                espn_injuries.classify_comment(comment), (None, None), comment[:40]
            )

    def test_out_wins_over_a_weaker_word_in_the_same_sentence(self):
        # "has returned to individual on-court activity" must not read as a
        # return; "won't play" is the operative clause.
        bucket, _ = espn_injuries.classify_comment(
            "Knueppel (hamstring) has returned to individual on-court activity "
            "but won't play in Wednesday's season opener."
        )
        self.assertEqual(bucket, "OUT")

    def test_negated_clearance_is_not_a_clearance(self):
        bucket, _ = espn_injuries.classify_comment(
            "Quaintance has been around the team but has not yet been cleared "
            "for basketball activities, Matt Guzman reports."
        )
        self.assertIsNone(bucket)
        bucket, _ = espn_injuries.classify_comment("Smith is not expected to play.")
        self.assertEqual(bucket, "OUT")

    def test_empty_and_missing_comments_are_safe(self):
        self.assertEqual(espn_injuries.classify_comment(None), (None, None))
        self.assertEqual(espn_injuries.classify_comment(""), (None, None))


class ParsedNoteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.notes, _ = espn_injuries.parse(load_fixture())
        self.by_name = {n.full_name: n for n in self.notes}

    def test_fixture_covers_every_branch(self):
        self.assertEqual(len(self.notes), 8)

    def test_structured_status_normalises_to_a_p_play_key(self):
        for note in self.notes:
            self.assertIn(note.status, P_PLAY_PRIOR, note.full_name)
            self.assertIn(note.bucket, P_PLAY_PRIOR, note.full_name)

    def test_day_to_day_refinement_moves_p_play(self):
        miller = self.by_name["Leonard Miller"]
        swain = self.by_name["Dailyn Swain"]
        hawkins = self.by_name["Jordan Hawkins"]

        # Tier 1 cannot tell these three apart.
        for note in (miller, swain, hawkins):
            self.assertEqual(note.status, "DAY_TO_DAY", note.full_name)

        self.assertEqual(miller.bucket, "PROBABLE")
        self.assertEqual(swain.bucket, "DOUBTFUL")
        self.assertEqual(hawkins.bucket, "OUT")

        self.assertAlmostEqual(P_PLAY_PRIOR[miller.bucket], 0.90)
        self.assertAlmostEqual(P_PLAY_PRIOR[swain.bucket], 0.25)
        self.assertAlmostEqual(P_PLAY_PRIOR[hawkins.bucket], 0.0)

    def test_unmatchable_comment_falls_back_to_the_structured_status(self):
        merrill = self.by_name["Sam Merrill"]
        self.assertFalse(merrill.comment_matched)
        self.assertIsNone(merrill.comment_bucket)
        self.assertEqual(merrill.bucket, "DAY_TO_DAY")
        self.assertFalse(merrill.clamped)

    def test_refinements_record_the_phrase_they_matched(self):
        # This is what makes a refinement auditable in the run notes.
        hawkins = self.by_name["Jordan Hawkins"]
        self.assertTrue(hawkins.comment_matched)
        self.assertIn("is out", (hawkins.comment_phrase or "").lower())
        self.assertTrue(hawkins.disagrees)

    def test_out_players_stay_out(self):
        for name in ("Mouhamed Gueye", "Henri Veesaar", "Devin Carter"):
            note = self.by_name[name]
            self.assertEqual(note.status, "OUT", name)
            self.assertEqual(note.bucket, "OUT", name)
            self.assertEqual(P_PLAY_PRIOR[note.bucket], 0.0, name)

    def test_season_ending_is_distinguished_from_out_tonight(self):
        # Both are p_play 0 tonight; only one is a drop candidate.
        veesaar = self.by_name["Henri Veesaar"]
        self.assertTrue(veesaar.out_for_season)
        self.assertEqual(veesaar.return_date, date(2027, 7, 1))

        gueye = self.by_name["Mouhamed Gueye"]
        self.assertFalse(gueye.out_for_season)
        self.assertEqual(gueye.return_date, date(2026, 11, 20))

    def test_timetable_free_note_keeps_espns_placeholder_return_date(self):
        # Carter's comment says "without a timetable" while details.returnDate
        # still carries a date. The date is ESPN's guess, not a commitment, so
        # it is surfaced rather than trusted -- the comment text is retained
        # alongside it for exactly that reason.
        carter = self.by_name["Devin Carter"]
        self.assertEqual(carter.return_date, date(2026, 11, 8))
        self.assertIn("without a timetable", carter.short_comment)

    def test_reported_at_is_tz_aware_and_from_the_note(self):
        gueye = self.by_name["Mouhamed Gueye"]
        self.assertEqual(
            gueye.reported_at, datetime(2026, 10, 19, 19, 38, tzinfo=timezone.utc)
        )

    def test_team_and_identity_survive(self):
        gueye = self.by_name["Mouhamed Gueye"]
        self.assertEqual(gueye.team_abbrev, "ATL")
        self.assertEqual(gueye.note_id, "-58751")


class CeilingRuleTest(unittest.TestCase):
    """A comment may refine within its status's band, never above it."""

    def _note(self, status, comment):
        doc = {
            "injuries": [
                {
                    "id": "1",
                    "displayName": "Test Team",
                    "injuries": [
                        {
                            "id": "9",
                            "status": status,
                            "date": "2026-10-21T12:00Z",
                            "shortComment": comment,
                            "longComment": "",
                            "details": {},
                            "athlete": {
                                "displayName": "Test Player",
                                "links": [
                                    {"href": "https://espn.com/nba/player/_/id/1/x"}
                                ],
                                "team": {"abbreviation": "TST"},
                            },
                        }
                    ],
                }
            ]
        }
        notes, _ = espn_injuries.parse(doc)
        return notes[0]

    def test_an_out_player_cannot_be_talked_onto_the_floor(self):
        note = self._note("Out", "Jones is probable for Wednesday's game.")
        self.assertEqual(note.comment_bucket, "PROBABLE")
        self.assertEqual(note.bucket, "OUT")
        self.assertTrue(note.clamped)

    def test_day_to_day_tops_out_at_probable(self):
        note = self._note("Day-To-Day", "Jones is expected to play Wednesday.")
        self.assertEqual(note.comment_bucket, "ACTIVE")
        self.assertEqual(note.bucket, "PROBABLE")
        self.assertTrue(note.clamped)

    def test_a_refinement_inside_the_band_is_not_clamped(self):
        note = self._note("Day-To-Day", "Jones is doubtful for Wednesday's game.")
        self.assertEqual(note.bucket, "DOUBTFUL")
        self.assertFalse(note.clamped)

    def test_downgrades_are_always_allowed(self):
        note = self._note("Day-To-Day", "Jones has been ruled out for Wednesday.")
        self.assertEqual(note.bucket, "OUT")
        self.assertFalse(note.clamped)


class MalformedInputTest(unittest.TestCase):
    def test_missing_injuries_list_raises_a_source_error(self):
        # guard() at the fetch boundary turns this into None; parse() itself is
        # allowed to be loud because it is called from inside that boundary.
        with self.assertRaises(SourceError):
            espn_injuries.parse({})
        with self.assertRaises(SourceError):
            espn_injuries.parse({"injuries": {"not": "a list"}})

    def test_junk_entries_are_skipped_not_fatal(self):
        notes, unidentified = espn_injuries.parse(
            {"injuries": ["nope", {"injuries": ["nope", None]}, {}]}
        )
        self.assertEqual(notes, [])
        self.assertEqual(unidentified, [])

    def test_unknown_status_falls_back_to_the_fantasy_abbreviation(self):
        notes, _ = espn_injuries.parse(
            {
                "injuries": [
                    {
                        "injuries": [
                            {
                                "status": "Reconditioning",
                                "shortComment": "",
                                "details": {"fantasyStatus": {"abbreviation": "OFS"}},
                                "athlete": {"displayName": "Test Player"},
                            }
                        ]
                    }
                ]
            }
        )
        self.assertEqual(notes[0].status, "OUT")
        self.assertTrue(notes[0].out_for_season)

    def test_unknown_status_everywhere_lands_on_gtd_not_active(self):
        # Appearing on the injury page is itself information; the fallback must
        # not read as "fine".
        notes, _ = espn_injuries.parse(
            {
                "injuries": [
                    {
                        "injuries": [
                            {
                                "status": "Reconditioning",
                                "shortComment": "",
                                "details": {},
                                "athlete": {"displayName": "Test Player"},
                            }
                        ]
                    }
                ]
            }
        )
        self.assertEqual(notes[0].status, "GTD")

    def test_a_note_with_no_name_is_dropped(self):
        notes, _ = espn_injuries.parse(
            {"injuries": [{"injuries": [{"status": "Out", "athlete": {}}]}]}
        )
        self.assertEqual(notes, [])

    def test_name_is_rebuilt_from_first_and_last_when_display_name_is_absent(self):
        notes, _ = espn_injuries.parse(
            {
                "injuries": [
                    {
                        "injuries": [
                            {
                                "status": "Out",
                                "athlete": {
                                    "firstName": "Test",
                                    "lastName": "Player",
                                },
                            }
                        ]
                    }
                ]
            }
        )
        self.assertEqual(notes[0].full_name, "Test Player")

    def test_absent_note_id_is_none(self):
        notes, _ = espn_injuries.parse(
            {
                "injuries": [
                    {
                        "injuries": [
                            {
                                "status": "Out",
                                "athlete": {"displayName": "Test Player"},
                            }
                        ]
                    }
                ]
            }
        )
        self.assertIsNone(notes[0].note_id)
        self.assertIsNone(notes[0].reported_at)
        self.assertIsNone(notes[0].return_date)


class IndexTest(unittest.TestCase):
    def test_duplicate_ids_resolve_to_the_worse_bucket(self):
        doc = {
            "injuries": [
                {
                    "injuries": [
                        {
                            "status": "Day-To-Day",
                            "shortComment": "Jones is probable Wednesday.",
                            "athlete": {
                                "displayName": "Jones",
                                "links": [
                                    {"href": "https://espn.com/nba/player/_/id/7/j"}
                                ],
                            },
                        },
                        {
                            "status": "Day-To-Day",
                            "shortComment": "Jones has been ruled out Wednesday.",
                            "athlete": {
                                "displayName": "Jones",
                                "links": [
                                    {"href": "https://espn.com/nba/player/_/id/7/j"}
                                ],
                            },
                        },
                    ]
                }
            ]
        }
        notes, _ = espn_injuries.parse(doc)
        self.assertEqual(espn_injuries.index_by_athlete_id(notes)["7"].bucket, "OUT")
        self.assertEqual(
            espn_injuries.index_by_name(notes)["jones"].bucket, "OUT"
        )


#: Half an hour after the fixture was captured. The clock is pinned to this
#: for every fetch test, because staleness is measured against wall-clock now
#: -- left unpinned, these tests would pass today and start failing on their
#: own in a couple of days, which is the worst kind of failure.
FIXED_NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)


class FetchTest(unittest.TestCase):
    """The fetch path, with no network and a pinned clock."""

    def setUp(self) -> None:
        from sources import base

        self._base = base
        self._real_utcnow = base.utcnow
        self._real_fetch_json = espn_injuries.fetch_json
        base.utcnow = lambda: FIXED_NOW

    def tearDown(self) -> None:
        self._base.utcnow = self._real_utcnow
        espn_injuries.fetch_json = self._real_fetch_json

    def serve(self, doc):
        """Make ``fetch_json`` return *doc* (or raise it, if it's an exception)."""
        if isinstance(doc, BaseException):

            def handler(*a, **kw):
                raise doc

        else:

            def handler(url, client=None, timeout=None, retries=None, **kw):
                self.captured = {
                    "url": url,
                    "cookies": dict(client.cookies) if client is not None else None,
                    "headers": dict(client.headers) if client is not None else {},
                }
                return doc

        espn_injuries.fetch_json = handler

    def test_the_endpoint_is_public_and_the_client_carries_no_cookies(self):
        self.serve(load_fixture())
        res = espn_injuries.EspnInjuries().fetch()

        self.assertIsNotNone(res)
        self.assertEqual(self.captured["url"], espn_injuries.ESPN_INJURIES_URL)
        # The whole point of this module owning its client: espn_s2/SWID
        # cannot reach this host even if something else in the process holds
        # them.
        self.assertEqual(self.captured["cookies"], {})
        header_names = " ".join(self.captured["headers"]).lower()
        self.assertNotIn("cookie", header_names)
        self.assertNotIn("authorization", header_names)

    def test_fetch_reports_what_it_did(self):
        self.serve(load_fixture())
        src = espn_injuries.EspnInjuries()
        res = src.fetch()

        self.assertEqual(src.last_parse["notes"], 8)
        # Veesaar, Miller, Swain, Hawkins, Sexton. Gueye, Carter and Merrill
        # carry no game-level verdict and keep their structured status.
        self.assertEqual(src.last_parse["refined_by_comment"], 5)
        self.assertEqual(src.last_parse["clamped"], 0)
        self.assertEqual(src.last_parse["unidentified"], ["Collin Sexton"])
        self.assertEqual(len(res.payload["notes"]), 8)
        self.assertEqual(res.payload["source_url"], espn_injuries.ESPN_INJURIES_URL)

    def test_as_of_comes_from_the_page_timestamp(self):
        self.serve(load_fixture())
        res = espn_injuries.EspnInjuries().fetch()
        self.assertEqual(res.as_of, datetime(2026, 10, 21, 16, 32, 48, tzinfo=timezone.utc))

    def test_a_missing_page_timestamp_falls_back_to_the_newest_note(self):
        doc = load_fixture()
        doc.pop("timestamp")
        self.serve(doc)
        res = espn_injuries.EspnInjuries().fetch()
        self.assertIsNotNone(res, "a shape change must not take the source dark")
        newest = max(n.reported_at for n in res.payload["notes"] if n.reported_at)
        self.assertEqual(res.as_of, newest)

    def test_the_fallback_window_still_has_a_floor(self):
        # Wider than the page window, but not unbounded: a page whose newest
        # note is weeks old is a dead feed, not a quiet news day.
        doc = load_fixture()
        doc.pop("timestamp")
        for team in doc["injuries"]:
            for note in team["injuries"]:
                note["date"] = "2026-09-01T00:00Z"
        self.serve(doc)
        self.assertIsNone(espn_injuries.EspnInjuries().fetch())

    def test_a_dead_source_degrades_the_run_instead_of_killing_it(self):
        import httpx

        self.serve(httpx.ConnectError("no route to host"))
        self.assertIsNone(espn_injuries.EspnInjuries().fetch())

    def test_an_unexpected_exception_is_also_contained(self):
        self.serve(RuntimeError("something nobody predicted"))
        self.assertIsNone(espn_injuries.EspnInjuries().fetch())

    def test_an_empty_page_is_a_failure_not_an_all_clear(self):
        self.serve({"injuries": []})
        self.assertIsNone(espn_injuries.EspnInjuries().fetch())

    def test_a_non_object_body_is_a_failure(self):
        self.serve(["nope"])
        self.assertIsNone(espn_injuries.EspnInjuries().fetch())

    def test_a_stale_page_returns_none(self):
        doc = load_fixture()
        doc["timestamp"] = "2026-09-01T00:00Z"
        self.serve(doc)
        self.assertIsNone(espn_injuries.EspnInjuries().fetch())


class WiringTest(unittest.TestCase):
    """What ``run_league`` does with the notes.

    ``_availability_overrides`` touches no instance state, so it is called
    unbound with a ``None`` self -- the same trick ``test_scheduler.py`` uses,
    and it keeps these tests from needing a configured league.
    """

    def setUp(self) -> None:
        import main as mainmod
        from models import RosterEntry

        self.main = mainmod
        self.RosterEntry = RosterEntry
        notes, _ = espn_injuries.parse(load_fixture())
        self.injuries = _FakeResult(
            {
                "notes": notes,
                "by_athlete_id": espn_injuries.index_by_athlete_id(notes),
                "by_name": espn_injuries.index_by_name(notes),
                "parse": {"notes": len(notes), "refined_by_comment": 5},
            }
        )

    def entry(self, player_id, name, injury_status="ACTIVE"):
        return self.RosterEntry(
            player_id=player_id,
            name=name,
            current_slot=0,
            eligible_slots=frozenset({0}),
            pro_team_id=1,
            injury_status=injury_status,
        )

    def overrides(self, roster):
        return self.main.Autopilot._availability_overrides(None, roster, self.injuries)

    def test_join_is_a_plain_id_match(self):
        # Verified 2026-10-21: fantasy playerId and site athlete id are the
        # same number, so no crosswalk sits in this path.
        over, _ = self.overrides([self.entry(4712863, "Mouhamed Gueye")])
        # Gueye's note is a surgery report with no game-level verdict, so the
        # structured status stands -- and the label says which tier spoke.
        self.assertEqual(over[4712863], ("OUT", "espn-injuries"))

    def test_the_label_names_the_tier_that_decided(self):
        over, _ = self.overrides([self.entry(5105571, "Henri Veesaar")])
        self.assertEqual(over[5105571], ("OUT", "espn-beat-report"))

    def test_a_player_without_an_athlete_id_still_joins_by_name(self):
        over, _ = self.overrides([self.entry(99999, "Collin Sexton")])
        self.assertEqual(over[99999][0], "QUESTIONABLE")

    def test_a_healthy_player_gets_no_override(self):
        over, notes = self.overrides([self.entry(1966, "LeBron James")])
        self.assertEqual(over, {})
        self.assertEqual(notes, [])

    def test_disagreement_is_reported_not_resolved(self):
        # ESPN's fantasy feed has Hawkins as day-to-day; his beat reporter has
        # him already ruled out. Both readings reach the run record.
        over, notes = self.overrides(
            [self.entry(4432810, "Jordan Hawkins", injury_status="DAY_TO_DAY")]
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("Jordan Hawkins", notes[0])
        self.assertIn("DAY_TO_DAY", notes[0])
        self.assertIn("OUT", notes[0])
        self.assertIn("is out", notes[0])

    def test_an_absent_page_yields_no_overrides_and_no_crash(self):
        self.assertEqual(
            self.main.Autopilot._availability_overrides(None, [self.entry(1, "A")], None),
            ({}, []),
        )

    def test_an_override_only_moves_p_play_downward(self):
        """The asymmetry that keeps a stale note from starting a benched player."""
        from solve import projections as projections_mod
        from espn import ids

        splits = {
            7: {ids.SPLIT_SEASON_PROJECTED: {ids.PTS: 20.0, ids.MINUTES: 32.0}}
        }
        roster = [self.entry(7, "Test Player", injury_status="OUT")]

        # ESPN says OUT, the page says PROBABLE: ESPN's floor must hold.
        _, avail, _ = projections_mod.build_projections(
            roster,
            splits,
            as_of=FIXED_NOW,
            statuses={7: ("PROBABLE", "espn-beat-report")},
        )
        self.assertEqual(avail[7].p_play, 0.0)

        # ESPN says active, the page says OUT: the page wins.
        roster = [self.entry(7, "Test Player", injury_status="ACTIVE")]
        _, avail, _ = projections_mod.build_projections(
            roster,
            splits,
            as_of=FIXED_NOW,
            statuses={7: ("OUT", "espn-beat-report")},
        )
        self.assertEqual(avail[7].p_play, 0.0)
        self.assertEqual(avail[7].status_source, "espn-beat-report")

    def test_the_page_is_fetched_once_per_ttl_not_once_per_league(self):
        import types

        calls = []

        class FakeSource:
            def fetch(self):
                calls.append(1)
                return "result"

        bot = types.SimpleNamespace(_injuries=None, _injuries_at=None)
        real = self.main.EspnInjuries
        self.main.EspnInjuries = FakeSource
        try:
            for _ in range(3):
                self.assertEqual(
                    self.main.Autopilot.injury_notes(bot), "result"
                )
        finally:
            self.main.EspnInjuries = real
        self.assertEqual(len(calls), 1, "every league in a cycle shares one fetch")

    def test_the_cache_expires(self):
        import types
        from datetime import timedelta

        calls = []

        class FakeSource:
            def fetch(self):
                calls.append(1)
                return "result"

        stale = datetime.now(timezone.utc) - self.main.INJURY_NOTE_TTL - timedelta(
            minutes=1
        )
        bot = types.SimpleNamespace(_injuries="old", _injuries_at=stale)
        real = self.main.EspnInjuries
        self.main.EspnInjuries = FakeSource
        try:
            self.assertEqual(self.main.Autopilot.injury_notes(bot), "result")
        finally:
            self.main.EspnInjuries = real
        self.assertEqual(len(calls), 1, "a stale cache must be refetched")


class _FakeResult:
    """Stands in for a ``SourceResult`` without reaching the network."""

    def __init__(self, payload):
        self.payload = payload
        self.as_of = FIXED_NOW
        self.staleness_ok = True


if __name__ == "__main__":
    unittest.main()
