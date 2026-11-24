"""The data layer on another thread, and the edit session's own lifecycle.

Run from app/ with:  .venv\\Scripts\\python.exe -m unittest tests.test_session -v

These are the regressions the fake-database tests could not see:

* **D1.**  The assistant runs ``chat_with_tools`` on a ``threading.Thread``
  (``fmrte.assistant.ChatWorker``) and passes the window's ``Snapshot`` into
  it.  The connection used to be opened with SQLite's default
  ``check_same_thread=True``, so every read from that thread raised
  ``ProgrammingError``: ``meta()`` swallowed it and returned ``{}`` (the prompt
  lost the game date and the build), and every tool that touched players
  reported "no players are stored".  The tests below drive the *real* snapshot
  from a real thread.
* **D3.**  A contract date edited on the player page is journaled, undoable and
  visible, because it goes through ``Snapshot.set_date`` now.
* **D4.**  The journal survives flushing several edits to one field, a refused
  replay leaves both stacks alone, the live session closes its process handles,
  a failed reload keeps the old connection, and an undo that restores the
  database's own value takes the stale live overlay with it.
* **D6.**  A live morale of 80 - a value the person can see and edit - reaches
  the game unchanged and comes back on undo.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

from fmrte import aiprompt, fmtools, spec  # noqa: E402
from fmrte.db import DB_PATH, Snapshot  # noqa: E402
from fmrte.fmmemory import LivePlayer  # noqa: E402

PULISIC_UID = 92074020


def live_record(uid: int = PULISIC_UID, **overrides) -> LivePlayer:
    """One player exactly as the game's own reader hands him back."""
    values = dict(unique_id=uid, ca=158, pa=163, condition=9417,
                  sharpness=3500, jadedness=-125, morale=14,
                  home_reputation=7800, current_reputation=7900,
                  world_reputation=6950)
    values.update(overrides)
    return LivePlayer(**values)


def make_session(with_injury: bool = False):
    """The fake game and a real ``LiveEditor`` over it (see test_livewrite)."""
    from tests.test_livewrite import (FakeEditor, FakeGame, build_injury,
                                      build_person, connected)

    game = FakeGame()
    build_person(game)
    if with_injury:
        build_injury(game)
    connected(game)
    editor = FakeEditor(game)
    editor.live_date = "2027-01-02"
    editor.managed_club = "Liverpool"
    return game, editor


def run_in_thread(test: unittest.TestCase, work, timeout: int = 240):
    """Run ``work`` on a real thread and hand back its result.

    A failure *inside* the thread is re-raised on the test's own thread with
    the worker's traceback, so a SQLite ``ProgrammingError`` is reported as the
    failure it is instead of vanishing into a thread.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - the point is to report it
            box["error"] = exc
            box["trace"] = traceback.format_exc()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    test.assertFalse(thread.is_alive(), "the worker thread hung")
    if "error" in box:
        test.fail(f"the worker thread failed: {box['error']!r}\n{box['trace']}")
    return box["value"]


# ---------------------------------------------------------------------------
# D1: the assistant's reads, from the thread the assistant actually uses
# ---------------------------------------------------------------------------


class CrossThreadTests(unittest.TestCase):
    """The window's connection, read from a worker thread (D1)."""

    @classmethod
    def setUpClass(cls):
        # Opened here, on the main thread, exactly like the window's data
        # layer: this is what used to make every worker-thread read fail.
        cls.db = Snapshot()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_meta_and_counts_come_back_on_a_worker_thread(self):
        meta, counts = run_in_thread(self, lambda: (self.db.meta(),
                                                    self.db.counts()))
        # The real save's own values, not the empty dict meta() used to
        # return when SQLite refused the connection.
        self.assertEqual(meta["game_date"], "2026-07-20")
        self.assertEqual(meta["fm_build"], "2494687")
        self.assertEqual(meta["game_version"], "27.1.0+2505117")
        self.assertEqual(counts["players"], 42344)

    def test_every_squad_reader_works_on_a_worker_thread(self):
        rows = run_in_thread(self, lambda: {
            "full": self.db.players_for_club("Milan", 5),
            "squad": self.db.squad("Milan", 5),
            "injured": self.db.injured_players(6),
            "club": self.db.club_by_name("Milan"),
            "player": self.db.player(PULISIC_UID),
        })
        self.assertEqual(len(rows["full"]), 5)
        self.assertGreater(len(rows["full"][0]), 100)   # the full attribute row
        self.assertEqual(len(rows["squad"]), 5)
        self.assertEqual(len(rows["injured"]), 6)
        self.assertEqual(rows["club"]["name"], "Milan")
        self.assertEqual(rows["player"]["name"], "Christian Mate Pulisic")
        # The D2 projection fix is part of what makes these lines usable.
        self.assertIsInstance(rows["squad"][0]["match_sharpness"], int)

    def test_the_assistant_context_keeps_the_date_build_and_sharpness(self):
        context = run_in_thread(self, lambda: aiprompt.save_context(self.db))
        self.assertIn("- Game date: 2026/07/20", context)
        self.assertIn("Football Manager build: 2494687", context)
        self.assertIn("42,344 players", context)
        self.assertIn("- Active club context: Milan (Serie A)", context)
        # D2: the squad lines used to all read "sharp None".
        self.assertNotIn("sharp None", context)
        self.assertRegex(context, r"sharp \d+")

    def test_the_squad_tool_finds_the_squad_on_a_worker_thread(self):
        tool = run_in_thread(self, lambda: fmtools.execute_tool(
            self.db, "fm27_analyze_squad", {"club": "Milan"}))
        self.assertNotIn("error", tool)
        self.assertEqual(tool["club"], "Milan")
        self.assertGreater(tool["squad_size"], 0)
        # The old symptom, named so a regression is unmistakable.
        self.assertNotEqual(tool.get("error"),
                            "no players are stored for Milan")

    def test_several_threads_share_one_connection(self):
        results: list[tuple] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(4)

        def work(index: int) -> None:
            try:
                barrier.wait(timeout=30)
                for _ in range(5):
                    meta = self.db.meta()
                    counts = self.db.counts()
                    squad = self.db.squad("Milan", 20)
                    results.append((index, meta.get("game_date"),
                                    counts["players"], len(squad),
                                    squad[0]["unique_id"]))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,), daemon=True)
                   for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(240)
        self.assertEqual([thread.is_alive() for thread in threads],
                         [False] * 4)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        # Every thread saw the same save, never a half-updated one.
        self.assertEqual({row[1] for row in results}, {"2026-07-20"})
        self.assertEqual({row[2] for row in results}, {42344})
        self.assertEqual({row[3] for row in results}, {20})

    def test_close_is_safe_twice_and_from_a_worker_thread(self):
        db = Snapshot()
        run_in_thread(self, lambda: (db.close(), db.close()))
        db.close()  # and again, on the main thread

    def test_reads_while_the_window_stages_edits(self):
        """One thread edits the overlay while others read through it (D1).

        The overlay is a plain dict, so a read that iterates it while the
        window writes to it raises "dictionary changed size during iteration"
        unless both sides take the same lock.
        """
        db = Snapshot()
        errors: list[str] = []
        try:
            player = db.player(PULISIC_UID)

            def reader() -> None:
                try:
                    for _ in range(60):
                        row = db.player(PULISIC_UID)
                        if row is None:
                            errors.append("the player vanished")
                            return
                        if row["dribbling"] not in (16, 17, 18):
                            errors.append(f"torn row: {row['dribbling']!r}")
                            return
                        if db.history and db.dirty() < 0:
                            errors.append("impossible dirty count")
                            return
                except BaseException as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            threads = [threading.Thread(target=reader, daemon=True)
                       for _ in range(3)]
            for thread in threads:
                thread.start()
            for value in (17, 18, 16, 18, 16, 17):
                db.set_value("players", player, "dribbling", value)
            for thread in threads:
                thread.join(120)
            self.assertEqual([thread.is_alive() for thread in threads],
                             [False] * 3)
            self.assertEqual(errors, [])
        finally:
            db.close()


# ---------------------------------------------------------------------------
# D3: a contract date is a normal, undoable edit
# ---------------------------------------------------------------------------


class _StubBox:
    """The two calls ``_commit_date`` makes on the EntryBox."""

    def __init__(self, text: str):
        self.text = text

    def get(self) -> str:
        return self.text

    def set(self, text: str) -> None:
        self.text = text


class _StubApp:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def set_message(self, text: str, kind: str = "ok") -> None:
        self.messages.append((text, kind))

    def refresh_status(self) -> None:
        pass

    @property
    def last(self) -> tuple[str, str]:
        return self.messages[-1]


class _StubSection:
    """The three attributes ``playersections._commit_date`` reads."""

    def __init__(self, db, player) -> None:
        self.db = db
        self.player = player
        self.app = _StubApp()


class DateEditTests(unittest.TestCase):
    """D3: ``Snapshot.set_date`` - the game, the overlay and the journal."""

    def setUp(self):
        self.game, self.editor = make_session()
        self.db = Snapshot()
        self.db.attach_live(self.editor, records=[live_record()])
        self.player = self.db.player(PULISIC_UID)

    def tearDown(self):
        self.db.close()

    def game_date(self) -> str:
        return self.editor.read_player(PULISIC_UID).values["contract_end_date"]

    def test_the_date_reaches_the_game_the_overlay_and_the_journal(self):
        self.assertEqual(self.player["contract_end_date"], "2027-06-30")
        stored, message = self.db.set_date(
            PULISIC_UID, "contract_end_date", "2028-01-31")
        self.assertTrue(stored, message)
        self.assertIn("2028-01-31", message)
        self.assertIn("written to the game", message)
        # The game's own bytes.
        self.assertEqual(self.game_date(), "2028-01-31")
        # The overlay, so a rebuilt page shows the new date instead of the
        # snapshot's old one (part of the original bug).
        self.assertEqual(self.db.live[PULISIC_UID]["contract_end_date"],
                         "2028-01-31")
        self.assertEqual(self.db.player(PULISIC_UID)["contract_end_date"],
                         "2028-01-31")
        # One normal journal entry, so Ctrl+Z covers it.
        self.assertEqual(len(self.db.journal), 1)
        change = self.db.journal[0].changes[0]
        self.assertEqual(change.column, "contract_end_date")
        self.assertEqual((change.old, change.new), ("2027-06-30", "2028-01-31"))
        self.assertTrue(change.in_game)
        self.assertEqual(len(self.db.history), 1)
        self.assertEqual(self.db.last_edit, "game")

    def test_undo_and_redo_take_the_date_back_and_forth(self):
        self.db.set_date(PULISIC_UID, "contract_end_date", "2028-01-31")
        result = self.db.undo()
        self.assertIsNotNone(result)
        self.assertEqual(result[0].split(":")[-1], "contract_end_date")
        self.assertEqual(self.game_date(), "2027-06-30")
        self.assertEqual(self.db.player(PULISIC_UID)["contract_end_date"],
                         "2027-06-30")
        self.assertEqual(self.db.journal, [])
        self.assertEqual(len(self.db.redo_stack), 1)
        self.db.redo()
        self.assertEqual(self.game_date(), "2028-01-31")
        self.assertEqual(self.db.player(PULISIC_UID)["contract_end_date"],
                         "2028-01-31")

    def test_a_date_edited_without_a_game_is_staged_and_undoable(self):
        db = Snapshot()
        try:
            db.set_date(PULISIC_UID, "contract_end_date", "2028-01-31")
            self.assertEqual(db.last_edit, "waiting")
            self.assertEqual(db.player(PULISIC_UID)["contract_end_date"],
                             "2028-01-31")
            self.assertEqual(db.dirty(), 1)
            self.assertEqual(len(db.journal), 1)
            db.undo()
            self.assertEqual(db.player(PULISIC_UID)["contract_end_date"],
                             "2027-06-30")
            self.assertEqual(db.dirty(), 0)
            db.redo()
            self.assertEqual(db.player(PULISIC_UID)["contract_end_date"],
                             "2028-01-31")
        finally:
            db.close()

    def test_a_staged_date_is_flushed_into_the_game(self):
        db = Snapshot()
        try:
            db.set_date(PULISIC_UID, "contract_end_date", "2028-07-01")
            self.assertEqual(db.last_flush, (0, 0))
            db.attach_live(self.editor)   # no records: keep the staged date
            self.assertEqual(db.last_flush, (1, 0))
            self.assertEqual(db.edits, {})
            self.assertEqual(self.game_date(), "2028-07-01")
            self.assertTrue(db.journal[0].changes[0].in_game)
        finally:
            db.close()

    def test_an_unchanged_or_impossible_date_is_reported_not_journaled(self):
        stored, message = self.db.set_date(
            PULISIC_UID, "contract_end_date", "2027-06-30")
        self.assertTrue(stored)
        self.assertIn("already", message)
        self.assertEqual(self.db.journal, [])

        for bad_column in ("contract_start_date", "joined_club_dat"):
            stored, message = self.db.set_date(
                PULISIC_UID, bad_column, "2028-01-31")
            self.assertFalse(stored)
            self.assertTrue(message)
        stored, message = self.db.set_date(
            PULISIC_UID, "contract_end_date", "not-a-date")
        self.assertFalse(stored)
        self.assertTrue(message)
        self.assertIsNone(self.db.player(987654321))
        stored, message = self.db.set_date(
            987654321, "contract_end_date", "2028-01-31")
        self.assertFalse(stored)
        self.assertIn("not in this snapshot", message)
        self.assertEqual(self.db.journal, [])
        self.assertEqual(self.db.history, [])


# ---------------------------------------------------------------------------
# D4.1: flushing several offline edits to one field
# ---------------------------------------------------------------------------


class FlushedChainTests(unittest.TestCase):
    """Two staged edits to one field stay two honest journal entries."""

    def setUp(self):
        self.game, self.editor = make_session()
        self.db = Snapshot()

    def tearDown(self):
        self.db.close()

    def dribbling(self) -> int:
        return self.editor.read_player(PULISIC_UID).values["dribbling"]

    def changes(self):
        return [change for action in self.db.journal
                for change in action.changes]

    def test_the_second_edit_keeps_its_own_new_value(self):
        player = self.db.player(PULISIC_UID)
        self.db.set_value("players", player, "dribbling", 18)
        self.db.set_value("players", player, "dribbling", 20)
        self.assertEqual([(change.old, change.new)
                          for change in self.changes()],
                         [(16, 18), (18, 20)])
        # One flush writes the net value only.
        self.db.attach_live(self.editor)
        self.assertEqual(self.db.last_flush, (1, 0))
        self.assertEqual(self.dribbling(), 20)
        # What the Edit History page shows: the values that were staged, not
        # "-> 20" twice (the bug this pins).
        self.assertEqual([row[2] for row in self.db.history], [18, 20])
        self.assertEqual([change.new for change in self.changes()], [18, 20])
        first, last = self.changes()
        self.assertFalse(first.in_game)
        self.assertTrue(first.superseded)   # the game holds its value too
        self.assertTrue(last.in_game)
        self.assertFalse(last.superseded)

    def test_undo_and_redo_walk_the_whole_chain_through_the_game(self):
        player = self.db.player(PULISIC_UID)
        self.db.set_value("players", player, "dribbling", 18)
        self.db.set_value("players", player, "dribbling", 20)
        self.db.attach_live(self.editor)

        self.db.undo()
        self.assertEqual(self.dribbling(), 18)
        self.db.undo()
        self.assertEqual(self.dribbling(), 16)
        self.assertEqual(self.db.player(PULISIC_UID)["dribbling"], 16)
        self.assertEqual(self.db.journal, [])
        self.assertEqual(len(self.db.redo_stack), 2)

        self.db.redo()
        self.assertEqual(self.dribbling(), 18)
        self.db.redo()
        self.assertEqual(self.dribbling(), 20)
        self.assertEqual(self.db.player(PULISIC_UID)["dribbling"], 20)
        self.assertEqual(self.db.redo_stack, [])

    def test_a_single_flushed_edit_is_unchanged(self):
        player = self.db.player(PULISIC_UID)
        self.db.set_value("players", player, "dribbling", 19)
        self.db.attach_live(self.editor)
        change = self.changes()[0]
        self.assertTrue(change.in_game)
        self.assertFalse(change.superseded)
        self.assertEqual((change.old, change.new), (16, 19))


# ---------------------------------------------------------------------------
# D4.2: a refused replay must not move the stacks
# ---------------------------------------------------------------------------


class RefusedReplayTests(unittest.TestCase):
    """A write the game refuses leaves the journal where it was."""

    def setUp(self):
        self.game, self.editor = make_session()
        self.db = Snapshot()
        self.db.attach_live(self.editor, records=[live_record()])
        self.player = self.db.player(PULISIC_UID)

    def tearDown(self):
        self.db.close()

    def dribbling(self) -> int:
        return self.editor.read_player(PULISIC_UID).values["dribbling"]

    def test_a_refused_undo_keeps_the_action_for_the_next_try(self):
        self.db.set_value("players", self.player, "dribbling", 19)
        self.assertEqual(self.dribbling(), 19)
        self.game.refuse_writes = True
        result = self.db.undo()
        self.assertIsNotNone(result)
        self.assertEqual(self.db.last_edit, "error")
        self.assertIn("Windows would not let", self.db.last_error)
        self.assertEqual(len(self.db.journal), 1)     # still undoable
        self.assertEqual(self.db.redo_stack, [])      # and not "undone"
        self.assertEqual(self.dribbling(), 19)        # the game kept it
        # The next Undo acts on the same action, and now it works.
        self.game.refuse_writes = False
        self.db.undo()
        self.assertEqual(self.db.last_edit, "game")
        self.assertEqual(self.db.journal, [])
        self.assertEqual(len(self.db.redo_stack), 1)
        self.assertEqual(self.dribbling(), 16)

    def test_a_refused_redo_keeps_the_action_for_the_next_try(self):
        self.db.set_value("players", self.player, "dribbling", 19)
        self.db.undo()
        self.assertEqual(self.dribbling(), 16)
        self.assertEqual(self.db.journal, [])
        self.game.refuse_writes = True
        result = self.db.redo()
        self.assertIsNotNone(result)
        self.assertEqual(self.db.last_edit, "error")
        self.assertEqual(self.db.journal, [])
        self.assertEqual(len(self.db.redo_stack), 1)
        self.assertEqual(self.dribbling(), 16)
        self.game.refuse_writes = False
        self.db.redo()
        self.assertEqual(self.db.last_edit, "game")
        self.assertEqual(len(self.db.journal), 1)
        self.assertEqual(self.db.redo_stack, [])
        self.assertEqual(self.dribbling(), 19)


# ---------------------------------------------------------------------------
# D4.3: handles, and reloading without losing the connection
# ---------------------------------------------------------------------------


class HandleLeakTests(unittest.TestCase):
    """One attach owns one process handle, and letting go closes it."""

    def test_a_second_attach_closes_the_first_handle(self):
        first_game, first = make_session()
        second_game, second = make_session()
        db = Snapshot()
        try:
            db.attach_live(first)
            self.assertFalse(first_game.store.closed)
            db.attach_live(second)
            self.assertTrue(first_game.store.closed)
            self.assertIs(db.editor, second)
            self.assertFalse(second_game.store.closed)
            self.assertFalse(db.read_only)
        finally:
            db.detach_live()          # closes the second handle too
            self.assertTrue(second_game.store.closed)
            db.conn.close()

    def test_detach_closes_the_handle_and_is_safe_twice(self):
        game, editor = make_session()
        db = Snapshot()
        try:
            db.attach_live(editor)
            db.detach_live()
            self.assertTrue(game.store.closed)
            self.assertIsNone(db.editor)
            db.detach_live()            # no handle left to close
            self.assertIsNone(db.editor)
        finally:
            db.conn.close()

    def test_reload_without_the_live_session_closes_it(self):
        game, editor = make_session()
        db = Snapshot()
        try:
            db.attach_live(editor)
            db.reload(keep_live=False)
            self.assertTrue(game.store.closed)
            self.assertIsNone(db.editor)
            self.assertEqual(db.live, {})
            self.assertEqual(db.counts()["players"], 42344)
        finally:
            db.conn.close()

    def test_reload_keeps_the_handle_it_keeps_the_session_for(self):
        game, editor = make_session()
        db = Snapshot()
        try:
            db.attach_live(editor)
            db.reload()
            self.assertFalse(game.store.closed)   # still attached and usable
            self.assertIs(db.editor, editor)
            self.assertEqual(editor.read_player(PULISIC_UID).values["ca"], 158)
        finally:
            db.conn.close()

    def test_a_failed_reload_keeps_the_old_connection_usable(self):
        import shutil
        directory = Snapshot.create_reload_dir(prefix="test-reload-")
        bad = os.path.join(directory, "not-a-snapshot.db")
        with open(bad, "wb") as handle:
            handle.write(b"this is not a sqlite database" * 64)
        db = Snapshot()
        old_conn = db.conn
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.reload(bad)
            # Nothing was swapped: the object is still the save it was.
            self.assertIs(db.conn, old_conn)
            self.assertEqual(db.path, DB_PATH)
            self.assertEqual(db.counts()["players"], 42344)
            self.assertEqual(db.meta()["game_date"], "2026-07-20")
            self.assertEqual(db.player(PULISIC_UID)["name"],
                             "Christian Mate Pulisic")
        finally:
            db.close()
            shutil.rmtree(directory, ignore_errors=True)

    def test_a_good_reload_closes_the_connection_it_replaced(self):
        db = Snapshot()
        old_conn = db.conn
        try:
            db.reload()
            self.assertIsNot(db.conn, old_conn)
            self.assertEqual(db.counts()["players"], 42344)
            with self.assertRaises(sqlite3.ProgrammingError):
                old_conn.execute("SELECT 1").fetchone()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# D4 (extra): an undo must take the stale live value with it
# ---------------------------------------------------------------------------


class StaleOverlayTests(unittest.TestCase):
    """The screen goes back when an undo restores the database's own value."""

    def test_an_undo_that_restores_the_database_value_drops_the_live_one(self):
        _game, editor = make_session()
        db = Snapshot()
        try:
            db.attach_live(editor)
            player = db.player(PULISIC_UID)
            db.set_value("players", player, "dribbling", 19)
            self.assertEqual(db.player(PULISIC_UID)["dribbling"], 19)
            # The handle goes away (the game closed) without detach_live, so
            # the overlay still holds the value that was written.
            db.editor = None
            result = db.undo()
            self.assertIsNotNone(result)
            self.assertEqual(db.last_edit, "error")
            self.assertIn("not attached", db.last_error)
            # The screen used to keep 19: the edits entry was removed and the
            # live overlay was left behind.
            self.assertEqual(db.player(PULISIC_UID)["dribbling"], 16)
        finally:
            db.conn.close()

    def test_an_undo_without_the_game_puts_the_injury_row_back(self):
        _game, editor = make_session(with_injury=True)
        db = Snapshot()
        try:
            db.attach_live(editor)
            player = db.player(PULISIC_UID)
            db.refresh_injury(player)      # read the game's injury into the row
            self.assertEqual(player["injured"], 1)
            healed, message = db.heal_player(player)
            self.assertTrue(healed, message)
            self.assertEqual(db.player(PULISIC_UID)["injured"], 0)
            db.detach_live()               # the game is gone, overlay cleared
            db.undo()
            self.assertEqual(db.last_edit, "error")
            self.assertIn("not attached", db.last_error)
            row = db.player(PULISIC_UID)
            self.assertEqual(row["injured"], 1)
            self.assertEqual(row["injury"], "Hamstring strain")
        finally:
            db.conn.close()


# ---------------------------------------------------------------------------
# D6: the live domain survives the round trip
# ---------------------------------------------------------------------------


class LiveValueRoundTripTests(unittest.TestCase):
    """A value the game reports is a value the person can edit and undo."""

    def setUp(self):
        self.game, self.editor = make_session()
        self.db = Snapshot()
        self.db.attach_live(self.editor, records=[live_record(morale=80)])
        self.player = self.db.player(PULISIC_UID)

    def tearDown(self):
        self.db.close()

    def game_values(self) -> dict:
        return self.editor.read_player(PULISIC_UID).values

    def test_the_screen_shows_the_live_value(self):
        self.assertEqual(self.player["morale"], 80)

    def test_the_value_the_screen_shows_reaches_the_game_unchanged(self):
        # The snapshot's own domain is still 0..20 ...
        self.assertEqual(spec.clamp("morale", 80), 20)
        self.assertEqual(spec.range_for("morale"), (0, 20))
        # ... and a value on its way into the game is not clamped with it: the
        # live writer's 0..100 table owns that, so 75 stays 75 (it used to be
        # clamped to 20 first, and the game was handed 20).
        changed = self.db.set_value("players", self.player, "morale", 75)
        self.assertTrue(changed)
        self.assertEqual(self.game_values()["morale"], 75)
        self.assertEqual(self.db.player(PULISIC_UID)["morale"], 75)
        self.assertEqual(self.db.last_value, 75)
        change = self.db.journal[-1].changes[0]
        self.assertEqual((change.old, change.new), (80, 75))

    def test_undo_puts_the_games_own_value_back(self):
        self.db.set_value("players", self.player, "morale", 75)
        self.db.undo()
        self.assertEqual(self.game_values()["morale"], 80)
        self.assertEqual(self.db.player(PULISIC_UID)["morale"], 80)

    def test_a_position_edit_stores_the_clamped_value_not_the_typed_one(self):
        changed = self.db.set_value("players", self.player, "striker", 99)
        self.assertTrue(changed)
        # What the page draws comes from the data layer, so it reads 20 and
        # the game holds 20 (the page used to show the 99 that was typed).
        self.assertEqual(self.db.last_value, 20)
        self.assertEqual(self.player["striker"], 20)
        self.assertEqual(self.game_values()["striker"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
