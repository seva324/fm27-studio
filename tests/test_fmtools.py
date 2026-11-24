"""Tests for the FM27 tool layer and the Match Lab knowledge base.

Nothing here talks to a provider or to a game. A small in-memory SQLite table
stands in for the snapshot, behind the data layer's *read interface*
(``query_players``/``players_for_club``/``apply_overlay``), so the SQL the data
layer issues is exercised for real rather than mocked away - and a tool that
reached past the interface would fail here instead of quietly working.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import sqlite3
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

from fmrte import assistant as A  # noqa: E402
from fmrte import fmlookup  # noqa: E402
from fmrte import fmtools as F  # noqa: E402
from fmrte import fmplans  # noqa: E402
from fmrte import knowledge as K  # noqa: E402
from fmrte import ratings as R  # noqa: E402
from fmrte import spec  # noqa: E402
from fmrte import tacticfiles  # noqa: E402

COLUMNS = (
    "id", "unique_id", "name", "club", "playing_club", "age", "nationality",
    "ca", "pa", "asking_price", "asking_price_raw", "salary_weekly_raw",
    "contract_end_date", "goalkeeper", "defender_left", "defender_central",
    "defender_right", "wing_back_left", "wing_back_right",
    "defensive_midfielder", "midfielder_left", "midfielder_central",
    "midfielder_right", "attacking_midfielder_left",
    "attacking_midfielder_central", "attacking_midfielder_right", "striker",
    "condition", "match_sharpness", "jadedness", "morale", "injured",
    "transfer_listed", "listed_for_loan", "transfer_agreed", "is_loaned_out",
    "injury", "injury_max_days_remaining", "on_duty", "future_transfer_club",
) + R.ATTRIBUTE_COLUMNS


def _player(unique_id: int, name: str, club: str, **overrides) -> dict:
    row = {column: None for column in COLUMNS}
    row.update({
        "id": unique_id, "unique_id": unique_id, "name": name, "club": club,
        "age": 25, "ca": 150, "pa": 160, "condition": 9400,
        "match_sharpness": 8800, "morale": 14, "jadedness": 0,
        "injured": 0, "transfer_listed": 0, "listed_for_loan": 0,
        "transfer_agreed": 0, "is_loaned_out": 0, "on_duty": 0,
        "asking_price": 10_000_000, "asking_price_raw": 10_000_000,
        "salary_weekly_raw": 40_000, "contract_end_date": "2029-06-30",
    })
    for column in R.ATTRIBUTE_COLUMNS:
        row[column] = 12
    row["goalkeeper"] = 1
    row["defender_central"] = 1
    row["midfielder_central"] = 1
    row["striker"] = 1
    row.update(overrides)
    return row


def _position_column(name: str) -> str:
    """The data layer's own position rule: column, short label or template name."""
    text = str(name or "").strip()
    if text in spec.POSITION_COLUMNS:
        return text
    if text in spec.POSITION_LABEL_TO_COLUMN:
        return spec.POSITION_LABEL_TO_COLUMN[text]
    if text in spec.POSITION_FIELDS:
        return spec.POSITION_FIELDS[text]
    raise ValueError(f"{name!r} is not a position")


class FakeDb:
    """A snapshot stand-in: one real SQLite table plus the read interface.

    The methods it implements are the ones ``fmrte.db.Snapshot`` exposes -
    ``query_players``, ``players_for_club``, ``injured_players``,
    ``apply_overlay`` and the club reads - because that interface is what the
    tool modules are allowed to use.  There is no ``_apply`` here on purpose.
    """

    sortable = frozenset(COLUMNS)

    def __init__(self, players, club=None, fixtures=None, meta=None):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in COLUMNS)
        quoted = ",".join(f'"{column}"' for column in COLUMNS)
        self.conn.execute(
            f'CREATE TABLE players ({",".join(f"{c}" for c in COLUMNS)})'
            .replace('"id"', "id"))
        self.conn.executemany(
            f'INSERT INTO players ({quoted}) VALUES ({placeholders})',
            [tuple(row.get(column) for column in COLUMNS) for row in players])
        self.conn.commit()
        self.live: dict[int, dict] = {}
        self.editor = None
        self.attached = False
        self.read_only = False
        self._club = club
        self._fixtures = fixtures or []
        self._meta = meta or {"game_date": "2026-07-20", "fm_build": "2494687"}

    def meta(self):
        return dict(self._meta)

    def counts(self):
        row = self.conn.execute("SELECT COUNT(*) FROM players").fetchone()
        return {"players": int(row[0]) if row else 0, "staff": 0,
                "clubs": 1, "competitions": 1}

    def game_date(self, meta=None):
        return str((meta or self._meta).get("game_date") or "2026-07-20")

    def apply_overlay(self, table, row):
        if table == "players":
            live = self.live.get(int(row.get("unique_id") or 0))
            if live:
                row.update(live)
        return row

    def query_players(self, *, club=None, name=None, search=None, position=None,
                      position_min=None, min_age=None, max_age=None,
                      min_ca=None, max_ca=None, min_pa=None, max_pa=None,
                      nationality=None, transfer_listed=None, injured=None,
                      exclude_free_agents=False, order="ca", descending=True,
                      limit=200, columns="*"):
        if order not in self.sortable:
            raise ValueError(f"{order!r} is not a column that can be sorted on")
        clauses: list[str] = []
        params: list = []
        if club is not None:
            clauses.append("club = ?")
            params.append(club)
        if name:
            clauses.append("name LIKE ?")
            params.append(f"%{name}%")
        if search:
            clauses.append("(name LIKE ? OR club LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if position is not None:
            column = _position_column(position)
            clauses.append(f'"{column}" >= ?')
            params.append(1 if position_min is None else int(position_min))
        for column, value, low in (("age", min_age, True), ("age", max_age, False),
                                   ("ca", min_ca, True), ("ca", max_ca, False),
                                   ("pa", min_pa, True), ("pa", max_pa, False)):
            if value is not None:
                clauses.append(f'"{column}" {"<=" if not low else ">="} ?')
                params.append(int(value))
        if nationality is not None:
            clauses.append("nationality = ?")
            params.append(nationality)
        if transfer_listed is not None:
            clauses.append("transfer_listed = ?")
            params.append(1 if transfer_listed else 0)
        if injured is not None:
            clauses.append("injured = ?")
            params.append(1 if injured else 0)
        if exclude_free_agents:
            clauses.append("(club IS NOT NULL AND TRIM(club) <> '')")
        sql = "SELECT * FROM players"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f' ORDER BY "{order}" {"DESC" if descending else "ASC"}'
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = [self.apply_overlay("players", dict(row))
                for row in self.conn.execute(sql, tuple(params))]
        if columns not in ("*", None):
            wanted = tuple(columns)
            unknown = [column for column in wanted if column not in self.sortable]
            if unknown:
                raise ValueError(f"not player columns: {', '.join(unknown)}")
            rows = [{column: row.get(column) for column in wanted}
                    for row in rows]
        return rows

    def players_for_club(self, club, limit=300, order="ca", descending=True):
        name = (club or "").strip()
        if not name:
            return []
        return self.query_players(club=name, limit=limit, order=order,
                                  descending=descending, columns="*")

    def injured_players(self, limit=6):
        return self.query_players(
            injured=True, order="ca", limit=limit,
            columns=("unique_id", "name", "club", "injury", "injured", "ca"))

    def club_by_name(self, name):
        if self._club and (self._club.get("name") or "").lower() == (name or "").lower():
            return dict(self._club)
        return None

    def club(self, key):
        return self.club_by_name(str(key))

    def squad(self, club, limit=300):
        return self.query_players(club=club, limit=limit)

    def clubs(self, query="", limit=200):
        return []

    def staff(self, club, limit=300):
        return []

    def fixtures(self, club, limit=20):
        return self._fixtures[:limit]

    def next_fixture(self, club):
        return self._fixtures[0] if self._fixtures else None

    def player(self, key):
        row = self.conn.execute(
            "SELECT * FROM players WHERE unique_id=? OR id=? LIMIT 1",
            (key, key)).fetchone()
        return self.apply_overlay("players", dict(row)) if row else None


def _squad(club: str = "Milan") -> list[dict]:
    """A squad that can fill a 4-4-2 without repeats."""
    rows = []
    identities = [
        ("Keeper One", {"goalkeeper": 20, "defender_central": 1}),
        ("Left Back", {"defender_left": 20, "wing_back_left": 16}),
        ("Right Back", {"defender_right": 20, "wing_back_right": 16}),
        ("Centre Back A", {"defender_central": 20}),
        ("Centre Back B", {"defender_central": 19}),
        ("Mid Left", {"midfielder_left": 20, "attacking_midfielder_left": 17}),
        ("Mid Centre A", {"midfielder_central": 20, "defensive_midfielder": 18}),
        ("Mid Centre B", {"midfielder_central": 19, "defensive_midfielder": 16}),
        ("Mid Right", {"midfielder_right": 20, "attacking_midfielder_right": 17}),
        ("Striker A", {"striker": 20}),
        ("Striker B", {"striker": 19}),
        ("Young Striker", {"striker": 16, "ca": 110}),
    ]
    for index, (name, positions) in enumerate(identities, start=1):
        row = _player(index, name, club)
        for column in ("goalkeeper", "defender_left", "defender_central",
                       "defender_right", "wing_back_left", "wing_back_right",
                       "defensive_midfielder", "midfielder_left",
                       "midfielder_central", "midfielder_right",
                       "attacking_midfielder_left",
                       "attacking_midfielder_central",
                       "attacking_midfielder_right", "striker"):
            row[column] = 1
        row.update(positions)
        rows.append(row)
    return rows


SHAPE = ["Goalkeeper", "DefenderLeft", "DefenderCentral", "DefenderCentral",
         "DefenderRight", "MidfielderLeft", "MidfielderCentral",
         "MidfielderCentral", "MidfielderRight", "Striker", "Striker"]


class KnowledgeTests(unittest.TestCase):
    def test_the_shipped_pack_parses(self):
        pack = K.load()
        self.assertEqual(len(pack.findings), 68)
        self.assertIn("Football Manager 27", pack.engine)
        self.assertTrue(pack.method)
        self.assertTrue(pack.caveats)

    def test_an_exact_finding_can_be_found(self):
        finding = K.find("S20-CA-INERT")
        self.assertIsNotNone(finding)
        self.assertEqual(finding.magnitude, 0.0)

    def test_search_matches_words_and_tags(self):
        matched = {finding.id for finding in K.search("pace", limit=20)}
        self.assertIn("S1-PACE", matched)
        tactical = K.search("", "tactics", 20)
        self.assertTrue(tactical)
        self.assertTrue(all("tactics" in finding.tags for finding in tactical))

    def test_a_study_reads_every_finding_in_it(self):
        rows = K.study("01-attribute-values")
        self.assertGreater(len(rows), 5)
        self.assertTrue(all(row.study == "01-attribute-values" for row in rows))

    def test_the_priorities_are_in_measured_order(self):
        priorities = K.priorities()
        self.assertEqual(len(priorities), 10)
        self.assertEqual(priorities[0]["finding_id"], "S5-SHARPNESS-FIRST")
        self.assertEqual([row["priority"] for row in priorities],
                         list(range(1, 11)))

    def test_an_unknown_study_lists_what_exists(self):
        result = F.knowledge_study("no-such-study")
        self.assertEqual(result["count"], 0)
        self.assertIn("01-attribute-values", result["available_studies"])


class PositionTests(unittest.TestCase):
    def test_template_positions_map_to_snapshot_columns(self):
        self.assertEqual(F.position_field("Goalkeeper"), "goalkeeper")
        self.assertEqual(F.position_field("ST"), "striker")
        self.assertEqual(F.position_field("DefenderCentral"), "defender_central")
        self.assertIsNone(F.position_field("Sweeper"))

    def test_labels_round_trip(self):
        for name, label in F.POSITION_LABELS.items():
            self.assertEqual(F.short_label(name), label)
            self.assertEqual(F.position_field(label), F.POSITION_FIELDS[name])

    def test_general_positions_come_from_the_role_table(self):
        self.assertEqual(F.general_for("Striker"), "Striker")
        self.assertEqual(F.general_for("AMR"), "Winger")
        self.assertIn("WideForward", R.RATINGS[F.general_for("AMR")])


class RoleFitTests(unittest.TestCase):
    def test_a_role_name_is_matched_in_either_spelling(self):
        self.assertEqual(F.canonical_role("Wide Forward"), "WideForward")
        self.assertEqual(F.canonical_role("wideforward"), "WideForward")
        self.assertIsNone(F.canonical_role("Not A Role"))

    def test_role_fit_scores_a_key_attribute(self):
        player = _player(1, "Fast Winger", "Milan", attacker=True)
        player["pace"] = 20
        player["acceleration"] = 20
        player["dribbling"] = 20
        weak = dict(player)
        weak["pace"] = 4
        weak["acceleration"] = 4
        strong_fit = F.role_fit(player, "WideForward", F.PHASE_IN, "AMR")
        weak_fit = F.role_fit(weak, "WideForward", F.PHASE_IN, "AMR")
        self.assertTrue(strong_fit.available)
        self.assertGreater(strong_fit.score, weak_fit.score)

    def test_an_unknown_role_is_unavailable_not_zero(self):
        fit = F.role_fit(_player(1, "X", "Milan"), "No Such Role",
                         F.PHASE_IN, "ST")
        self.assertFalse(fit.available)

    def test_a_slot_fit_blends_position_role_and_ability(self):
        player = _player(1, "Striker", "Milan", striker=20, ca=200)
        fit = F.slot_fit(player, 1, "Striker")
        self.assertEqual(fit.position_score, 20.0)
        # 35 for the position, up to 45 for the role, 20 for a full CA reading.
        role_part = (fit.role_score or 0.0) / 20.0 * 45.0
        self.assertAlmostEqual(fit.overall,
                               round((35.0 + role_part + 20.0) * 10) / 10, places=1)

    def test_the_original_s_own_role_answers_for_the_position(self):
        result = F.role_attributes("ST")
        self.assertEqual(result["general_position"], "Striker")
        self.assertTrue(result["roles"])
        for row in result["roles"]:
            self.assertTrue(row["key_attributes"])

    def test_an_unknown_position_is_refused(self):
        self.assertIn("error", F.role_attributes("SW"))


class LineupTests(unittest.TestCase):
    def setUp(self):
        self.players = _squad()

    def test_every_slot_is_filled_once_with_a_unique_player(self):
        result = F.optimize_lineup(self.players, SHAPE)
        self.assertEqual(result["filled"], 11)
        self.assertFalse(result["unfilled_slots"])
        names = [row["player_name"] for row in result["assignments"]]
        self.assertEqual(len(names), len(set(names)))

    def test_a_player_below_the_minimum_is_not_used(self):
        players = [row for row in self.players if row["name"] != "Keeper One"]
        result = F.optimize_lineup(players, SHAPE)
        self.assertIn("GK", result["unfilled_slots"])

    def test_an_injured_player_is_excluded_unless_asked_for(self):
        for row in self.players:
            if row["name"] == "Striker A":
                row["injured"] = 1
                row["injury"] = "Hamstring"
        without = F.optimize_lineup(self.players, SHAPE)
        names = [row["player_name"] for row in without["assignments"]]
        self.assertNotIn("Striker A", names)
        with_injured = F.optimize_lineup(
            self.players, SHAPE, F.Constraints(include_injured=True))
        names = [row["player_name"] for row in with_injured["assignments"]]
        self.assertIn("Striker A", names)

    def test_a_lock_is_kept_and_warned_about_when_unfit(self):
        locked_id = self.players[0]["unique_id"]  # the goalkeeper
        constraints = F.Constraints(locked={1: locked_id})
        result = F.optimize_lineup(self.players, SHAPE, constraints)
        first = result["assignments"][0]
        self.assertEqual(first["slot"], 1)
        self.assertEqual(first["player_unique_id"], locked_id)
        self.assertTrue(first["locked"])

    def test_alternatives_are_offered_per_slot(self):
        result = F.optimize_lineup(self.players, SHAPE)
        self.assertTrue(result["alternatives"])
        for rows in result["alternatives"].values():
            self.assertLessEqual(len(rows), 3)

    def test_an_empty_shape_is_refused(self):
        self.assertIn("error", F.optimize_lineup(self.players, []))


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.players = _squad("Milan") + [
            _player(101, "Cheap Striker", "Torino", striker=20, ca=140,
                    asking_price_raw=5_000_000, asking_price=5_000_000),
            _player(102, "Expensive Striker", "Juventus", striker=20, ca=170,
                    asking_price_raw=90_000_000, asking_price=90_000_000),
            _player(103, "Unknown Price Striker", "Napoli", striker=20, ca=160,
                    asking_price_raw=None, asking_price=None),
            _player(104, "Loaned Out Striker", "Lazio", striker=20, ca=150,
                    is_loaned_out=1),
        ]
        self.db = FakeDb(self.players)

    def test_the_price_filter_is_literal(self):
        result = F.transfer_shortlist(
            self.db, "Striker", excluding_club="Milan", max_asking_price=10_000_000)
        names = [row["player_name"] for row in result["candidates"]]
        self.assertIn("Cheap Striker", names)
        self.assertNotIn("Expensive Striker", names)

    def test_an_unknown_price_stays_explicitly_unknown(self):
        result = F.transfer_shortlist(self.db, "Striker", excluding_club="Milan")
        rows = {row["player_name"]: row for row in result["candidates"]}
        self.assertIn("Unknown Price Striker", rows)
        self.assertIsNone(rows["Unknown Price Striker"]["asking_price"])
        self.assertFalse(rows["Unknown Price Striker"]["asking_price_known"])

    def test_the_own_club_and_unavailable_players_are_dropped(self):
        result = F.transfer_shortlist(self.db, "Striker", excluding_club="Milan")
        names = [row["player_name"] for row in result["candidates"]]
        self.assertNotIn("Striker A", names)
        self.assertNotIn("Loaned Out Striker", names)

    def test_a_replacement_names_the_player_it_replaces(self):
        reference = next(row for row in self.players
                         if row["name"] == "Striker A")
        result = F.find_replacements(self.db, reference["unique_id"],
                                     position="Striker")
        self.assertEqual(result["reference"]["player_name"], "Striker A")
        for row in result["candidates"]:
            self.assertIn("position_gain", row)


class SquadTests(unittest.TestCase):
    def setUp(self):
        self.players = _squad()
        for row in self.players:
            row["passing"] = 15
            row["technique"] = 15
            row["first_touch"] = 15
            row["decisions"] = 15
            row["pace"] = 15
            row["acceleration"] = 15
            row["stamina"] = 15
            row["work_rate"] = 15
            row["aggression"] = 13
            row["anticipation"] = 15
            row["composure"] = 15
            row["crossing"] = 15
            row["jumping_reach"] = 15
        self.db = FakeDb(self.players, club={
            "name": "Milan", "nation": "Italy", "competition": "Serie A",
            "reputation": 8300, "balance": 50_000_000,
            "transfer_budget": 20_000_000, "payroll_budget": 90_000_000,
            "training_facilities": 18, "youth_facilities": 16,
            "youth_coaching": 17, "youth_recruitment": 15,
        })

    def test_squad_analysis_reports_depth_and_needs(self):
        result = F.analyze_squad(self.db, "Milan")
        self.assertEqual(result["squad_size"], len(self.players))
        self.assertTrue(result["position_depth"])
        self.assertIsInstance(result["recruitment_needs"], list)

    def test_a_shape_adds_the_best_eleven(self):
        result = F.analyze_squad(self.db, "Milan", shape=SHAPE)
        self.assertEqual(len(result["tactic_fit"]), 11)
        self.assertFalse(result["unfilled_slots"])

    def test_instructions_are_values_the_game_accepts(self):
        instructions = F.derive_instructions(self.players)
        self.assertEqual(set(instructions), set(F.TEAM_INSTRUCTION_KEYS))
        for key, value in instructions.items():
            self.assertIn(value, F.TEAM_INSTRUCTION_VALUES[key])

    def test_a_technical_squad_keeps_the_ball(self):
        instructions = F.derive_instructions(self.players)
        self.assertIn(instructions["passingDirectness"], ("Shorter", "Standard"))
        self.assertNotEqual(instructions["tackling"], "Get Stuck In")

    def test_a_reckless_squad_is_told_to_get_stuck_in(self):
        rough = []
        for row in self.players:
            copy = dict(row)
            copy["aggression"] = 18
            rough.append(copy)
        self.assertEqual(F.derive_instructions(rough)["tackling"], "Get Stuck In")

    def test_club_context_carries_the_squad_and_the_money(self):
        result = F.club_context(self.db, "Milan", 5)
        self.assertEqual(result["club"], "Milan")
        self.assertEqual(result["transfer_budget"], 20_000_000)
        self.assertEqual(len(result["squad"]), 5)
        self.assertEqual(result["facility_scale"], "FM's own 0-20 scale")

    def test_an_unknown_club_is_an_error_not_a_crash(self):
        self.assertIn("error", F.club_context(self.db, "Nowhere FC"))

    def test_fixtures_come_back_with_the_game_date(self):
        db = FakeDb(self.players, fixtures=[{"home_club": "Milan",
                                             "away_club": "Inter",
                                             "kickoff_date": "2026-07-25"}])
        result = F.fixtures(db, "Milan", 5)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["game_date"], "2026-07-20")


class TrainingTests(unittest.TestCase):
    def test_a_week_has_seven_days_of_three_sessions(self):
        week = F.training_week("Balanced")
        self.assertEqual(len(week["days"]), 7)
        for day in week["days"]:
            self.assertEqual(len(day["sessions"]), 3)

    def test_every_session_is_a_measured_identifier(self):
        for focus in ("Balanced", "Pressing", "Attacking", "Ball control",
                      "Recovery", "Team bonding"):
            week = F.training_week(focus)
            for day in week["days"]:
                for session in day["sessions"]:
                    self.assertIn(session["id"], F.ACTIVITY_BY_ID)

    def test_the_focus_changes_the_week(self):
        pressing = F.training_week("Pressing")
        recovery = F.training_week("Recovery")
        self.assertNotEqual(pressing["days"], recovery["days"])

    def test_the_match_day_is_named(self):
        week = F.training_week("Balanced", match_day="Saturday")
        saturday = week["days"][5]
        self.assertEqual([session["id"] for session in saturday["sessions"]],
                         [500, 500, 500])

    def test_the_sheet_names_every_day(self):
        sheet = F.training_week("Attacking")["sheet"]
        for day in F.DAYS:
            self.assertIn(day, sheet)

    def test_the_activity_table_is_listed(self):
        result = F.training_activities()
        self.assertEqual(result["count"], len(F.TRAINING_ACTIVITIES))
        self.assertIn("Rest", [row["name"] for row in result["activities"]])


class SearchPushdownTests(unittest.TestCase):
    """A4: a name search reaches the data layer, not just the top 4,000.

    ``search_players`` used to ask for the 4,000 highest-ability players and
    then filter the query's words in Python.  A save has 42,344 players, so
    every youth prospect and lower-league player was unreachable.  The longest
    word of the query now goes into ``query_players(name=...)``; these tests
    prove the argument arrives, that a low-ability player outside the old slice
    is found, and that the remaining words are still applied in Python.
    """

    class _Recording:
        """A data-layer stand-in that keeps every filter it is handed."""

        def __init__(self):
            self.calls: list[dict] = []

        def query_players(self, **kwargs):
            self.calls.append(kwargs)
            return []

    def test_the_longest_word_reaches_the_data_layer(self):
        db = self._Recording()
        F.search_players(db, "Christian Pulisic")
        self.assertEqual(len(db.calls), 1)
        self.assertEqual(db.calls[0]["name"], "Christian")
        # The rest of the old query is unchanged: the scan is still bounded and
        # free agents are still excluded.
        self.assertEqual(db.calls[0]["limit"], fmlookup.SEARCH_SCAN_LIMIT)
        self.assertTrue(db.calls[0]["exclude_free_agents"])

    def test_an_empty_query_pushes_no_name_filter(self):
        db = self._Recording()
        F.search_players(db, "")
        self.assertIsNone(db.calls[0]["name"])

    def test_a_query_with_no_ascii_word_keeps_the_bounded_scan(self):
        # SQLite's LIKE is case-insensitive for ASCII only, so a non-ASCII word
        # cannot be pushed without risking a row the Python pass accepts.
        db = self._Recording()
        F.search_players(db, "Müller")
        self.assertIsNone(db.calls[0]["name"])

    def test_a_player_outside_the_old_top_four_thousand_is_findable(self):
        # 4,501 players above him by CA: the old scan (limit=4,000, ordered by
        # CA) could never have come back with a 41-CA youth player.
        players = [_player(index, f"Filler {index}", "Filler FC", ca=150,
                           pa=160)
                   for index in range(1, fmlookup.SEARCH_SCAN_LIMIT + 501)]
        players.append(_player(999999, "Aaron Prospect", "Youth FC", ca=41,
                               pa=120))
        db = FakeDb(players)
        found = F.search_players(db, "Aaron Prospect")
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["players"][0]["player_unique_id"], 999999)
        self.assertEqual(found["players"][0]["player_name"], "Aaron Prospect")
        # He is in the data layer's answer, not the old slice of it.
        self.assertEqual(found["examined"], 1)

    def test_every_word_of_the_query_still_has_to_match(self):
        db = FakeDb([_player(1, "Aaron Prospect", "Youth FC"),
                     _player(2, "Aaron Senior", "Youth FC")])
        self.assertEqual(F.search_players(db, "aaron prospect")["count"], 1)
        self.assertEqual(F.search_players(db, "aaron nobody")["count"], 0)
        self.assertEqual(F.search_players(db, "aaron")["count"], 2)


class SchemaDispatcherAgreementTests(unittest.TestCase):
    """A5: the schemas and ``execute_tool`` are one contract.

    ``additionalProperties`` is false on every tool, so an argument the
    dispatcher reads but the schema does not declare is an argument the model
    can never legally pass - the feature is dead, and no error says so.
    """

    @staticmethod
    def _source() -> str:
        with open(os.path.join(APP, "fmrte", "fmtools.py"),
                  encoding="utf-8") as handle:
            return handle.read()

    @classmethod
    def _dispatcher_reads(cls) -> set[str]:
        tree = ast.parse(cls._source())
        names: set[str] = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "execute_tool"):
                continue
            for call in ast.walk(node):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "get"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "args"
                        and call.args
                        and isinstance(call.args[0], ast.Constant)):
                    names.add(str(call.args[0].value))
        return names

    @staticmethod
    def _declared(tool_name: str) -> dict:
        """The schema's property table for one tool."""
        for spec in F.tool_definitions():
            if spec["function"]["name"] == tool_name:
                return spec["function"]["parameters"]["properties"]
        raise AssertionError(f"{tool_name} is not in the catalogue")

    def test_every_argument_the_dispatcher_reads_is_declared(self):
        declared: set[str] = set()
        for spec in F.tool_definitions():
            declared.update(spec["function"]["parameters"]["properties"])
        undeclared = self._dispatcher_reads() - declared
        self.assertEqual(undeclared, set(),
                         f"the dispatcher reads arguments no schema declares: "
                         f"{sorted(undeclared)}")

    def test_every_declared_argument_is_read_by_the_dispatcher(self):
        reads = self._dispatcher_reads()
        for spec in F.tool_definitions():
            declared = set(spec["function"]["parameters"]["properties"])
            self.assertEqual(declared - reads, set(),
                             f"{spec['function']['name']} declares arguments "
                             f"no branch reads: {sorted(declared - reads)}")

    def test_the_shortlist_declares_the_attribute_floors_it_filters_on(self):
        properties = self._declared("fm27_transfer_shortlist")
        self.assertIn("minimum_attributes", properties)
        self.assertEqual(properties["minimum_attributes"]["type"], "object")
        self.assertEqual(properties["minimum_attributes"]["additionalProperties"],
                         {"type": "integer"})
        result = F.execute_tool(FakeDb(_squad()), "fm27_transfer_shortlist",
                                {"position": "Striker",
                                 "minimum_attributes": {"pace": 15}})
        self.assertNotIn("error", result)
        self.assertEqual(result["criteria"]["minimum_attributes"], {"pace": 15})

    def test_the_replacement_tool_declares_the_club_it_excludes(self):
        self.assertIn("club", self._declared("fm27_find_replacements"))
        result = F.execute_tool(FakeDb(_squad()), "fm27_find_replacements",
                                {"player_unique_id": 10, "position": "ST",
                                 "club": "Milan"})
        self.assertNotIn("error", result)

    def test_a_malformed_attribute_floor_is_ignored_not_fatal(self):
        result = F.execute_tool(FakeDb(_squad()), "fm27_transfer_shortlist",
                                {"position": "Striker",
                                 "minimum_attributes": ["pace"]})
        self.assertNotIn("error", result)
        self.assertIsNone(result["criteria"]["minimum_attributes"])


class InstructionThresholdTests(unittest.TestCase):
    """A6: the squad means stay fractional, so the .5 thresholds apply.

    ``derive_instructions`` used to average with ``int(round(...))`` and then
    compare those integers with 14.5, 13.5, 11.5 and 10.5: the rounding threw
    away the half a threshold was written for.
    """

    @staticmethod
    def _squad(values: tuple[int, ...], *columns: str) -> list[dict]:
        """One player per value, ``values`` applied to every named attribute."""
        players = []
        for index, value in enumerate(values, start=1):
            row = _player(index, f"Player {index}", "Milan")
            for column in R.ATTRIBUTE_COLUMNS:
                row[column] = 10
            for column in columns:
                row[column] = value
            players.append(row)
        return players

    def test_the_average_is_a_float(self):
        players = [{"passing": 10}, {"passing": 11}]
        self.assertIsInstance(fmplans._average(players, "passing"), float)
        self.assertEqual(fmplans._average(players, "passing"), 10.5)
        self.assertEqual(fmplans._average([], "passing"), 0.0)

    def test_a_fractional_mean_reaches_the_threshold_it_was_written_for(self):
        # passing/technique/first_touch average 14.5 exactly.  Rounded to 14
        # first, ``technical >= 14.5`` is false and tempo falls back to
        # Standard; the mean itself clears it.
        squad = self._squad((14, 15), "passing", "technique", "first_touch",
                            "decisions", "composure")
        instructions = fmplans.derive_instructions(squad)
        self.assertEqual(instructions["tempo"], "Higher")
        self.assertEqual(instructions["passingDirectness"], "Shorter")
        # Athletic and pressing means are 10.0, so the low branches are taken
        # with the same fractional means.
        self.assertEqual(instructions["lineOfEngagement"], "Lower")
        self.assertEqual(instructions["triggerPress"], "Lower")

    def test_a_mean_of_fourteen_does_not_clear_the_fourteen_and_a_half_rule(self):
        squad = self._squad((14, 14), "passing", "technique", "first_touch",
                            "decisions")
        instructions = fmplans.derive_instructions(squad)
        self.assertEqual(instructions["tempo"], "Standard")


class ToolCatalogueTests(unittest.TestCase):
    def test_every_tool_has_a_name_and_a_schema(self):
        specs = F.tool_definitions()
        self.assertGreaterEqual(len(specs), 14)
        names = set()
        for spec in specs:
            self.assertEqual(spec["type"], "function")
            function = spec["function"]
            self.assertTrue(function["name"].startswith("fm27_"))
            self.assertTrue(function["description"])
            self.assertEqual(function["parameters"]["type"], "object")
            names.add(function["name"])
        self.assertEqual(len(names), len(specs))

    def test_an_unknown_tool_is_reported(self):
        result = F.execute_tool(FakeDb(_squad()), "fm27_not_a_tool", {})
        self.assertIn("error", result)

    def test_every_tool_survives_an_empty_argument_set(self):
        db = FakeDb(_squad())
        for spec in F.tool_definitions():
            name = spec["function"]["name"]
            result = F.execute_tool(db, name, {})
            self.assertIsInstance(result, dict, name)

    def test_the_lineup_tool_runs_through_the_catalogue(self):
        db = FakeDb(_squad())
        result = F.execute_tool(db, "fm27_optimize_lineup",
                                {"shape": SHAPE, "club": "Milan"})
        self.assertEqual(result["filled"], 11)

    def test_the_training_tool_runs_through_the_catalogue(self):
        result = F.execute_tool(FakeDb(_squad()),
                                "fm27_create_training_schedule",
                                {"focus": "Pressing"})
        self.assertEqual(len(result["days"]), 7)


class ToolLoopTests(unittest.TestCase):
    """The model's tool-calling round trip, with the transport stubbed out."""

    def setUp(self):
        from fmrte import assistant as A

        self.A = A
        self.db = FakeDb(_squad(), club={"name": "Milan", "nation": "Italy",
                                         "competition": "Serie A",
                                         "reputation": 8300})
        self.sent = []
        self.original = A._http_chat

    def tearDown(self):
        self.A._http_chat = self.original

    def _settings(self):
        return self.A.AssistantSettings(provider="commandcode")

    def test_a_tool_call_is_run_and_its_result_goes_back_to_the_model(self):
        replies = [
            {"choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "fm27_training_activities",
                                 "arguments": "{}"}}],
            }}]},
            {"choices": [{"message": {"content": "Here is the week."}}]},
        ]

        def fake(settings, key, messages, tools=None, timeout=None):
            self.sent.append((list(messages), tools))
            return replies[len(self.sent) - 1]

        self.A._http_chat = fake
        answer = self.A.chat_with_tools(
            self._settings(), "key", self.db, "What activities exist?")
        self.assertEqual(answer, "Here is the week.")
        self.assertEqual(len(self.sent), 2)
        first_messages, first_tools = self.sent[0]
        self.assertEqual(first_messages[0]["role"], "system")
        self.assertTrue(first_tools)
        second_messages, _tools = self.sent[1]
        roles = [message["role"] for message in second_messages]
        self.assertIn("tool", roles)
        tool_message = second_messages[-1]
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertIn("Rest", tool_message["content"])

    def test_the_turn_ends_when_the_model_stops_calling_tools(self):
        self.A._http_chat = lambda *args, **kwargs: {
            "choices": [{"message": {"content": "  No tools needed.  "}}]}
        answer = self.A.chat_with_tools(
            self._settings(), "key", self.db, "Hello")
        self.assertEqual(answer, "No tools needed.")

    def test_a_provider_without_tool_support_falls_back_once(self):
        from fmrte import assistant as A

        calls = []

        def fake(settings, key, messages, tools=None, timeout=None):
            calls.append(bool(tools))
            if tools:
                raise A.AssistantError("The provider request failed (HTTP 400).")
            return {"choices": [{"message": {"content": "Plain answer."}}]}

        self.A._http_chat = fake
        answer = A.chat_with_tools(self._settings(), "key", self.db, "Hi")
        self.assertEqual(answer, "Plain answer.")
        self.assertEqual(calls, [True, False])

    def test_an_endless_tool_loop_is_stopped(self):
        def fake(settings, key, messages, tools=None, timeout=None):
            return {"choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": f"call_{len(messages)}", "type": "function",
                    "function": {"name": "fm27_training_activities",
                                 "arguments": "{}"}}],
            }}]}

        self.A._http_chat = fake
        answer = self.A.chat_with_tools(
            self._settings(), "key", self.db, "Loop forever")
        self.assertIn("tool", answer.lower())

    def test_a_cli_provider_uses_the_plain_prompt_envelope(self):
        from fmrte import assistant as A

        prompts = []
        original = A.chat_completion
        A.chat_completion = (
            lambda settings, key, prompt, timeout=None: prompts.append(prompt)
            or "done")
        try:
            settings = A.AssistantSettings(provider="codex")
            answer = A.chat_with_tools(settings, "", self.db, "Who to sell?")
            self.assertEqual(answer, "done")
            self.assertIn("The following Football Manager 2027 context was "
                          "loaded locally by the application.", prompts[0])
            self.assertIn("Who to sell?", prompts[0])
        finally:
            A.chat_completion = original


class OneAuthorityTests(unittest.TestCase):
    """Each domain rule has one object; a re-introduced copy fails here.

    ``assertIs`` rather than ``assertEqual`` on purpose: a copy passes an
    equality check today and drifts tomorrow.
    """

    def test_the_instruction_table_is_one_mapping(self):
        self.assertIs(F.TEAM_INSTRUCTION_VALUES, A.TEAM_INSTRUCTIONS)
        self.assertIs(F.TEAM_INSTRUCTION_VALUES, fmplans.TEAM_INSTRUCTIONS)
        self.assertEqual(F.TEAM_INSTRUCTION_KEYS, tuple(F.TEAM_INSTRUCTION_VALUES))

    def test_the_activity_table_is_one_tuple(self):
        self.assertIs(F.TRAINING_ACTIVITIES, fmplans.TRAINING_ACTIVITIES)
        self.assertIs(A.TRAINING_ACTIVITIES, fmplans.TRAINING_ACTIVITIES)
        self.assertIs(F.ACTIVITY_BY_ID, A.ACTIVITY_BY_ID)
        self.assertIs(F.ACTIVITY_BY_ID, fmplans.ACTIVITY_BY_ID)
        self.assertEqual(F.ACTIVITY_BY_ID,
                         {row[0]: row for row in F.TRAINING_ACTIVITIES})

    def test_the_weekday_names_are_one_tuple(self):
        self.assertIs(F.DAYS, A.DAYS)
        self.assertIs(F.DAYS, fmplans.DAYS)
        self.assertEqual(len(F.DAYS), 7)

    def test_the_position_labels_are_the_spec_table(self):
        self.assertIs(F.POSITION_LABELS, spec.POSITION_LABELS)
        self.assertIs(A.POSITION_LABELS, spec.POSITION_LABELS)
        self.assertIs(F.POSITION_FIELDS, spec.POSITION_FIELDS)
        self.assertIs(F.LABEL_TO_GENERAL, spec.LABEL_TO_GENERAL)

    def test_the_plan_vocabulary_has_one_home(self):
        self.assertIs(A.DUTIES, fmplans.DUTIES)
        self.assertIs(A.ROLE_CATALOGUE, fmplans.ROLE_CATALOGUE)
        self.assertIs(A.SESSIONS_PER_DAY, fmplans.SESSIONS_PER_DAY)
        self.assertIs(A.formation_label, fmplans.formation_label)
        self.assertIs(A.load_templates, tacticfiles.load_templates)


class ImportDirectionTests(unittest.TestCase):
    """``fmtools`` must not need the assistant to be importable.

    The old cycle worked only because both hops were deferred: ``fmtools``
    imported ``assistant`` inside ``build_tactic_from_squad`` and ``assistant``
    imported ``fmtools`` inside two helpers.  The direction is now pinned.
    """

    def _run(self, script: str) -> str:
        done = subprocess.run([sys.executable, "-c", script], cwd=APP,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0,
                         f"{script} failed: {done.stdout}{done.stderr}")
        return done.stdout.strip()

    def test_the_tool_layer_does_not_load_the_assistant(self):
        self.assertEqual(
            self._run("import fmrte.fmtools; import sys; "
                      "print('fmrte.assistant' in sys.modules)"),
            "False")

    def test_the_assistant_does_load_the_tool_layer(self):
        self.assertEqual(
            self._run("import fmrte.assistant; import sys; "
                      "print('fmrte.fmtools' in sys.modules)"),
            "True")

    def test_the_plan_tables_do_not_load_the_assistant(self):
        self.assertEqual(
            self._run("import fmrte.fmplans; import sys; "
                      "print('fmrte.assistant' in sys.modules)"),
            "False")


class DataLayerSeamTests(unittest.TestCase):
    """Only ``db.py`` may reach the connection or the private overlay."""

    PATTERNS = (
        re.compile(r"\bdb\.conn\b"),
        re.compile(r"\bdb\._apply\b"),
        re.compile(r"\bassert\b[^\n]*\._apply\("),
    )

    def test_no_module_outside_the_data_layer_reaches_into_it(self):
        offenders: list[str] = []
        paths = sorted(glob.glob(os.path.join(APP, "fmrte", "*.py")))
        paths += sorted(glob.glob(os.path.join(APP, "fmrte", "pages", "*.py")))
        self.assertTrue(paths, "no modules were scanned")
        for path in paths:
            if os.path.basename(path) == "db.py":
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for pattern in self.PATTERNS:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(
                        f"{os.path.relpath(path, APP)}:{line}: {match.group(0)}")
        self.assertEqual(offenders, [],
                         "a module reached past the data layer's interface")


class FitCacheLifetimeTests(unittest.TestCase):
    """A fit must describe the player as he is *now*, not as he was."""

    def test_a_second_fit_sees_the_player_as_he_is_now(self):
        player = _player(7, "Changed", "Milan", striker=20, ca=150)
        first = F.slot_fit(player, 1, "Striker")
        player["striker"] = 4
        second = F.slot_fit(player, 1, "Striker")
        self.assertEqual(first.position_score, 20.0)
        self.assertEqual(second.position_score, 4.0)
        self.assertLess(second.overall, first.overall)

    def test_a_lineup_run_twice_scores_the_second_state(self):
        db = FakeDb(_squad())
        first = F.execute_tool(db, "fm27_optimize_lineup",
                               {"shape": SHAPE, "club": "Milan"})
        self.assertEqual(first["filled"], 11)
        chosen = next(row for row in first["assignments"]
                      if row["position"] == "ST")
        # Drop him below the position minimum between the two calls. If any
        # memo survived the first call, the second would still place him.
        db.conn.execute("UPDATE players SET striker=? WHERE unique_id=?",
                        (4, chosen["player_unique_id"]))
        second = F.execute_tool(db, "fm27_optimize_lineup",
                                {"shape": SHAPE, "club": "Milan"})
        names = [row["player_name"] for row in second["assignments"]]
        self.assertNotIn(chosen["player_name"], names)


class OneGeneratorTests(unittest.TestCase):
    """The panel and the tool build one plan from the same generator."""

    def setUp(self):
        self.db = FakeDb(_squad(), club={"name": "Milan", "nation": "Italy",
                                         "competition": "Serie A",
                                         "reputation": 8300})
        self.templates = tacticfiles.load_templates()
        if not self.templates:
            self.skipTest("the game's Tactics folder is not on this machine")

    def test_the_panel_path_and_the_tool_path_agree(self):
        # What the panel passes: the template name from the combo, the style,
        # the name from the entry, and the club that is open.
        template = self.templates[0]["name"]
        panel = F.build_tactic_from_squad(
            self.db, "Milan", template, "Balanced", name="My plan")
        tool = F.execute_tool(
            self.db, "fm27_build_tactic_from_squad",
            {"club": "Milan", "template": template, "style": "Balanced",
             "name": "My plan"})
        self.assertNotIn("error", panel, panel)
        self.assertNotIn("error", tool, tool)
        self.assertEqual(panel["sheet"], tool["sheet"])
        self.assertEqual(panel["roles"], tool["roles"])
        self.assertEqual(panel["instructions"], tool["instructions"])
        self.assertIs(panel["writes_game"], False)

    def test_the_panel_has_no_second_generator_to_fall_back_to(self):
        path = os.path.join(APP, "fmrte", "studio_api.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, path)
        segment = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "tactic_plan":
                segment = ast.get_source_segment(source, node)
        self.assertIsNotNone(segment, "tactic_plan is gone from studio_api")
        self.assertIn("build_tactic_from_squad", segment)
        self.assertNotIn("build_tactic_plan", segment)
        self.assertNotIn("load_templates", segment)

    def test_every_assigned_player_gets_a_role_from_the_same_index(self):
        """A6: the role pass reuses the index the eleven was picked from."""
        template = {"name": "4-4-2 test", "file": "test.json", "positions": SHAPE}
        original = fmplans.tacticfiles.load_templates
        fmplans.tacticfiles.load_templates = lambda: [template]
        try:
            built = F.build_tactic_from_squad(self.db, "Milan", "4-4-2 test",
                                              "Balanced")
        finally:
            fmplans.tacticfiles.load_templates = original
        self.assertNotIn("error", built, built)
        self.assertEqual(len(built["roles"]), 11)
        identifiers = [row["player_unique_id"] for row in built["roles"]]
        self.assertEqual(len(set(identifiers)), len(identifiers))
        for row in built["roles"]:
            self.assertTrue(row["player_name"], row)
            self.assertTrue(row["in_possession"]["role"], row)
            self.assertTrue(row["out_of_possession"]["role"], row)


class ModuleShapeTests(unittest.TestCase):
    """The new modules hide rules; they must not drag a toolkit along."""

    PLAIN_MODULES = ("fmroles.py", "fmlineup.py", "fmsquad.py",
                     "fmtransfers.py", "fmlookup.py", "fmplans.py",
                     "tacticfiles.py", "aiprompt.py")

    def test_none_of_them_imports_tkinter_sqlite3_or_ctypes(self):
        for name in self.PLAIN_MODULES:
            path = os.path.join(APP, "fmrte", name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0]
                                    for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertNotIn("tkinter", imported, name)
            self.assertNotIn("sqlite3", imported, name)
            self.assertNotIn("ctypes", imported, name)
            self.assertNotIn("urllib", imported, name)


if __name__ == "__main__":
    unittest.main()
