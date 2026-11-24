"""Unit tests for the snapshot layer, the rating maths and the live reader.

Run from app/ with:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v

Every expected value in the case tests was read off the person's own
screenshots (reference/picture-2.png for Pulisic, reference/picture-3.png for
AC Milan) and is checked against the real snapshot database, so a regression in
the data path fails the suite instead of silently showing a wrong number.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import struct
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

from fmrte import aiprompt  # noqa: E402
from fmrte import fmtools, livewrite, memmap  # noqa: E402
from fmrte import ratings  # noqa: E402
from fmrte import spec  # noqa: E402
from fmrte.db import (ATTRIBUTE_COLUMNS, DB_PATH, POSITIONS,  # noqa: E402
                      SEARCH_COLUMNS, SQUAD_COLUMNS, Snapshot)
from fmrte.fmmemory import LivePlayer, LiveReport  # noqa: E402

PULISIC_UID = 92074020
MILAN_CLUB_ID = 5598


class SnapshotTests(unittest.TestCase):
    """The database itself: it exists, it is complete, it is the right save."""

    @classmethod
    def setUpClass(cls):
        cls.db = Snapshot()

    def test_database_file_exists(self):
        self.assertTrue(os.path.exists(DB_PATH), DB_PATH)

    def test_counts_match_the_original_status_bar(self):
        counts = self.db.counts()
        self.assertEqual(counts["players"], 42344)
        self.assertEqual(counts["staff"], 23309)
        self.assertEqual(counts["clubs"], 42129)
        self.assertEqual(counts["competitions"], 6625)

    def test_metadata_is_the_person_save(self):
        meta = self.db.meta()
        self.assertEqual(meta["game_date"], "2026-07-20")
        self.assertEqual(meta["fm_build"], "2494687")
        self.assertEqual(meta["game_version"], "27.1.0+2505117")
        self.assertEqual(meta["snapshot_id"][:8], "9c9ddb6a")

    def test_every_player_has_the_core_fields(self):
        rows = self.db.conn.execute(
            "SELECT COUNT(*) FROM players WHERE ca IS NULL OR pa IS NULL "
            "OR condition IS NULL OR morale IS NULL OR unique_id IS NULL"
        ).fetchone()
        self.assertEqual(rows[0], 0)

    def test_unique_ids_are_unique(self):
        duplicate = self.db.conn.execute(
            "SELECT unique_id, COUNT(*) n FROM players GROUP BY unique_id "
            "HAVING n > 1 LIMIT 1").fetchone()
        self.assertIsNone(duplicate)


class PulisicCaseTests(unittest.TestCase):
    """The player in reference/picture-2.png, field by field."""

    @classmethod
    def setUpClass(cls):
        cls.db = Snapshot()
        cls.player = cls.db.player(PULISIC_UID)

    def test_player_is_found(self):
        self.assertIsNotNone(self.player, "Pulisic is missing from the snapshot")

    def test_identity_matches_the_screenshot(self):
        p = self.player
        self.assertEqual(p["unique_id"], 92074020)
        self.assertEqual(p["name"], "Christian Mate Pulisic")
        self.assertEqual(p["nationality"], "美利坚合众国")
        self.assertEqual(p["club"], "Milan")
        self.assertEqual(p["date_of_birth"], "1998-09-18")
        self.assertEqual(p["age"], 27)
        self.assertEqual(p["height_cm"], 178)

    def test_ability_and_status_match_the_screenshot(self):
        p = self.player
        self.assertEqual(p["ca"], 158)
        self.assertEqual(p["pa"], 163)
        self.assertEqual(p["condition"], 9417)
        self.assertEqual(p["match_sharpness"], 3500)
        self.assertEqual(p["jadedness"], -125)
        self.assertEqual(p["morale"], 14)
        self.assertEqual(p["asking_price_raw"], 48900392)
        self.assertEqual(p["salary_weekly_raw"], 84746)
        self.assertEqual(p["contract_end_date"], "2027-06-30")
        self.assertEqual(p["home_reputation"], 7800)
        self.assertEqual(p["current_reputation"], 7900)
        self.assertEqual(p["world_reputation"], 6950)
        self.assertEqual(p["left_foot"], 13)
        self.assertEqual(p["right_foot"], 20)

    def test_attributes_match_the_screenshot(self):
        p = self.player
        expected = {
            "corners": 15, "crossing": 14, "dribbling": 16, "finishing": 15,
            "first_touch": 16, "free_kicks": 12, "heading": 5,
            "long_shots": 15, "long_throws": 4, "marking": 3, "passing": 15,
            "penalties": 14, "tackling": 6, "technique": 15,
            "aggression": 12, "anticipation": 15, "bravery": 11,
            "composure": 13, "concentration": 14, "decisions": 13,
            "determination": 13, "flair": 15, "leadership": 12,
            "off_the_ball": 17, "positioning": 6, "teamwork": 13,
            "vision": 13, "work_rate": 13,
            "acceleration": 16, "agility": 15, "balance": 14,
            "jumping_reach": 8, "natural_fitness": 13, "pace": 14,
            "stamina": 13, "strength": 11,
            "consistency": 13, "dirtiness": 4, "important_matches": 14,
            "injury_proneness": 12, "versatility": 15,
            "adaptability": 15, "ambition": 15, "loyalty": 11,
            "pressure": 13, "professionalism": 14, "sportsmanship": 16,
            "temperament": 14, "controversy": 6,
        }
        for column, value in expected.items():
            with self.subTest(attribute=column):
                self.assertEqual(p[column], value)

    def test_positions_match_the_screenshot(self):
        p = self.player
        self.assertEqual(p["attacking_midfielder_right"], 20)
        self.assertEqual(p["attacking_midfielder_central"], 18)
        self.assertEqual(p["striker"], 17)
        self.assertEqual(p["attacking_midfielder_left"], 15)
        self.assertEqual(Snapshot.position_text(p), "AML/AMC/AMR")

    def test_rating_matches_the_screenshot(self):
        # The original's header reads 77.49 % / 77.49 %, not 77.50 %: it keeps
        # the ratio in a float32 and truncates the percentage to two decimals.
        self.assertEqual(Snapshot.rating(self.player), 77.49)
        self.assertEqual(ratings.role_rating(self.player), 77.49)

    def test_rating_is_ca_share_plus_the_best_role(self):
        # The original's own settings help (Assets/Languages/english.json):
        # "How much CA counts in Actual Rating and Potential Rating. The rest
        # comes from the player's best role. Default: 50%."
        # Pulisic is the one player where this and the old key-only reading
        # coincide (CA 158/200 = 0.79, role score 0.76, blend 0.775), so a
        # second player is needed to separate them.
        self.assertEqual(ratings.CA_SHARE, 0.5)
        self.assertAlmostEqual(ratings.role_score(self.player), 0.76, places=9)
        self.assertEqual(ratings.key_rating(self.player), 77.49)

        db = Snapshot()
        kylian = db.player(85139014)
        self.assertEqual(kylian["ca"], 192)
        blended = (ratings.CA_SHARE * (kylian["ca"] / ratings.ABILITY_MAX)
                   + (1.0 - ratings.CA_SHARE) * ratings.role_score(kylian))
        self.assertEqual(ratings.role_rating(kylian), ratings.as_displayed(blended))
        self.assertEqual(ratings.role_rating(kylian), 89.40)
        self.assertGreater(ratings.role_rating(kylian),
                           ratings.key_rating(kylian))

    def test_role_matches_the_screenshot(self):
        self.assertEqual(ratings.role_label(self.player), "Wide Forward")
        self.assertEqual(ratings.best_general_position(self.player), "Winger")

    def test_penalty_is_zero_in_the_best_position(self):
        best = ratings.best_general_position(self.player)
        self.assertEqual(ratings.position_penalty(self.player, best), 0)

    def test_injury_matches_the_screenshot(self):
        p = self.player
        self.assertEqual(p["injured"], 1)
        self.assertEqual(p["injury"], "胫骨严重骨折")
        self.assertEqual(p["injury_min_days_remaining"], "13")
        self.assertEqual(p["injury_max_days_remaining"], "31")


class SearchCaseTests(unittest.TestCase):
    """Searching the way the person does: type a name, get the same player."""

    @classmethod
    def setUpClass(cls):
        cls.db = Snapshot()

    def test_search_by_surname_finds_pulisic(self):
        results = self.db.search_players("Pulisic")
        ids = [row["unique_id"] for row in results]
        self.assertIn(PULISIC_UID, ids)

    def test_search_is_case_insensitive(self):
        lower = [r["unique_id"] for r in self.db.search_players("pulisic")]
        upper = [r["unique_id"] for r in self.db.search_players("PULISIC")]
        self.assertEqual(lower, upper)
        self.assertIn(PULISIC_UID, lower)

    def test_search_by_club_finds_the_squad(self):
        results = self.db.search_players("Milan")
        self.assertTrue(any(r["unique_id"] == PULISIC_UID for r in results))

    def test_search_results_are_sorted_by_ability(self):
        results = self.db.search_players("Milan", 50)
        abilities = [r["ca"] for r in results]
        self.assertEqual(abilities, sorted(abilities, reverse=True))

    def test_search_returns_the_same_row_as_the_player_page(self):
        found = self.db.search_players("Pulisic")[0]
        page = self.db.player(found["unique_id"])
        self.assertEqual(found["unique_id"], page["unique_id"])
        self.assertEqual(found["ca"], page["ca"])
        self.assertEqual(found["pa"], page["pa"])

    def test_empty_search_returns_nothing(self):
        self.assertEqual(self.db.search_players("zzzz-no-such-player"), [])

    def test_leaderboard_is_ordered(self):
        rows = self.db.leaderboard(20)
        self.assertEqual(len(rows), 20)
        abilities = [r["ca"] for r in rows]
        self.assertEqual(abilities, sorted(abilities, reverse=True))


class ClubCaseTests(unittest.TestCase):
    """AC Milan, from reference/picture-3.png."""

    @classmethod
    def setUpClass(cls):
        cls.db = Snapshot()
        cls.club = cls.db.club(MILAN_CLUB_ID)

    def test_club_is_found(self):
        self.assertIsNotNone(self.club)

    def test_club_matches_the_screenshot(self):
        c = self.club
        self.assertEqual(c["name"], "Milan")
        self.assertEqual(c["nation"], "意大利")
        self.assertEqual(c["competition"], "Serie A")
        self.assertEqual(c["reputation"], 8300)
        self.assertEqual(c["training_facilities"], 19)
        self.assertEqual(c["youth_facilities"], 19)
        self.assertEqual(c["youth_coaching"], 15)
        self.assertEqual(c["youth_recruitment"], 19)

    def test_display_name_is_the_original_name(self):
        self.assertEqual(Snapshot.display_club(self.club["name"]), "AC Milan")

    def test_staff_counts_are_real(self):
        counts = self.db.staff_counts("Milan")
        self.assertEqual(counts.get("scout"), 15)
        self.assertEqual(counts.get("physiotherapist"), 12)
        self.assertEqual(counts.get("coach"), 10)
        self.assertEqual(counts.get("sports scientist"), 4)

    def test_next_fixture_matches_the_screenshot(self):
        fixture = self.db.next_fixture("Milan")
        self.assertIsNotNone(fixture)
        self.assertEqual(fixture["kickoff_date"], "2026-07-25")
        self.assertEqual(fixture["opponent"], "帕尼扎")

    def test_squad_contains_pulisic(self):
        squad = self.db.squad("Milan", 200)
        ids = [row["unique_id"] for row in squad]
        self.assertIn(PULISIC_UID, ids)


class SquadProjectionTests(unittest.TestCase):
    """D2: the assistant's prompt reads the squad through the list projection.

    ``SQUAD_COLUMNS`` omitted ``match_sharpness``, so every top-player line in
    ``aiprompt.save_context`` read "sharp None" even though S5-SHARPNESS-FIRST
    is one of the assistant's always-on rules.  The projection and the prompt
    are pinned together here so the column cannot go missing again.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = Snapshot()

    def test_the_projection_carries_sharpness_and_jadedness(self):
        self.assertIn("match_sharpness", SEARCH_COLUMNS)
        self.assertIn("match_sharpness", SQUAD_COLUMNS)
        self.assertIn("jadedness", SQUAD_COLUMNS)
        row = self.db.squad("Milan", 1)[0]
        self.assertIn("match_sharpness", row)
        self.assertIn("jadedness", row)
        self.assertIsInstance(row["match_sharpness"], int)
        self.assertIsInstance(row["jadedness"], int)

    def test_save_context_prints_a_numeric_sharpness(self):
        context = aiprompt.save_context(self.db)
        self.assertIn("- Top squad players:", context)
        self.assertNotIn("sharp None", context)
        self.assertIsNotNone(re.search(r"sharp \d+", context))
        # The real values, on Pulisic's own line (3500 stored, -125 jadedness).
        pulisic = [line for line in context.splitlines()
                   if f"[{PULISIC_UID}]" in line]
        self.assertEqual(len(pulisic), 1, context)
        self.assertIn("sharp 3500", pulisic[0])
        self.assertIn("cond 9417", pulisic[0])

    def test_the_narrow_projection_still_hides_the_attribute_block(self):
        row = self.db.squad("Milan", 1)[0]
        self.assertEqual(len(row), len(SEARCH_COLUMNS))
        self.assertNotIn("dribbling", row)


class EditTests(unittest.TestCase):
    """Edits are local, reversible and never touch the shipped database."""

    def setUp(self):
        self.db = Snapshot()
        self.db.reset_edits()
        self.player = self.db.player(PULISIC_UID)

    def tearDown(self):
        self.db.reset_edits()
        self.db.conn.close()

    def test_set_value_changes_the_view_only(self):
        self.db.set_value("players", self.player, "ca", 199)
        self.assertEqual(self.db.player(PULISIC_UID)["ca"], 199)
        raw = sqlite3.connect(DB_PATH).execute(
            "SELECT ca FROM players WHERE unique_id=?", (PULISIC_UID,)).fetchone()
        self.assertEqual(raw[0], 158)

    def test_undo_restores_the_original(self):
        self.db.set_value("players", self.player, "morale", 20)
        self.db.undo()
        self.assertEqual(self.db.player(PULISIC_UID)["morale"], 14)

    def test_redo_reapplies(self):
        self.db.set_value("players", self.player, "morale", 20)
        self.db.undo()
        self.db.redo()
        self.assertEqual(self.db.player(PULISIC_UID)["morale"], 20)

    def test_dirty_counts_unsaved_edits(self):
        self.assertEqual(self.db.dirty(), 0)
        self.db.set_value("players", self.player, "condition", 10000)
        self.assertEqual(self.db.dirty(), 1)

    def test_heal_preset_values(self):
        # Presets/Heal.json: Condition 10000, MatchSharpness 10000, Jadedness 0.
        for column, value in (("condition", 10000), ("match_sharpness", 10000),
                              ("jadedness", 0), ("injured", 0)):
            self.db.set_value("players", self.player, column, value)
        healed = self.db.player(PULISIC_UID)
        self.assertEqual(healed["condition"], 10000)
        self.assertEqual(healed["match_sharpness"], 10000)
        self.assertEqual(healed["jadedness"], 0)
        self.assertEqual(healed["injured"], 0)


class RatingTests(unittest.TestCase):
    """The role-rating maths, checked against the original's own output."""

    def test_key_attributes_weight(self):
        self.assertEqual(ratings.KEY_ATTRIBUTES_WEIGHT, 16)
        self.assertEqual(ratings.PREFERABLE_ATTRIBUTES_WEIGHT, 4)

    def test_penalty_ladder(self):
        self.assertEqual(len(ratings.POSITION_RATING_PENALTIES), 20)
        self.assertEqual(ratings.POSITION_RATING_PENALTIES[0], 76)
        self.assertEqual(ratings.POSITION_RATING_PENALTIES[-1], 0)

    def test_rating_is_bounded(self):
        db = Snapshot()
        for row in db.leaderboard(50):
            player = db.player(row["unique_id"])
            value = ratings.role_rating(player)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 100.0)

    def test_every_player_has_a_role(self):
        db = Snapshot()
        for row in db.leaderboard(50):
            player = db.player(row["unique_id"])
            self.assertIn(ratings.best_general_position(player),
                          ratings.GENERAL_ORDER)

    def test_displayed_rating_truncates_like_the_original(self):
        # 124/160 is exactly 0.775 in double precision, but the original keeps
        # it in a float32 (0.7749999761581421) and truncates, so the screen
        # reads 77.49. This is the whole reason the build used to be 0.01 off.
        self.assertEqual(ratings.as_displayed(124 / 160), 77.49)
        self.assertEqual(ratings.as_displayed(1.0), 100.0)
        self.assertEqual(ratings.as_displayed(0.0), 0.0)
        # A value that would round up must still truncate, not round.
        self.assertEqual(ratings.as_displayed(0.6669), 66.68)

    def test_clamp_uses_the_originals_own_maximums(self):
        # The maximums the original's clamp helper is called with, read out of
        # its machine code: 20 for attributes, 200 for ability, 10000 for
        # condition/sharpness/reputation.
        self.assertEqual(ratings.clamp("dribbling", 99), 20)
        self.assertEqual(ratings.clamp("dribbling", -5), 0)
        self.assertEqual(ratings.clamp("ca", 250), 200)
        self.assertEqual(ratings.clamp("pa", 250), 200)
        self.assertEqual(ratings.clamp("condition", 20000), 10000)
        self.assertEqual(ratings.clamp("match_sharpness", 20000), 10000)
        self.assertEqual(ratings.clamp("morale", 99), 20)
        self.assertEqual(ratings.clamp("current_reputation", 20000), 10000)
        # Jadedness is signed: the Destroy preset writes 1000 and the save
        # holds -125, so a negative value must survive.
        self.assertEqual(ratings.clamp("jadedness", -125), -125)
        self.assertEqual(ratings.clamp("jadedness", 1000), 1000)
        # An unclamped column is left alone.
        self.assertEqual(ratings.clamp("asking_price_raw", 123456), 123456)

    def test_edits_are_clamped_by_the_snapshot(self):
        db = Snapshot()
        player = db.player(PULISIC_UID)
        db.set_value("players", player, "dribbling", 99)
        self.assertEqual(player["dribbling"], 20)
        db.set_value("players", player, "condition", 99999)
        self.assertEqual(player["condition"], 10000)
        db.set_value("players", player, "jadedness", -125)
        self.assertEqual(player["jadedness"], -125)

    def test_position_edits_are_clamped_to_the_games_1_to_20(self):
        """A typed 99 is stored as 20, and the row the page draws says 20."""
        db = Snapshot()
        try:
            player = db.player(PULISIC_UID)
            db.set_value("players", player, "striker", 99)
            self.assertEqual(player["striker"], 20)
            self.assertEqual(db.last_value, 20)
            db.set_value("players", player, "striker", 0)
            self.assertEqual(player["striker"], 1)
            self.assertEqual(db.player(PULISIC_UID)["striker"], 1)
        finally:
            db.close()


class LiveReaderTests(unittest.TestCase):
    """The live reader must fail in plain words, never with a raw TypeError."""

    def test_live_error_has_a_code(self):
        from fmrte.live import LiveError
        error = LiveError("message", "hint", "code")
        self.assertEqual(error.code, "code")
        self.assertEqual(error.hint, "hint")

    def test_diagnose_never_raises(self):
        from fmrte.live import diagnose
        report = diagnose()
        self.assertIsInstance(report.ok, bool)
        self.assertIsInstance(report.message, str)

    def test_module_enumeration_does_not_raise(self):
        """The exact call that used to raise "argument 3: TypeError"."""
        import ctypes
        from fmrte import live
        if os.name != "nt":
            self.skipTest("Windows only")
        handle = live.kernel32.OpenProcess(
            live.PROCESS_VM_READ | live.PROCESS_QUERY_INFORMATION, False,
            os.getpid())
        self.assertTrue(handle)
        try:
            base, size = live._module_base(handle, "python.exe")
            self.assertGreater(base, 0)
            self.assertGreater(size, 0)
            self.assertEqual(live._module_base(handle, "no_such_module.dll"),
                             (0, 0))
            info = live._ModuleInfo()
            modules = (live.w.HMODULE * 2048)()
            needed = live.w.DWORD()
            live._enum_modules(handle, modules, ctypes.sizeof(modules),
                               ctypes.byref(needed), live.LIST_MODULES_ALL)
            ok = live.psapi.GetModuleInformation(
                handle, modules[0], ctypes.byref(info), ctypes.sizeof(info))
            self.assertTrue(ok)
        finally:
            live.kernel32.CloseHandle(live.w.HANDLE(handle))

    def test_decode_record_rejects_nonsense(self):
        from fmrte.live import decode_record
        self.assertIsNone(decode_record(b"\x00" * 0x400))


class SpecAuthorityTests(unittest.TestCase):
    """The one field table, and the drift it replaced.

    Before ``fmrte/spec.py`` existed the same facts were declared by hand in
    six modules and had already drifted: three attribute tables, four position
    tables and two range tables that disagreed.  These tests are the contract
    that keeps every consumer pointed at the single authority, so the drift
    cannot come back: each test fails the moment a module re-declares a table,
    names a column the spec does not know, or lets two ranges disagree again.
    """

    # -- the authority itself ------------------------------------------------

    def test_the_spec_is_data_only(self):
        """spec must not import the project, or anything heavy or cyclic.

        This is what lets both ``db`` and ``ratings`` import it: if it ever
        needed either of them back the old import cycle would return.
        """
        with open(spec.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), spec.__file__)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0,
                                 "spec must not use relative imports")
                self.assertTrue(node.module, "spec must not do a bare import")
                imported.add(node.module)
        # A small allow-list: pure data plus the namedtuple helper.
        self.assertLessEqual(imported, {"__future__", "collections"},
                             f"unexpected import in spec.py: {imported}")

    def test_the_import_cycle_is_gone(self):
        """Neither ratings nor db may need the other to be importable first."""
        cases = {
            "ratings": ("import fmrte.ratings; "
                        "print('fmrte.db' in __import__('sys').modules)"),
            "db": ("import fmrte.db; "
                   "print('fmrte.ratings' in __import__('sys').modules)"),
        }
        for name, script in cases.items():
            done = subprocess.run([sys.executable, "-c", script], cwd=APP,
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0,
                             f"importing fmrte.{name} failed: "
                             f"{done.stdout}{done.stderr}")
            self.assertEqual(done.stdout.strip(), "False",
                             f"importing fmrte.{name} still pulls in the "
                             f"other module: {done.stdout.strip()}")

    def test_the_old_names_are_reexports_not_copies(self):
        """A public name kept for callers must be the spec's object.

        A copy would pass a content check today and drift tomorrow, which is
        exactly the failure this module exists to prevent.
        """
        self.assertIs(ATTRIBUTE_COLUMNS, spec.ATTRIBUTE_COLUMNS)
        self.assertIs(ratings.ATTRIBUTE_COLUMNS, spec.ATTRIBUTE_COLUMN_NAMES)
        self.assertIs(ratings.EDIT_RANGES, spec.RANGES)
        self.assertIs(ratings.POSITION_TO_GENERAL, spec.LABEL_TO_GENERAL)
        self.assertIs(ratings.GENERAL_TO_COMPETENCE,
                      spec.GENERAL_TO_COMPETENCE)
        self.assertIs(ratings.GENERAL_ORDER, spec.GENERAL_ORDER)
        self.assertIs(fmtools.POSITION_FIELDS, spec.POSITION_FIELDS)
        self.assertIs(fmtools.POSITION_LABELS, spec.POSITION_LABELS)
        self.assertIs(fmtools.LABEL_TO_GENERAL, spec.LABEL_TO_GENERAL)
        self.assertIs(memmap.POSITION_COLUMNS, spec.POSITION_COLUMNS)

    # -- attributes ----------------------------------------------------------

    def test_the_displayed_attribute_table_is_the_spec_table(self):
        rows = [tuple(row) for row in spec.ATTRIBUTE_COLUMNS]
        self.assertEqual(len(rows), 52)
        columns = [row[1] for row in rows]
        self.assertEqual(len(set(columns)), 52, "duplicate attribute column")
        self.assertEqual({row[2] for row in rows},
                         {"Technical", "Mental", "Physical", "Hidden",
                          "Goalkeeping"})
        for label, column, _group in rows:
            self.assertTrue(label.strip(), column)
            self.assertIn(column, spec.KNOWN_COLUMN_SET)
            self.assertEqual(spec.range_for(column), (0, spec.ATTRIBUTE_MAX),
                             column)
        # Every displayed column is also in the wider table the maths uses.
        self.assertLessEqual(set(columns), set(spec.ATTRIBUTE_COLUMN_NAMES))

    def test_the_wider_attribute_table_is_a_superset(self):
        wide = spec.ATTRIBUTE_COLUMN_NAMES
        self.assertEqual(len(wide), 62)
        self.assertEqual(len(set(wide)), 62, "duplicate column in the 62 set")
        self.assertLessEqual(
            {row[1] for row in spec.ATTRIBUTE_COLUMNS}, set(wide))
        # The ten columns the screens do not show are the hidden/personality
        # ones and the two foot strengths.
        self.assertEqual(set(wide) - {row[1] for row in spec.ATTRIBUTE_COLUMNS},
                         set(spec.HIDDEN_ATTRIBUTE_COLUMNS))

    # -- positions -----------------------------------------------------------

    def test_ui_and_tool_positions_are_the_spec_positions(self):
        expected = [(position.label, position.column)
                    for position in spec.POSITIONS]
        self.assertEqual([tuple(row) for row in POSITIONS], expected)
        self.assertEqual(len(spec.POSITIONS), 14)
        self.assertEqual(spec.POSITION_COLUMNS, memmap.POSITION_COLUMNS)
        for label, column in POSITIONS:
            self.assertIn(column, spec.POSITION_COLUMNS)
            self.assertEqual(spec.POSITION_LABEL_TO_COLUMN[label], column)
            # The tool layer must resolve the label the screens use to the
            # same column, in both spellings.
            self.assertEqual(fmtools.position_field(label), column)
            self.assertEqual(fmtools.position_field(
                {v: k for k, v in spec.POSITION_LABELS.items()}[label]), column)
            self.assertIn(spec.LABEL_TO_GENERAL[label], spec.GENERAL_ORDER)
        # Every template name resolves to the column of the same short label.
        for template, column in fmtools.POSITION_FIELDS.items():
            label = fmtools.POSITION_LABELS[template]
            self.assertEqual(spec.POSITION_LABEL_TO_COLUMN[label], column,
                             template)
        # Every general position the role table files roles under is known.
        self.assertLessEqual(set(spec.GENERAL_ORDER), set(ratings.RATINGS))
        for general, competences in spec.GENERAL_TO_COMPETENCE.items():
            self.assertTrue(competences, general)
            for column in competences:
                self.assertIn(column, spec.POSITION_COLUMNS, general)

    # -- ranges --------------------------------------------------------------

    def test_edit_and_live_ranges_agree_with_the_spec(self):
        self.assertEqual(ratings.EDIT_RANGES, spec.RANGES)
        # The live writer owns its own declaration until the later refactor;
        # here it must equal the spec's live table column for column.
        self.assertEqual(len(livewrite.RANGES), len(spec.LIVE_RANGES))
        for column, bounds in livewrite.RANGES.items():
            self.assertIn(column, spec.LIVE_RANGES, column)
            self.assertEqual(spec.LIVE_RANGES[column], bounds, column)
        # Two tables may differ only where the difference is declared, with a
        # reason, in LIVE_OVERRIDES.  Anything else is silent drift.
        for column, bounds in spec.LIVE_RANGES.items():
            if column not in spec.RANGES or column in spec.LIVE_OVERRIDES:
                continue
            self.assertEqual(bounds, spec.RANGES[column], column)
        for column, (bounds, reason) in spec.LIVE_OVERRIDES.items():
            self.assertTrue(reason, f"{column} override has no reason")
            self.assertIn(column, spec.RANGES, column)
            self.assertEqual(spec.LIVE_RANGES[column], bounds, column)
            self.assertNotEqual(bounds, spec.RANGES[column],
                                f"{column} is listed as an override but does "
                                f"not differ")

    def test_clamp_agrees_with_the_spec_everywhere(self):
        columns = (tuple(spec.RANGES) + ("dribbling", "left_foot", "adaptability",
                                         "goalkeeper", "unique_id",
                                         "asking_price_raw", "not_a_column"))
        for column in columns:
            for value in (-20000, -125, -1, 0, 1, 19, 20, 21, 99, 250,
                          9999, 10000, 20000):
                self.assertEqual(ratings.clamp(column, value),
                                 spec.clamp(column, value), (column, value))
        # An unclamped column is left exactly as given.
        self.assertEqual(spec.clamp("not_a_column", 123456), 123456)
        self.assertIsNone(spec.range_for("not_a_column"))

    def test_positions_are_clamped_to_the_games_1_to_20(self):
        """The 14 position columns were not clamped at all (99 stayed 99).

        A position is stored 1..20 directly (a position the player cannot play
        is 1, not 0) - the same rule ``livewrite._bounds`` already applied to
        the write path - so the canonical table has to say the same thing.
        """
        self.assertEqual(spec.POSITION_RANGE, (1, spec.ATTRIBUTE_MAX))
        self.assertEqual(spec.range_for("striker"), spec.POSITION_RANGE)
        for position in spec.POSITIONS:
            column = position.column
            self.assertEqual(spec.range_for(column), spec.POSITION_RANGE, column)
            self.assertEqual(spec.clamp(column, 99), 20, column)
            self.assertEqual(spec.clamp(column, 21), 20, column)
            self.assertEqual(spec.clamp(column, 0), 1, column)
            self.assertEqual(spec.clamp(column, -5), 1, column)
            self.assertEqual(spec.clamp(column, 12), 12, column)
            # The legacy entry point agrees, because it delegates here.
            self.assertEqual(ratings.clamp(column, 99), 20, column)

    # -- the live record -----------------------------------------------------

    def test_memmap_fields_are_known_spec_columns(self):
        self.assertEqual(set(memmap.DIRECT_FIELDS), set(spec.DIRECT_COLUMNS))
        for column in memmap.DIRECT_FIELDS:
            self.assertIn(column, spec.KNOWN_COLUMN_SET, column)
        for column in memmap.DIRECT_POSITIVE_FIELDS:
            self.assertIn(column, spec.KNOWN_COLUMN_SET, column)
        for column in memmap.ATTRIBUTE_OFFSETS:
            self.assertIn(column, spec.KNOWN_COLUMN_SET, column)
        self.assertEqual(spec.ATTRIBUTE_STORAGE_SCALE,
                         memmap.ATTRIBUTE_STORAGE_SCALE)
        self.assertEqual(spec.ATTRIBUTE_STORAGE_SCALE * spec.ATTRIBUTE_MAX, 100)

    # -- the role table ------------------------------------------------------

    def test_every_role_attribute_is_a_real_spec_column(self):
        """Every attribute the shipped role table names exists in the spec.

        This reuses ``ratings.validate_role_definitions`` (the rule the
        original's own 0x1407406C0 applies) with the spec's wide column set
        instead of restating the role table here.
        """
        columns = set(spec.ATTRIBUTE_COLUMN_NAMES)
        self.assertEqual(
            ratings.validate_role_definitions(known_columns=columns), [])
        named = {column
                 for roles in ratings.RATINGS.values()
                 for duties in roles.values()
                 for key, preferable in duties.values()
                 for column in tuple(key) + tuple(preferable)}
        self.assertTrue(named, "the role table was not traversed")
        self.assertEqual(sorted(named - spec.KNOWN_COLUMN_SET), [])
        self.assertEqual(sorted(named - columns), [])


# ---------------------------------------------------------------------------
# The data layer's own interface: reads, the live session and the edit journal
# ---------------------------------------------------------------------------
# ``Snapshot`` used to be thin enough that every caller reached past it:
# ``fmtools`` ran raw SQL and called the private ``_apply``, the assistant ran
# its own injury query, ``App`` ran its own "best player" query and closed
# ``db.conn`` itself, and the live session was assembled by hand in the Load
# Game dialog.  That had already cost two bugs:
#
# * Bug 1 - ``App.reload_snapshot`` carried only ``editor`` across a reload, so
#   the per-player live overlay and the live game date / managed club were
#   dropped and "Use last snapshot" fell back to the database's values.
# * Bug 2 - ``Snapshot.history`` and ``LiveEditor.undo_stack`` were pushed
#   independently (``heal_player`` pushed three entries to one and one to the
#   other, so Undo popped mismatched pairs) and ``redo`` never wrote into the
#   game at all.
#
# These tests pin the interface that replaces all of that.  They drive the real
# ``LiveEditor`` over the fake process ``tests/test_livewrite.py`` lays out, so
# the offsets, the write gate and the read-back verification are production
# code and only the game is fake.


def live_record(uid: int = PULISIC_UID, **overrides) -> LivePlayer:
    """One player exactly as the game's own reader hands him back."""
    values = dict(unique_id=uid, ca=158, pa=163, condition=9417,
                  sharpness=3500, jadedness=-125, morale=14,
                  home_reputation=7800, current_reputation=7900,
                  world_reputation=6950)
    values.update(overrides)
    return LivePlayer(**values)


def attached_snapshot(with_injury: bool = True):
    """A snapshot, the fake game and a real ``LiveEditor`` over it."""
    from tests.test_livewrite import (FakeEditor, FakeGame, build_injury,
                                      build_person, connected)

    game = FakeGame()
    build_person(game)
    if with_injury:
        build_injury(game)
    connected(game)
    editor = FakeEditor(game)
    # What LiveEditor.attach() fills in from the game's own report.
    editor.live_date = "2027-01-02"
    editor.managed_club = "Liverpool"
    return Snapshot(), game, editor


class LiveSessionTests(unittest.TestCase):
    """attach_live / detach_live / reload: the session belongs to the data layer."""

    def setUp(self):
        self.db, self.game, self.editor = attached_snapshot()
        self.player = self.db.player(PULISIC_UID)

    def tearDown(self):
        self.db.conn.close()

    def test_attach_live_stores_the_editor_the_metadata_and_the_values(self):
        count = self.db.attach_live(self.editor, records=[live_record(ca=190)])
        self.assertEqual(count, 1)
        self.assertTrue(self.db.attached)
        self.assertFalse(self.db.read_only)
        self.assertEqual(self.db.live_meta,
                         {"game_date": "2027-01-02",
                          "managed_club": "Liverpool"})
        # meta() merges them, so the status bar and the assistant describe the
        # running save rather than the snapshot.
        meta = self.db.meta()
        self.assertEqual(meta["managed_club"], "Liverpool")
        self.assertEqual(self.db.game_date(meta), "2027/01/02")
        # And the overlay is what the screen shows.
        self.assertEqual(self.db.player(PULISIC_UID)["ca"], 190)

    def test_attach_live_counts_every_record_it_applied(self):
        records = {PULISIC_UID: live_record(),
                   85139014: live_record(uid=85139014, ca=195)}
        self.assertEqual(self.db.attach_live(self.editor, records=records), 2)
        self.assertEqual(self.db.live_count(), 2)

    def test_attach_live_replaces_a_stale_overlay(self):
        """A fresh attach is a fresh reading: no leftover live values."""
        self.db.apply_live([live_record()])
        self.assertEqual(self.db.live_count(), 1)
        count = self.db.attach_live(self.editor, records=[live_record(uid=12345)])
        self.assertEqual(count, 1)
        self.assertEqual(self.db.live_count(), 1)
        self.assertNotIn(PULISIC_UID, self.db.live)

    def test_attach_live_takes_the_date_from_the_report_first(self):
        report = LiveReport(ok=True, game_date="2026-12-24",
                            managed_club="Milan")
        self.db.attach_live(self.editor, records=[live_record()], report=report)
        self.assertEqual(self.db.live_meta,
                         {"game_date": "2026-12-24", "managed_club": "Milan"})

    def test_an_unknown_date_never_replaces_the_snapshot_one(self):
        self.editor.live_date = ""
        self.editor.managed_club = ""
        self.db.attach_live(self.editor, records=[live_record()])
        self.assertEqual(self.db.live_meta, {})
        self.assertEqual(self.db.meta()["game_date"], "2026-07-20")

    def test_attach_live_writes_the_edits_that_were_waiting(self):
        self.db.set_value("players", self.player, "dribbling", 19)
        self.assertEqual(self.db.last_edit, "waiting")
        self.assertEqual(self.db.last_flush, (0, 0))
        self.db.attach_live(self.editor)
        self.assertEqual(self.db.last_flush, (1, 0))
        self.assertEqual(self.editor.read_player(PULISIC_UID).values["dribbling"], 19)
        self.assertEqual(self.db.edits, {})
        self.assertEqual(self.db.player(PULISIC_UID)["dribbling"], 19)

    def test_a_read_only_attach_keeps_the_edits_staged_and_shows_them(self):
        from tests.test_livewrite import (FakeEditor, FakeGame, build_person,
                                          connected)

        game = FakeGame(can_write=False)
        build_person(game)
        connected(game)
        db = Snapshot()
        try:
            db.set_value("players", db.player(PULISIC_UID), "dribbling", 19)
            db.attach_live(FakeEditor(game), records=[live_record()])
            self.assertTrue(db.attached)
            self.assertTrue(db.read_only)
            self.assertEqual(db.last_flush, (0, 0))
            self.assertEqual(len(db.edits), 1)
            self.assertEqual(db.player(PULISIC_UID)["dribbling"], 19)
            self.assertEqual(game.writes, 0)
        finally:
            db.conn.close()

    def test_detach_live_clears_the_overlay_and_keeps_the_rows(self):
        self.db.attach_live(self.editor, records=[live_record(ca=190)])
        self.assertEqual(self.db.player(PULISIC_UID)["ca"], 190)
        self.db.detach_live()
        self.assertFalse(self.db.attached)
        self.assertFalse(self.db.read_only)
        self.assertEqual(self.db.live, {})
        self.assertEqual(self.db.live_meta, {})
        # The snapshot rows are untouched: they are the database.
        self.assertEqual(self.db.player(PULISIC_UID)["ca"], 158)
        self.assertEqual(self.db.counts()["players"], 42344)

    def test_reload_keeps_the_live_session(self):
        """Bug 1: this used to fall back to the database's own values."""
        self.db.attach_live(self.editor, records=[live_record(ca=190)])
        before = self.db.path
        self.db.reload()
        self.assertTrue(self.db.attached)
        self.assertEqual(self.db.path, before)
        self.assertEqual(self.db.live_meta,
                         {"game_date": "2027-01-02",
                          "managed_club": "Liverpool"})
        self.assertEqual(self.db.live_count(), 1)
        self.assertEqual(self.db.player(PULISIC_UID)["ca"], 190)
        self.assertEqual(self.db.counts()["players"], 42344)

    def test_reload_without_keep_live_clears_the_session(self):
        self.db.attach_live(self.editor, records=[live_record(ca=190)])
        self.db.reload(keep_live=False)
        self.assertFalse(self.db.attached)
        self.assertEqual(self.db.live, {})
        self.assertEqual(self.db.live_meta, {})
        self.assertEqual(self.db.player(PULISIC_UID)["ca"], 158)
        self.assertEqual(self.db.meta()["game_date"], "2026-07-20")

    def test_reload_starts_a_fresh_journal_and_overlay(self):
        self.db.set_value("players", self.player, "morale", 20)
        self.assertEqual(self.db.dirty(), 1)
        self.db.reload()
        self.assertEqual(self.db.journal, [])
        self.assertEqual(self.db.history, [])
        self.assertEqual(self.db.dirty(), 0)
        self.assertIsNone(self.db.undo())

    def test_reload_refuses_a_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.db.reload(os.path.join(APP, "data", "no_such_snapshot.db"))

    def test_nothing_attached_is_not_a_read_only_session(self):
        """The ``getattr(editor, "read_only", False)`` the dialog used to do."""
        db = Snapshot()
        try:
            self.assertFalse(db.attached)
            self.assertFalse(db.read_only)
        finally:
            db.conn.close()


class EditJournalTests(unittest.TestCase):
    """One user action is one undo entry, and redo writes into the game."""

    def setUp(self):
        self.db, self.game, self.editor = attached_snapshot(with_injury=True)
        self.db.attach_live(self.editor, records=[live_record()])
        self.player = self.db.player(PULISIC_UID)

    def tearDown(self):
        self.db.conn.close()

    def game_values(self):
        return self.editor.read_player(PULISIC_UID).values

    def injury_pointer(self):
        from tests.test_livewrite import PERSON
        return self.game.read(PERSON + memmap.INJURY_REFERENCE_REL, 8)

    def test_heal_is_one_journal_entry_however_many_fields_it_touches(self):
        """Bug 2: three column entries and one byte entry could not line up."""
        before = len(self.db.journal)
        healed, message = self.db.heal_player(self.player)
        self.assertTrue(healed, message)
        self.assertEqual(len(self.db.journal), before + 1)
        action = self.db.journal[-1]
        self.assertEqual(action.label, "heal")
        self.assertEqual(len(action.changes), 4)  # condition, sharpness, jadedness, injury
        # The Edit History page still lists every field the one action touched.
        self.assertEqual(len(self.db.history), 4)
        self.assertEqual(self.game_values()["condition"], 10000)
        self.assertEqual(self.game_values()["match_sharpness"], 10000)
        self.assertEqual(self.game_values()["jadedness"], 0)
        self.assertFalse(self.editor.read_injury(PULISIC_UID).injured)

    def test_one_undo_puts_the_whole_heal_back_in_the_game(self):
        self.db.heal_player(self.player)
        result = self.db.undo()
        self.assertIsNotNone(result)
        self.assertEqual(result[0].split(":")[-1], "heal")
        self.assertEqual(self.game_values()["condition"], 9417)
        self.assertEqual(self.game_values()["match_sharpness"], 3500)
        self.assertEqual(self.game_values()["jadedness"], -125)
        from tests.test_livewrite import INJURY_REFERENCE
        self.assertEqual(self.injury_pointer(),
                         struct.pack("<Q", INJURY_REFERENCE))
        self.assertTrue(self.editor.read_injury(PULISIC_UID).injured)
        # And the screen shows the game's own injury again, not a stale copy.
        healed = self.db.player(PULISIC_UID)
        self.assertEqual(healed["condition"], 9417)
        self.assertEqual(healed["injured"], 1)
        self.assertEqual(healed["injury"], "Hamstring strain")

    def test_redo_writes_the_heal_back_into_the_game(self):
        self.db.heal_player(self.player)
        self.db.undo()
        result = self.db.redo()
        self.assertIsNotNone(result)
        self.assertEqual(result[0].split(":")[-1], "heal")
        self.assertEqual(self.game_values()["condition"], 10000)
        self.assertEqual(self.game_values()["match_sharpness"], 10000)
        self.assertEqual(self.game_values()["jadedness"], 0)
        self.assertFalse(self.editor.read_injury(PULISIC_UID).injured)

    def test_redo_writes_a_plain_live_edit_into_the_game(self):
        """Bug 2, second half: redo used to touch the overlay only."""
        self.db.set_value("players", self.player, "dribbling", 20)
        self.assertEqual(self.game_values()["dribbling"], 20)
        self.db.undo()
        self.assertEqual(self.game_values()["dribbling"], 16)
        self.db.redo()
        self.assertEqual(self.game_values()["dribbling"], 20)
        self.assertEqual(self.db.player(PULISIC_UID)["dribbling"], 20)

    def test_a_plain_edit_is_one_entry_and_keeps_its_return_shape(self):
        # App.undo() prints result[0].split(":")[-1].
        self.db.set_value("players", self.player, "morale", 20)
        self.assertEqual(len(self.db.journal), 1)
        self.assertEqual(len(self.db.history), 1)
        result = self.db.undo()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].split(":")[-1], "morale")
        self.assertEqual(result[1:], (14, 20))

    def test_a_second_edit_clears_the_redo_stack(self):
        self.db.set_value("players", self.player, "morale", 20)
        self.db.undo()
        self.assertEqual(len(self.db.redo_stack), 1)
        self.db.set_value("players", self.player, "morale", 18)
        self.assertEqual(self.db.redo_stack, [])
        self.assertIsNone(self.db.redo())

    def test_a_staged_edit_undoes_and_redoes_in_the_overlay(self):
        db = Snapshot()
        try:
            player = db.player(PULISIC_UID)
            db.set_value("players", player, "morale", 20)
            self.assertEqual(db.player(PULISIC_UID)["morale"], 20)
            self.assertEqual(db.dirty(), 1)
            db.undo()
            self.assertEqual(db.player(PULISIC_UID)["morale"], 14)
            self.assertEqual(db.dirty(), 0)
            db.redo()
            self.assertEqual(db.player(PULISIC_UID)["morale"], 20)
            self.assertEqual(db.dirty(), 1)
        finally:
            db.conn.close()

    def test_a_flushed_edit_is_undone_in_the_game_not_the_overlay(self):
        """A waiting edit that reaches the game must be undoable there."""
        db = Snapshot()
        try:
            db.set_value("players", db.player(PULISIC_UID), "dribbling", 19)
            self.assertEqual(db.last_edit, "waiting")
            db.attach_live(self.editor)
            self.assertEqual(
                self.editor.read_player(PULISIC_UID).values["dribbling"], 19)
            db.undo()
            self.assertEqual(
                self.editor.read_player(PULISIC_UID).values["dribbling"], 16)
            self.assertEqual(db.edits, {})
        finally:
            db.conn.close()

    def test_undo_reports_a_game_it_can_no_longer_write_to(self):
        self.db.set_value("players", self.player, "dribbling", 20)
        self.db.detach_live()
        result = self.db.undo()
        self.assertIsNotNone(result)
        self.assertEqual(self.db.last_edit, "error")
        self.assertIn("not attached", self.db.last_error)
        # The screen still shows the value the undo restored.
        self.assertEqual(self.db.player(PULISIC_UID)["dribbling"], 16)

    def test_undo_and_redo_with_nothing_recorded_are_none(self):
        self.assertIsNone(self.db.undo())
        self.assertIsNone(self.db.redo())


class QueryInterfaceTests(unittest.TestCase):
    """The read interface: no caller needs ``db.conn`` for these any more."""

    @classmethod
    def setUpClass(cls):
        cls.db = Snapshot()

    @classmethod
    def tearDownClass(cls):
        cls.db.conn.close()

    def test_the_overlay_is_public_and_private_aliases_are_gone(self):
        row = {"id": 1, "unique_id": PULISIC_UID, "ca": 158}
        self.db.live[PULISIC_UID] = {"ca": 199}
        try:
            self.assertEqual(
                Snapshot.apply_overlay(self.db, "players", dict(row))["ca"], 199)
            # The private alias existed only until the tool layer moved off it.
            # Nothing may reach past the interface again, so it is not coming back.
            self.assertFalse(hasattr(Snapshot, "_apply"))
        finally:
            self.db.live.pop(PULISIC_UID, None)

    def test_rows_are_plain_dicts(self):
        row = self.db.query_players(club="Milan", limit=1)[0]
        self.assertIsInstance(row, dict)

    def test_players_for_club_returns_every_column(self):
        rows = self.db.players_for_club("Milan", 5)
        self.assertEqual(len(rows), 5)
        # A role fit needs the whole attribute block, not the list projection.
        self.assertGreater(len(rows[0]), 100)
        self.assertIn("dribbling", rows[0])
        self.assertNotIn("dribbling", self.db.squad("Milan", 5)[0])
        abilities = [row["ca"] for row in rows]
        self.assertEqual(abilities, sorted(abilities, reverse=True))

    def test_players_for_club_applies_the_live_overlay(self):
        self.db.live[PULISIC_UID] = {"ca": 199}
        try:
            rows = self.db.players_for_club("Milan", 200)
            pulisic = next(row for row in rows
                           if row["unique_id"] == PULISIC_UID)
            self.assertEqual(pulisic["ca"], 199)
            self.assertEqual(rows[0]["unique_id"], PULISIC_UID)  # now the best
        finally:
            self.db.live.pop(PULISIC_UID, None)

    def test_query_by_club_name_position_age_and_ability(self):
        milan = self.db.query_players(club="Milan", limit=None)
        self.assertTrue(milan)
        self.assertEqual({row["club"] for row in milan}, {"Milan"})
        self.assertIn(PULISIC_UID, {row["unique_id"] for row in milan})

        named = self.db.query_players(name="Pulisic")
        self.assertEqual([row["unique_id"] for row in named], [PULISIC_UID])
        self.assertIn(PULISIC_UID,
                      {row["unique_id"]
                       for row in self.db.query_players(search="Pulisic")})

        strikers = self.db.query_players(position="ST", position_min=20,
                                         limit=None)
        self.assertTrue(strikers)
        self.assertTrue(all(row["striker"] >= 20 for row in strikers))
        # A short label, a template name and the column mean the same thing.
        self.assertEqual(len(strikers), len(self.db.query_players(
            position="Striker", position_min=20, limit=None)))
        self.assertEqual(len(strikers), len(self.db.query_players(
            position="striker", position_min=20, limit=None)))

        aged = self.db.query_players(min_age=27, max_age=27, club="Milan",
                                     limit=None)
        self.assertTrue(aged)
        self.assertTrue(all(row["age"] == 27 for row in aged))

        best = self.db.query_players(min_ca=190, limit=None)
        self.assertEqual([row["ca"] for row in best], [192])

    def test_query_by_nationality_and_the_flags(self):
        usa = self.db.query_players(nationality="美利坚合众国", limit=None)
        self.assertTrue(usa)
        self.assertTrue(all(row["nationality"] == "美利坚合众国" for row in usa))

        listed = self.db.query_players(transfer_listed=True, limit=None)
        self.assertTrue(listed)
        self.assertTrue(all(row["transfer_listed"] for row in listed))

        hurt = self.db.query_players(injured=True, limit=None)
        self.assertTrue(hurt)
        self.assertTrue(all(row["injured"] for row in hurt))
        self.assertTrue(self.db.query_players(injured=False, limit=10))

        signed = self.db.query_players(exclude_free_agents=True, limit=None)
        self.assertTrue(all(str(row["club"] or "").strip() for row in signed))
        self.assertLess(len(signed), len(self.db.query_players(limit=None)))

    def test_order_and_limit_are_honoured(self):
        youngest = self.db.query_players(order="age", descending=False, limit=5)
        self.assertEqual(len(youngest), 5)
        ages = [row["age"] for row in youngest]
        self.assertEqual(ages, sorted(ages))
        best = self.db.query_players(order="ca", limit=5)
        abilities = [row["ca"] for row in best]
        self.assertEqual(abilities, sorted(abilities, reverse=True))
        self.assertGreater(len(self.db.query_players(limit=None)),
                           len(self.db.query_players(limit=10)))

    def test_a_filter_runs_on_the_value_the_screen_sees(self):
        """The overlay is applied before the filters are re-checked."""
        # The stored copy (158) passes the floor, but the live value the game
        # reports does not, so he must drop out of the result.
        self.db.live[PULISIC_UID] = {"ca": 100}
        try:
            ids = {row["unique_id"]
                   for row in self.db.query_players(min_ca=150, limit=None)}
            self.assertNotIn(PULISIC_UID, ids)
        finally:
            self.db.live.pop(PULISIC_UID, None)

    def test_a_column_that_cannot_be_sorted_asked_for_or_played_in_is_refused(self):
        with self.assertRaises(ValueError):
            self.db.query_players(order="ca; DROP TABLE players")
        with self.assertRaises(ValueError):
            self.db.query_players(columns=("name", "no_such_column"))
        with self.assertRaises(ValueError):
            self.db.query_players(position="Winger")  # a general position
        with self.assertRaises(ValueError):
            self.db.query_players(limit=0)

    def test_injured_players_gives_the_assistant_its_row(self):
        rows = self.db.injured_players(6)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["injured"] for row in rows))
        for key in ("unique_id", "name", "club", "injury", "ca"):
            self.assertIn(key, rows[0])
        abilities = [row["ca"] for row in rows]
        self.assertEqual(abilities, sorted(abilities, reverse=True))

    def test_top_player_is_the_best_current_ability(self):
        top = self.db.top_player()
        self.assertIsNotNone(top)
        self.assertEqual(top["ca"], 192)
        self.assertEqual(top["unique_id"], 85139014)  # Mbappé
        # And the live overlay decides it, not only the stored copy.
        self.db.live[85139014] = {"ca": 150}
        try:
            self.assertNotEqual(self.db.top_player()["unique_id"], 85139014)
        finally:
            self.db.live.pop(85139014, None)

    def test_the_list_screens_keep_their_narrow_projection(self):
        row = self.db.search_players("Pulisic", 1)[0]
        self.assertEqual(len(row), len(SEARCH_COLUMNS))
        self.assertNotIn("dribbling", row)
        self.assertEqual(self.db.search_players("Pulisic")[0]["unique_id"],
                         PULISIC_UID)
        squad = self.db.squad("Milan", 5)
        self.assertTrue(squad)
        self.assertIn("unique_id", squad[0])
        self.assertNotIn("vision", squad[0])


class OverlayLimitTests(unittest.TestCase):
    """D5: ``limit`` is a contract, even when the overlay disqualifies rows.

    SQLite applies its ``LIMIT`` while it reads the stored rows, before the live
    overlay exists, so a row it accepted can be disqualified afterwards and the
    caller got fewer rows than it asked for.  Healing the three best injured
    players used to make ``injured_players(6)`` return three rows.
    """

    def setUp(self):
        self.db = Snapshot()
        self.db.reset_edits()
        self.db.live.clear()

    def tearDown(self):
        self.db.live.clear()
        self.db.reset_edits()
        self.db.conn.close()

    def injured(self, limit: int) -> list[dict[str, Any]]:
        return self.db.injured_players(limit)

    def test_healing_the_best_injured_players_still_returns_a_full_page(self):
        before = self.injured(9)
        self.assertEqual(len(before), 9)
        healed = [row["unique_id"] for row in before[:3]]
        # The heal clears ``injured`` in the live overlay (``heal_player`` does
        # exactly this): the three best are no longer injured on screen.
        for uid in healed:
            self.db.live[uid] = {"injured": 0}
        after = self.injured(6)
        self.assertEqual(len(after), 6)
        self.assertTrue(all(row["injured"] for row in after))
        self.assertEqual([row["unique_id"] for row in after],
                         [row["unique_id"] for row in before[3:9]])
        # And they are still in ability order, so this is the same query.
        abilities = [row["ca"] for row in after]
        self.assertEqual(abilities, sorted(abilities, reverse=True))

    def test_a_filter_the_overlay_rewrites_keeps_the_page_full(self):
        # ``min_ca=150`` accepts the stored copy of the best players; dropping
        # their live CA below the floor must not shorten the page either.
        stored = self.db.query_players(min_ca=150, limit=12)
        self.assertEqual(len(stored), 12)
        for row in stored[:4]:
            self.db.live[row["unique_id"]] = {"ca": 100}
        after = self.db.query_players(min_ca=150, limit=12)
        self.assertEqual(len(after), 12)
        self.assertTrue(all(row["ca"] >= 150 for row in after))
        self.assertFalse({row["unique_id"] for row in after}
                         & {row["unique_id"] for row in stored[:4]})

    def test_an_edit_that_disqualifies_a_row_also_keeps_the_page_full(self):
        # The edits overlay counts too: it is applied after SQLite's LIMIT.
        stored = self.db.query_players(position="ST", position_min=19, limit=5)
        self.assertEqual(len(stored), 5)
        first = stored[0]
        key = f"players:{first['id']}:striker"
        self.db.edits[key] = 1          # no game attached: a staged edit
        try:
            after = self.db.query_players(position="ST", position_min=19,
                                         limit=5)
            self.assertEqual(len(after), 5)
            self.assertNotIn(first["unique_id"],
                             {row["unique_id"] for row in after})
        finally:
            self.db.edits.pop(key, None)

    def test_the_plain_path_still_uses_the_database_limit(self):
        # No overlay at all: nothing can disqualify a row, so the cheap path is
        # correct and is what the list screens keep using.
        self.assertFalse(self.db._overlay_can_change([("club", "eq", "Milan")]))
        self.db.live[PULISIC_UID] = {"ca": 199}
        try:
            self.assertTrue(self.db._overlay_can_change([("ca", "min", 150)]))
            self.assertTrue(self.db._overlay_can_change([("injured", "eq", 1)]))
            self.assertFalse(self.db._overlay_can_change(
                [("club", "eq", "Milan")]))
        finally:
            self.db.live.pop(PULISIC_UID, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
