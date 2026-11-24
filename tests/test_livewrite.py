"""Tests for the live write path, the tabs and the interface scale.

The write path is tested against a fake game: the real FM27 decoder
(``fmmemory.FmMemory``) over an ``InMemoryProcessMemory``, so the person
lookup, the attribute block, the injury chain, the read-back verification and
the write gate are the production code - only the process is fake.  That
exercises the offsets, the two storage scales, the verification read-back and
the undo without needing Football Manager to be running.
"""

from __future__ import annotations

import struct
import unittest
from datetime import date as _date

from fmrte import memmap as M
from fmrte.fmmemory import BLOCK_BEFORE, BLOCK_SIZE, FmMemory, TABLE_VECTOR_PTR
from fmrte.livewrite import (EditError, LiveEditor, MAX_UNDO_STEPS, RANGES,
                             _read_date)
from fmrte.procmem import InMemoryProcessMemory


class FakeGame(FmMemory):
    """A stand-in for the attached game: the real decoder over fake memory.

    It is the production class with the bytes swapped out, so ``read``,
    ``write`` (the one write gate), ``read_injury`` and ``write_value`` are the
    real implementations.  ``refuse_writes`` models a process that accepts the
    handle but refuses the write, which is what Windows does to a game running
    at a higher integrity level unless the handle has the write rights.
    """

    def __init__(self, can_write: bool = True):
        self.store = InMemoryProcessMemory(pid=1, can_write=can_write)
        super().__init__(self.store)
        self.refuse_writes = False

    def put(self, address: int, data: bytes) -> None:
        self.store.put(address, data)

    @property
    def writes(self) -> int:
        return self.store.writes

    def write(self, address: int, data: bytes) -> bool:
        if self.refuse_writes:
            return False
        return super().write(address, data)


PERSON = 0x100000
UID = 92074020


def build_person(game: FakeGame, uid: int = UID) -> None:
    """Lay out one player record the way the game does."""
    # The 0x400-byte block that starts at person - 0x1A0.
    block = bytearray(0x400)
    base = 0x1A0  # index of the person pointer inside the block

    def put(rel: int, fmt: str, value) -> None:
        struct.pack_into(fmt, block, base + rel, value)

    put(12, "<I", uid)
    put(-36, "<h", 158)
    put(-34, "<h", 163)
    put(-44, "<H", 9417)
    put(-48, "<H", 3500)
    put(-46, "<h", -125)
    put(-28, "<B", 14)
    put(-42, "<H", 7800)
    put(-40, "<H", 7900)
    put(-38, "<H", 6950)
    put(-84, "<I", 48900392)
    put(-90, "<B", 178)
    put(168, "<Q", 0x200000)

    # The attribute block: 71 bytes at person - 314, indexed at offset - 40.
    block_start = base + M.ATTRIBUTE_BLOCK_REL
    for column, offset in M.ATTRIBUTE_OFFSETS.items():
        index = block_start + offset - M.ATTRIBUTE_INDEX_BASE
        if column in M.POSITION_COLUMNS:
            block[index] = 16
        else:
            block[index] = 80  # shown as 16

    game.put(PERSON - 0x1A0, bytes(block))

    # The registration record.
    registration = bytearray(120)
    struct.pack_into("<I", registration, M.SALARY_WEEKLY_REL, 84746)
    struct.pack_into("<HH", registration, M.CONTRACT_DATE_REL, 181, 2027)
    struct.pack_into("<HH", registration, M.JOINED_DATE_REL, 194, 2023)
    registration[M.TRANSFER_STATUS_REL] = 0
    game.put(0x200000, bytes(registration))


INJURY_REFERENCE = 0x300000
INJURY_ITEM = 0x310000
INJURY_TYPE = 0x320000


def build_injury(game: FakeGame, name: str = "Hamstring strain") -> None:
    """Lay out the injury chain the game stores behind the player's pointer.

    person - 0x190 -> reference; reference[0] -> item; item carries the type
    pointer, the start date and the two day counts. This is the same chain
    ``PlayerExporter.injuryStatus`` walks.
    """
    game.put(PERSON + M.INJURY_REFERENCE_REL,
             struct.pack("<Q", INJURY_REFERENCE))
    game.put(INJURY_REFERENCE, struct.pack("<Q", INJURY_ITEM))
    item = bytearray(0x40)
    struct.pack_into("<Q", item, M.INJURY_TYPE_REL, INJURY_TYPE)
    struct.pack_into("<H", item, M.INJURY_DAY_REL, 201)   # 2026-07-20
    struct.pack_into("<H", item, M.INJURY_YEAR_REL, 2026)
    struct.pack_into("<H", item, M.INJURY_FULL_TRAINING_REL, 21)
    struct.pack_into("<H", item, M.INJURY_LIGHT_TRAINING_REL, 9)
    game.put(INJURY_ITEM, bytes(item))
    encoded = name.encode("utf-8")
    game.put(INJURY_TYPE + M.INJURY_TYPE_NAME_REL,
             struct.pack("<I", len(encoded)) + encoded)


def connected(game: FakeGame, uid: int = UID) -> FakeGame:
    """Point the fake's person lookup at the one record it laid out."""
    game.find_person = lambda wanted: PERSON if wanted == uid else None
    return game


class FakeEditor(LiveEditor):
    """A LiveEditor whose person lookup is the fake record."""

    def __init__(self, game: FakeGame, uid: int = UID):
        super().__init__(game)
        self.uid = uid

    def read_player(self, unique_id):
        if unique_id != self.uid:
            return None
        return super().read_player(unique_id)

    def find(self, unique_id):
        return PERSON if unique_id == self.uid else None


class LiveWriteTests(unittest.TestCase):
    def setUp(self):
        self.game = FakeGame()
        build_person(self.game)
        self.editor = FakeEditor(self.game)
        # find_person lives on the attached game; give the fake one.
        connected(self.game)

    def test_reads_the_record_the_way_the_game_stores_it(self):
        record = self.editor.read_player(UID)
        self.assertIsNotNone(record)
        self.assertEqual(record.values["ca"], 158)
        self.assertEqual(record.values["pa"], 163)
        self.assertEqual(record.values["condition"], 9417)
        self.assertEqual(record.values["match_sharpness"], 3500)
        self.assertEqual(record.values["jadedness"], -125)
        self.assertEqual(record.values["morale"], 14)
        self.assertEqual(record.values["height_cm"], 178)
        self.assertEqual(record.values["asking_price_raw"], 48900392)
        self.assertEqual(record.values["salary_weekly_raw"], 84746)
        self.assertEqual(record.values["contract_end_date"], "2027-06-30")
        self.assertEqual(record.values["joined_club_date"], "2023-07-13")

    def test_visible_attributes_are_stored_at_five_times_the_shown_value(self):
        record = self.editor.read_player(UID)
        self.assertEqual(record.values["dribbling"], 16)
        address = (PERSON + M.ATTRIBUTE_BLOCK_REL
                   + M.ATTRIBUTE_OFFSETS["dribbling"] - M.ATTRIBUTE_INDEX_BASE)
        self.assertEqual(self.game.read(address, 1)[0], 80)

    def test_positions_are_stored_one_to_one(self):
        record = self.editor.read_player(UID)
        self.assertEqual(record.values["striker"], 16)
        address = (PERSON + M.ATTRIBUTE_BLOCK_REL
                   + M.ATTRIBUTE_OFFSETS["striker"] - M.ATTRIBUTE_INDEX_BASE)
        self.assertEqual(self.game.read(address, 1)[0], 16)

    def test_writing_an_attribute_scales_it_and_reads_it_back(self):
        _record, applied = self.editor.write(UID, "dribbling", 18, "Dribbling")
        self.assertEqual(applied, 18)
        address = (PERSON + M.ATTRIBUTE_BLOCK_REL
                   + M.ATTRIBUTE_OFFSETS["dribbling"] - M.ATTRIBUTE_INDEX_BASE)
        self.assertEqual(self.game.read(address, 1)[0], 90)
        self.assertEqual(self.editor.read_player(UID).values["dribbling"], 18)

    def test_writing_a_position_does_not_scale_it(self):
        self.editor.write(UID, "striker", 20, "Striker")
        address = (PERSON + M.ATTRIBUTE_BLOCK_REL
                   + M.ATTRIBUTE_OFFSETS["striker"] - M.ATTRIBUTE_INDEX_BASE)
        self.assertEqual(self.game.read(address, 1)[0], 20)

    def test_writing_ability_uses_the_two_byte_field(self):
        self.editor.write(UID, "ca", 200, "Current ability")
        self.assertEqual(self.editor.read_player(UID).values["ca"], 200)

    def test_writing_condition_uses_the_10000_ceiling(self):
        self.editor.write(UID, "condition", 10000, "Condition")
        self.assertEqual(self.editor.read_player(UID).values["condition"], 10000)
        # A value above the ceiling is clamped, not written raw.
        _record, applied = self.editor.write(UID, "condition", 20000, "Condition")
        self.assertEqual(applied, 10000)

    def test_jadedness_is_signed(self):
        self.editor.write(UID, "jadedness", -500, "Jadedness")
        self.assertEqual(self.editor.read_player(UID).values["jadedness"], -500)

    def test_writing_the_weekly_wage(self):
        self.editor.write(UID, "salary_weekly_raw", 100000, "Wages")
        self.assertEqual(
            self.editor.read_player(UID).values["salary_weekly_raw"], 100000)

    def test_a_refused_write_raises_and_leaves_the_value_alone(self):
        self.game.refuse_writes = True
        with self.assertRaises(EditError):
            self.editor.write(UID, "ca", 100, "Current ability")
        self.assertEqual(self.editor.read_player(UID).values["ca"], 158)

    def test_undo_puts_the_old_bytes_back(self):
        self.editor.write(UID, "dribbling", 20, "Dribbling")
        self.assertEqual(self.editor.read_player(UID).values["dribbling"], 20)
        label = self.editor.undo()
        self.assertIn("Dribbling", label)
        self.assertEqual(self.editor.read_player(UID).values["dribbling"], 16)

    def test_an_unknown_player_is_refused(self):
        with self.assertRaises(EditError):
            self.editor.write(1, "ca", 100, "Current ability")

    def test_a_field_with_no_offset_is_refused(self):
        with self.assertRaises(EditError):
            self.editor.write(UID, "club", 1, "Club")

    def test_every_range_matches_the_original_clamp_helper(self):
        self.assertEqual(RANGES["ca"], (1, 200))
        self.assertEqual(RANGES["pa"], (1, 200))
        self.assertEqual(RANGES["condition"], (0, 10000))
        self.assertEqual(RANGES["match_sharpness"], (0, 10000))
        self.assertEqual(RANGES["morale"], (0, 100))

    def test_date_decoding(self):
        raw = struct.pack("<HH", 181, 2027)
        self.assertEqual(_read_date(raw, 0), "2027-06-30")
        raw = struct.pack("<HH", 194, 2023)
        self.assertEqual(_read_date(raw, 0), "2023-07-13")

    def test_a_nonsense_date_is_blank(self):
        raw = struct.pack("<HH", 0, 0)
        self.assertEqual(_read_date(raw, 0), "")


class InjuryTests(unittest.TestCase):
    """Heal has to clear the injury record, not just the numbers.

    The original's Heal preset writes ``IsInjured = false``. In the game that
    flag is the injury reference, so a heal that only writes condition and
    sharpness leaves the player injured - which is exactly what the person
    reported.
    """

    def setUp(self):
        self.game = FakeGame()
        build_person(self.game)
        build_injury(self.game)
        connected(self.game)
        self.editor = FakeEditor(self.game)

    def test_the_injury_is_read_from_the_pointer_chain(self):
        injury = self.editor.read_injury(UID)
        self.assertIsNotNone(injury)
        self.assertTrue(injury.injured)
        self.assertEqual(injury.name, "Hamstring strain")
        self.assertEqual(injury.start_date, "2026-07-20")
        self.assertEqual(injury.full_training_days, 21)
        self.assertEqual(injury.light_training_days, 9)

    def test_the_record_carries_the_reference(self):
        record = self.editor.read_player(UID)
        self.assertEqual(record.injury_reference, INJURY_REFERENCE)

    def test_heal_zeroes_the_reference_and_keeps_the_item(self):
        cleared = self.editor.heal(UID)
        self.assertEqual(cleared.name, "Hamstring strain")
        self.assertEqual(
            self.game.read(PERSON + M.INJURY_REFERENCE_REL, 8), b"\x00" * 8)
        # The item itself is untouched, so the history is not destroyed.
        self.assertIsNotNone(self.game.read(INJURY_ITEM, 0x40))
        self.assertFalse(self.editor.read_injury(UID).injured)

    def test_heal_is_undoable(self):
        self.editor.heal(UID)
        label = self.editor.undo()
        self.assertIn("injury", label)
        self.assertEqual(
            self.game.read(PERSON + M.INJURY_REFERENCE_REL, 8),
            struct.pack("<Q", INJURY_REFERENCE))
        self.assertTrue(self.editor.read_injury(UID).injured)

    def test_healing_a_fit_player_changes_nothing(self):
        self.editor.heal(UID)
        writes = self.game.writes
        self.editor.heal(UID)
        self.assertEqual(self.game.writes, writes)

    def test_a_refused_write_puts_the_reference_back(self):
        self.game.refuse_writes = True
        with self.assertRaises(EditError):
            self.editor.heal(UID)
        self.assertEqual(
            self.game.read(PERSON + M.INJURY_REFERENCE_REL, 8),
            struct.pack("<Q", INJURY_REFERENCE))

    def test_a_fit_player_reads_as_not_injured(self):
        self.game.put(PERSON + M.INJURY_REFERENCE_REL, b"\x00" * 8)
        injury = self.editor.read_injury(UID)
        self.assertIsNotNone(injury)
        self.assertFalse(injury.injured)


class ReadOnlyAttachTests(unittest.TestCase):
    """A read-only attach must still read; only writes may refuse.

    Windows refuses the write handle whenever the game runs at a higher
    integrity level than this editor, which is the normal case for the Xbox /
    Game Pass build.  The editor then attaches read-only, and everything
    readable has to keep working, because live values and live injuries are
    display data.

    This used to be wrong: ``read_live_values`` and ``read_injury`` opened with
    an ``if self.read_only: raise EditError(...)`` guard, so a read-only attach
    could not show live injury data at all, and ``db.refresh_injury`` swallowed
    the error - the player screen quietly kept the snapshot's stale values.
    These tests fail if that guard ever comes back.
    """

    def setUp(self):
        self.game = FakeGame(can_write=False)
        build_person(self.game)
        build_injury(self.game)
        connected(self.game)
        self.editor = FakeEditor(self.game)

    def test_the_attach_is_read_only_because_the_handle_has_no_write_rights(self):
        self.assertFalse(self.game.can_write)
        self.assertTrue(self.editor.read_only)

    def test_read_live_values_still_works_read_only(self):
        values = self.editor.read_live_values(UID)
        self.assertIsNotNone(values)
        self.assertEqual(values["ca"], 158)
        self.assertEqual(values["condition"], 9417)
        self.assertEqual(values["dribbling"], 16)

    def test_read_injury_still_works_read_only(self):
        injury = self.editor.read_injury(UID)
        self.assertIsNotNone(injury)
        self.assertTrue(injury.injured)
        self.assertEqual(injury.name, "Hamstring strain")
        self.assertEqual(injury.start_date, "2026-07-20")

    def test_reading_never_writes_a_byte(self):
        self.editor.read_live_values(UID)
        self.editor.read_player(UID)
        self.editor.read_injury(UID)
        self.assertEqual(self.game.writes, 0)

    def test_write_refuses_and_writes_nothing(self):
        with self.assertRaises(EditError) as caught:
            self.editor.write(UID, "ca", 100, "Current ability")
        self.assertIn("read-only", str(caught.exception))
        self.assertEqual(self.game.writes, 0)
        self.assertEqual(self.editor.read_player(UID).values["ca"], 158)

    def test_write_date_refuses(self):
        with self.assertRaises(EditError):
            self.editor.write_date(
                UID, "contract_end_date", _date(2030, 6, 30), "Contract end")
        self.assertEqual(self.game.writes, 0)

    def test_heal_refuses_and_leaves_the_injury_attached(self):
        with self.assertRaises(EditError):
            self.editor.heal(UID)
        self.assertEqual(self.game.writes, 0)
        self.assertTrue(self.editor.read_injury(UID).injured)

    def test_restore_injury_refuses(self):
        with self.assertRaises(EditError):
            self.editor.restore_injury(UID, INJURY_REFERENCE)
        self.assertEqual(self.game.writes, 0)

    def test_the_write_gate_itself_refuses(self):
        """Even a caller that forgets the guard cannot write."""
        self.assertFalse(self.game.write(PERSON - 0xA0, b"\xff"))
        self.assertEqual(self.game.writes, 0)

    def test_undo_with_nothing_recorded_is_a_no_op(self):
        # Undo must not raise on a read-only attach: db.undo() calls it first
        # and falls back to the local undo when it returns None.
        self.assertIsNone(self.editor.undo())
        self.assertEqual(self.game.writes, 0)


class ReadOnlyRefreshTests(unittest.TestCase):
    """The screen-level consequence of the read-only fix.

    ``db.refresh_injury`` catches every error from ``read_injury`` and leaves
    the row untouched, so on a read-only attach the player screen used to keep
    showing the snapshot's stale injury with no error anywhere.  This pins the
    path the screen actually takes.
    """

    def test_the_injury_row_is_refreshed_from_a_read_only_attach(self):
        from fmrte.db import Snapshot

        game = FakeGame(can_write=False)
        build_person(game)
        build_injury(game)
        connected(game)
        db = Snapshot()
        db.editor = FakeEditor(game)
        row = {"unique_id": UID}

        db.refresh_injury(row)

        self.assertEqual(row["injured"], 1)
        self.assertEqual(row["injury"], "Hamstring strain")
        self.assertEqual(row["injury_start_date"], "2026-07-20")
        self.assertEqual(row["injury_full_training_days_remaining"], 21)
        self.assertEqual(row["injury_light_training_days_remaining"], 9)


# ---------------------------------------------------------------------------
# read_many: the progress callback is a batch report, not a per-player one
# ---------------------------------------------------------------------------

# The people-vector length of the shipped snapshot's save, so the cadence the
# Load Game dialog sees is measured against the real number.
PEOPLE_COUNT = 42344
TABLE_AT = 0x400000
COLLECTION_AT = 0x401000
VECTOR_AT = 0x402000
ENTRIES_AT = 0x600000


def lay_out_person(game: FakeGame, person: int, uid: int) -> None:
    """The block the game keeps one person's scalars in, around ``person``."""
    block = bytearray(BLOCK_SIZE)
    struct.pack_into("<I", block, BLOCK_BEFORE + 12, uid)
    struct.pack_into("<h", block, BLOCK_BEFORE - 36, 120)
    struct.pack_into("<h", block, BLOCK_BEFORE - 34, 130)
    struct.pack_into("<H", block, BLOCK_BEFORE - 44, 9000)
    game.put(person - BLOCK_BEFORE, bytes(block))


def lay_out_people(game: FakeGame, count: int = PEOPLE_COUNT,
                   uids: dict[int, int] | None = None) -> list[int]:
    """The people vector, with a decodable person behind ``uids``' indices.

    Every other entry is a null pointer, which ``read_many`` skips without a
    read - so the test measures the callback cadence, not how fast the vector
    can be walked.
    """
    uids = dict(uids or {})
    pointers = [0] * count
    for index, uid in uids.items():
        pointers[index] = 0x800000 + index * 0x1000
        lay_out_person(game, pointers[index], uid)
    game.put(TABLE_AT + M.SLOTS["PeopleOffset"],
             struct.pack("<Q", COLLECTION_AT))
    game.put(COLLECTION_AT + TABLE_VECTOR_PTR, struct.pack("<Q", VECTOR_AT))
    game.put(VECTOR_AT, struct.pack("<QQ", ENTRIES_AT, ENTRIES_AT + count * 8))
    game.store.map(ENTRIES_AT, data=struct.pack(f"<{count}Q", *pointers))
    game.table_base = TABLE_AT
    return pointers


class ReadManyProgressTests(unittest.TestCase):
    """How ``read_many`` reports progress, and what it does with no callback.

    Two real defects are pinned here.  The batching block already reported
    progress every 5,000 pointers, but a second, unconditional call sat inside
    the per-player branch: with the default ``progress=None`` it raised
    ``TypeError: 'NoneType' object is not callable`` on the first player, and
    from the Load Game dialog it queued one Tk message per player instead of
    one per 5,000.
    """

    def setUp(self):
        self.game = FakeGame()
        # A player behind every 500th index, so a per-player message would be
        # visible: 85 matched players on a 42,344-entry vector.
        self.uids = {index: 900 + index
                     for index in range(0, PEOPLE_COUNT, 500)}
        self.pointers = lay_out_people(self.game, uids=self.uids)
        self.editor = LiveEditor(self.game)

    def test_read_many_reads_with_no_progress_callback(self):
        """``progress`` is optional: the default must simply read."""
        found = self.editor.read_many(set(self.uids.values()), progress=None)
        self.assertEqual(len(found), len(self.uids))
        self.assertEqual(found[900].ca, 120)
        self.assertEqual(found[900].condition, 9000)

    def test_progress_is_reported_per_5000_players_not_per_player(self):
        calls: list[tuple[str, int, int]] = []
        found = self.editor.read_many(
            set(self.uids.values()),
            progress=lambda label, index, count: calls.append(
                (label, index, count)))
        self.assertEqual(len(found), len(self.uids))
        expected = [(index, PEOPLE_COUNT)
                    for index in range(0, PEOPLE_COUNT, 5000)]
        expected.append((PEOPLE_COUNT, PEOPLE_COUNT))
        self.assertEqual([(index, count) for _l, index, count in calls], expected)
        self.assertEqual({label for label, _i, _c in calls}, {"Reading players"})
        # Ten messages, not 42,344: the 85 matched players added none.
        self.assertEqual(len(calls), 10)
        self.assertLess(len(calls) * 1000, PEOPLE_COUNT)


class UndoStackTests(unittest.TestCase):
    """The editor's own undo is a bounded fallback, not an endless log.

    ``db.Snapshot``'s journal is the undo authority now; the editor's stack only
    backs ``LiveEditor.undo()``, and it used to grow by one entry per write for
    the life of the session.
    """

    def setUp(self):
        self.game = FakeGame()
        build_person(self.game)
        connected(self.game)
        self.editor = FakeEditor(self.game)

    def test_the_stack_stops_at_the_cap_and_undo_still_works(self):
        writes = MAX_UNDO_STEPS + 10
        for step in range(writes):
            self.editor.write(UID, "dribbling", step % 20 + 1, "Dribbling")
        self.assertEqual(len(self.editor.undo_stack), MAX_UNDO_STEPS)
        # Undo still reaches the newest write, so it puts back the value the
        # write before it left behind.
        previous = (writes - 2) % 20 + 1
        label = self.editor.undo()
        self.assertIn("Dribbling", label)
        self.assertEqual(self.editor.read_player(UID).values["dribbling"],
                         previous)
        self.assertEqual(len(self.editor.undo_stack), MAX_UNDO_STEPS - 1)

    def test_one_write_is_still_one_undo(self):
        self.editor.write(UID, "dribbling", 20, "Dribbling")
        self.assertEqual(len(self.editor.undo_stack), 1)
        self.editor.undo()
        self.assertEqual(self.editor.undo_stack, [])


class MemoryMapTests(unittest.TestCase):
    def test_the_layout_matches_the_observed_offsets(self):
        self.assertEqual(M.DIRECT_FIELDS["ca"], (-36, "<h"))
        self.assertEqual(M.DIRECT_FIELDS["pa"], (-34, "<h"))
        self.assertEqual(M.DIRECT_FIELDS["condition"], (-44, "<H"))
        self.assertEqual(M.DIRECT_FIELDS["match_sharpness"], (-48, "<H"))
        self.assertEqual(M.DIRECT_FIELDS["jadedness"], (-46, "<h"))
        self.assertEqual(M.DIRECT_FIELDS["morale"], (-28, "<B"))
        self.assertEqual(M.ATTRIBUTE_BLOCK_REL, -314)
        self.assertEqual(M.ATTRIBUTE_INDEX_BASE, 40)
        self.assertEqual(M.ATTRIBUTE_STORAGE_SCALE, 5)
        self.assertEqual(M.REGISTRATION_POINTER_REL, 168)
        self.assertEqual(M.SALARY_WEEKLY_REL, 32)
        self.assertEqual(M.CONTRACT_DATE_REL, 72)
        self.assertEqual(M.DATE_DAY_MASK, 511)

    def test_the_attribute_table_has_the_observed_counts(self):
        self.assertEqual(len(M.POSITION_COLUMNS), 14)
        self.assertEqual(len(M.ATTRIBUTE_OFFSETS), 68)
        self.assertEqual(len(M.DIRECT_POSITIVE_FIELDS), 8)

    def test_the_offset_table_slots(self):
        self.assertEqual(M.SLOTS["PeopleOffset"], 336)
        self.assertEqual(M.SLOTS["ContinentOffset"], 264)
        self.assertEqual(M.SLOTS["RegionOffset"], 352)
        self.assertEqual(M.SLOTS["CompetitionOffset"], 256)


if __name__ == "__main__":
    unittest.main()
