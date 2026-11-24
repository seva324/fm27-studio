"""Tests for the FM27 memory module and the process-memory seam.

These are the tests that could not exist before the split.  ``live.py`` used
to own both the Win32 calls and the FM27 layout, so the decoder could only be
driven by subclassing the process object; anything that was not "read a block
and decode it" - the in-game date, the manager's club, the readable-region
scan, the read-only attach - was untested.

Now the decoder talks to :class:`fmrte.procmem.ProcessMemory` only, and
:class:`fmrte.procmem.InMemoryProcessMemory` implements that interface over a
dictionary of byte blocks.  Every test here runs the production decoder over
that adapter, so no game and no Windows handle is involved.
"""

from __future__ import annotations

import ast
import os
import struct
import unittest
from pathlib import Path

from fmrte import fmmemory, live, livewrite, memmap, procmem, spec
from fmrte.fmmemory import (
    BLOCK_BEFORE,
    BUILD_TO_CURRENT_DATE_RVA,
    BUILD_TO_HUMAN_MANAGER_RVA,
    BUILD_TO_TABLE_RVA,
    FmMemory,
    KNOWN_DATE_RVAS,
    KNOWN_HUMAN_MANAGER_RVAS,
    KNOWN_TABLE_RVAS,
    MIN_PEOPLE,
    SLOTS,
    TABLE_TO_CURRENT_DATE_RVA,
    TABLE_TO_HUMAN_MANAGER_RVA,
    TABLE_VECTOR_PTR,
    decode_date,
    decode_record,
)
from fmrte.procmem import InMemoryProcessMemory, LiveError

BASE = 0x140000000

GOOD_COUNTS = {
    "ContinentOffset": 7,
    "RegionOffset": 28,
    "PeopleOffset": 42344,
    "TeamOffset": 60000,
    "ClubOffset": 42129,
    "CompetitionOffset": 6625,
    "NationOffset": 251,
    "CurrencyOffset": 120,
    "CityOffset": 80000,
    "StadiumOffset": 20000,
    "AgreementOffset": 500,
}


def put_u64(memory, address: int, value: int) -> None:
    memory.put(address, struct.pack("<Q", value))


def build_table(memory, table_at: int, counts=None) -> None:
    """Lay out a plausible offset table with one collection per slot.

    The whole table, its eleven collections, their vectors and their entry
    blocks fit in the 64 KiB the caller has mapped at ``table_at``.
    """
    counts = GOOD_COUNTS if counts is None else counts
    next_free = table_at + 0x2000
    for name, offset in SLOTS.items():
        count = counts.get(name, 0)
        collection = next_free
        vector = next_free + 0x100
        entries = next_free + 0x200
        next_free += 0x1000
        put_u64(memory, table_at + offset, collection)
        put_u64(memory, collection + TABLE_VECTOR_PTR, vector)
        put_u64(memory, vector, entries)
        put_u64(memory, vector + 8, entries + count * 8)


def build_date(memory, address: int, day: int, year: int, flags: int = 0) -> None:
    memory.put(address, struct.pack("<HH", day | flags, year))


def build_club_chain(memory, root: int, name: str, allocations: list[int]) -> None:
    """manager -> employment -> team -> club -> UTF-16 length-prefixed name."""
    manager = allocations[0]
    employment = allocations[1]
    team = allocations[2]
    club = allocations[3]
    text = allocations[4]
    put_u64(memory, root, manager)
    put_u64(memory, manager + fmmemory.MANAGER_EMPLOYMENT_REL, employment)
    put_u64(memory, employment + fmmemory.EMPLOYMENT_TEAM_REL, team)
    put_u64(memory, team + fmmemory.TEAM_CLUB_REL, club)
    put_u64(memory, club + fmmemory.CLUB_NAME_RELS[0], text)
    memory.map(text, size=4 + len(name) * 2)
    memory.put(text, struct.pack("<i", len(name)) + name.encode("utf-16-le"))
    return club


def build_people(memory, table_at: int, vector: int, entries: int, count: int,
                 uid_at: dict[int, int] | None = None) -> list[int]:
    """A people vector of ``count`` person pointers (only the named ones exist)."""
    collection = vector - 0x100
    memory.map(vector, size=0x10)
    put_u64(memory, table_at + SLOTS["PeopleOffset"], collection)
    put_u64(memory, collection + TABLE_VECTOR_PTR, vector)
    put_u64(memory, vector, entries)
    put_u64(memory, vector + 8, entries + count * 8)
    pointers = [BASE + 0x300000 + index * 0x100 for index in range(count)]
    memory.map(entries, data=struct.pack(f"<{count}Q", *pointers))
    for index, uid in (uid_at or {}).items():
        memory.put(pointers[index] + fmmemory.REL_UNIQUE_ID,
                   struct.pack("<I", uid))
    return pointers


class RecordingMemory(InMemoryProcessMemory):
    """An adapter that remembers which ranges were asked for."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reads: list[tuple[int, int, bool]] = []

    def read(self, address: int, size: int) -> bytes | None:
        data = super().read(address, size)
        self.reads.append((address, size, data is not None))
        return data


# ---------------------------------------------------------------------------
# The adapter itself
# ---------------------------------------------------------------------------

class InterfaceTests(unittest.TestCase):
    """The seam is an explicit, documented interface with two adapters."""

    def test_the_interface_covers_exactly_what_the_decoder_needs(self):
        for name in ("pid", "module_base", "module_size", "read", "write",
                     "readable_regions", "close", "can_write"):
            self.assertTrue(hasattr(procmem.ProcessMemory, name), name)
        for name in ("read", "write", "readable_regions", "close"):
            self.assertTrue(procmem.ProcessMemory.__dict__[name].__doc__, name)

    def test_the_bare_interface_is_abstract_and_loud(self):
        memory = procmem.ProcessMemory()
        with self.assertRaises(NotImplementedError):
            memory.read(1, 1)
        with self.assertRaises(NotImplementedError):
            memory.write(1, b"x")
        with self.assertRaises(NotImplementedError):
            memory.readable_regions(0, 1)
        with self.assertRaises(NotImplementedError):
            memory.close()
        with self.assertRaises(NotImplementedError):
            memory.can_write

    def test_both_adapters_implement_the_interface(self):
        self.assertIsInstance(InMemoryProcessMemory(), procmem.ProcessMemory)
        self.assertIsInstance(procmem.Win32ProcessMemory(pid=1, handle=0),
                              procmem.ProcessMemory)


class GameBuildLookupTests(unittest.TestCase):
    """``game_build`` reads the version resource of the exe it is handed.

    ``open_game`` already knows that path; without this it called the
    no-argument ``game_build()``, which ran the whole process discovery again
    on every attach.
    """

    def test_a_known_exe_is_not_discovered_again(self):
        calls: list[int] = []
        original = procmem.find_game_process

        def spy():
            calls.append(1)
            return (1, "")

        procmem.find_game_process = spy
        try:
            # A path with no game_plugin.dll beside it: no version resource, and
            # - the point - no second process enumeration.
            self.assertIsNone(procmem.game_build(r"C:\no\such\game\fm.exe"))
        finally:
            procmem.find_game_process = original
        self.assertEqual(calls, [])

    def test_an_empty_exe_still_looks_the_game_up(self):
        if os.name != "nt":
            self.skipTest("game_build reads version resources only on Windows")
        calls: list[int] = []
        original = procmem.find_game_process

        def spy():
            calls.append(1)
            return (1, "")

        procmem.find_game_process = spy
        try:
            self.assertIsNone(procmem.game_build())
        finally:
            procmem.find_game_process = original
        self.assertEqual(calls, [1], "the no-argument form still finds the game")


class InMemoryAdapterTests(unittest.TestCase):
    """The fake must answer exactly like ReadProcessMemory does."""

    def setUp(self):
        self.memory = InMemoryProcessMemory(pid=7, module_base=BASE,
                                           module_size=0x2000)

    def test_read_inside_one_block_succeeds(self):
        self.memory.map(BASE + 0x1000, b"abcdefgh")
        self.assertEqual(self.memory.read(BASE + 0x1000, 4), b"abcd")

    def test_a_read_that_leaves_the_block_returns_none(self):
        """ReadProcessMemory fails the whole range when any byte is missing."""
        self.memory.map(BASE + 0x1000, b"abcdefgh")
        self.assertIsNone(self.memory.read(BASE + 0x1000, 9))
        self.assertIsNone(self.memory.read(BASE + 0x2000, 4))

    def test_write_lands_in_a_writable_block(self):
        self.memory.map(BASE + 0x1000, size=8)
        self.assertTrue(self.memory.write(BASE + 0x1002, b"xy"))
        self.assertEqual(self.memory.read(BASE + 0x1000, 4), b"\x00\x00xy")
        self.assertEqual(self.memory.writes, 1)

    def test_write_refuses_a_read_only_block(self):
        self.memory.map(BASE + 0x1000, size=8, writable=False)
        self.assertFalse(self.memory.write(BASE + 0x1000, b"x"))
        self.assertEqual(self.memory.read(BASE + 0x1000, 2), b"\x00\x00")

    def test_write_refuses_without_write_rights(self):
        memory = InMemoryProcessMemory(module_base=BASE, can_write=False)
        memory.map(BASE + 0x1000, size=8)
        self.assertFalse(memory.write(BASE + 0x1000, b"x"))
        self.assertFalse(memory.can_write)

    def test_readable_regions_are_ascending_and_clipped(self):
        self.memory.map(BASE + 0x1000, size=0x100)
        self.memory.map(BASE + 0x3000, size=0x100, writable=False)
        self.memory.map(BASE + 0x1100, size=0x100)  # adjacent, same flag
        self.assertEqual(
            self.memory.readable_regions(BASE, BASE + 0x4000),
            [(BASE + 0x1000, BASE + 0x1200, True),
             (BASE + 0x3000, BASE + 0x3100, False)])

    def test_a_declared_region_may_be_wider_than_the_mapped_bytes(self):
        """A committed range whose interior read fails: the 64 KiB case."""
        self.memory.map(BASE + 0x1000, size=0x100)
        self.memory.declare_region(BASE + 0x1000, BASE + 0x9000)
        self.assertEqual(self.memory.readable_regions(BASE, BASE + 0xA000),
                         [(BASE + 0x1000, BASE + 0x9000, True)])
        self.assertIsNone(self.memory.read(BASE + 0x1000, 0x2000))

    def test_close_makes_everything_unreadable(self):
        self.memory.map(BASE + 0x1000, size=8)
        self.memory.close()
        self.assertIsNone(self.memory.read(BASE + 0x1000, 4))
        self.assertFalse(self.memory.can_write)
        self.memory.close()  # idempotent


# ---------------------------------------------------------------------------
# The protection a refused write is retried under
# ---------------------------------------------------------------------------

class WriteProtectionTests(unittest.TestCase):
    """A refused write gets the *minimal* writable protection, then it is back.

    ``Win32ProcessMemory.write`` used to ask for ``PAGE_EXECUTE_READWRITE`` on
    every write - data pages included - and to restore the old protection
    without a ``finally``, so an exception in between left the page writable
    *and* executable.  The protect path itself needs a real handle, so the
    decision (:func:`procmem.write_page_protection`) is pure and is tested
    here, and the shape of the write path is checked in the source.
    """

    def test_a_writable_block_needs_no_protection_change(self):
        """The direct write is the normal path, and the fake still takes it."""
        memory = InMemoryProcessMemory(module_base=BASE)
        memory.map(BASE + 0x1000, size=8)
        self.assertTrue(memory.write(BASE + 0x1000, b"\x01\x02"))
        self.assertEqual(memory.read(BASE + 0x1000, 2), b"\x01\x02")
        self.assertEqual(memory.writes, 1, "one direct write, no retry")

    def test_a_data_page_is_asked_for_read_write_only(self):
        self.assertEqual(procmem.write_page_protection(), procmem.PAGE_READWRITE)
        for current in (procmem.PAGE_READONLY, procmem.PAGE_READWRITE,
                        procmem.PAGE_WRITECOPY, procmem.PAGE_NOACCESS,
                        procmem.PAGE_READWRITE | procmem.PAGE_GUARD):
            self.assertEqual(procmem.write_page_protection(current),
                             procmem.PAGE_READWRITE, hex(current))
        # The whole point: a data page never gains the execute bit.
        self.assertNotEqual(procmem.write_page_protection(),
                            procmem.PAGE_EXECUTE_READWRITE)
        self.assertNotIn(procmem.write_page_protection(),
                         procmem.EXECUTABLE_PROTECTIONS)

    def test_an_executing_page_keeps_its_execute_right(self):
        for current in (procmem.PAGE_EXECUTE_READ,
                        procmem.PAGE_EXECUTE_READWRITE,
                        procmem.PAGE_EXECUTE_WRITECOPY):
            self.assertEqual(procmem.write_page_protection(current),
                             procmem.PAGE_EXECUTE_READWRITE, hex(current))

    def test_a_handle_without_write_rights_writes_nothing(self):
        self.assertFalse(procmem.Win32ProcessMemory(pid=1, handle=0)
                         .write(BASE + 0x1000, b"x"))
        self.assertFalse(procmem.Win32ProcessMemory(pid=1, handle=1,
                                                    can_write=False)
                         .write(BASE + 0x1000, b"x"))

    def test_the_restore_is_in_a_finally_and_the_minimal_value_is_asked_for(self):
        """The two defects, pinned in the source that has to run on Windows."""
        source = Path(procmem.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, procmem.__file__)
        owner = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef)
                     and node.name == "Win32ProcessMemory")
        write = next(node for node in owner.body
                     if isinstance(node, ast.FunctionDef) and node.name == "write")
        guarded = [node for node in ast.walk(write)
                   if isinstance(node, ast.Try) and node.finalbody]
        self.assertEqual(len(guarded), 1, "the restore needs exactly one finally")
        in_finally = {call.func.attr for call in ast.walk(guarded[0])
                      if isinstance(call, ast.Call)
                      and isinstance(call.func, ast.Attribute)}
        self.assertIn("VirtualProtectEx", in_finally)
        asked = {call.func.id for call in ast.walk(write)
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
        self.assertIn("write_page_protection", asked)
        # The old unconditional PAGE_EXECUTE_READWRITE is gone from the path.
        constants = {node.attr for node in ast.walk(write)
                     if isinstance(node, ast.Attribute)
                     and isinstance(node.value, ast.Name)
                     and node.value.id == "kernel32"}
        self.assertIn("VirtualProtectEx", constants)


# ---------------------------------------------------------------------------
# The write gate
# ---------------------------------------------------------------------------
class WriteGateTests(unittest.TestCase):
    """One function moves bytes into the game, and it checks ``can_write``."""

    def test_a_read_only_attachment_cannot_write(self):
        memory = InMemoryProcessMemory(module_base=BASE, can_write=False)
        memory.map(BASE + 0x1000, size=8)
        game = FmMemory(memory)
        self.assertFalse(game.can_write)
        self.assertFalse(game.write(BASE + 0x1000, b"\xff"))
        self.assertFalse(game.write_value(BASE + 0x1000, "<Q", 1))
        self.assertEqual(memory.read(BASE + 0x1000, 2), b"\x00\x00")
        self.assertEqual(memory.writes, 0)

    def test_a_writable_attachment_writes_through_the_adapter(self):
        memory = InMemoryProcessMemory(module_base=BASE)
        memory.map(BASE + 0x1000, size=8)
        game = FmMemory(memory)
        self.assertTrue(game.write(BASE + 0x1000, b"\x01\x02"))
        self.assertEqual(memory.read(BASE + 0x1000, 2), b"\x01\x02")
        self.assertEqual(memory.writes, 1)

    def test_there_is_exactly_one_call_to_the_adapter_write(self):
        """A second write path would be a second place to forget the check."""
        source = Path(fmmemory.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, fmmemory.__file__)
        gates = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "memory"
        ]
        self.assertEqual(len(gates), 1, "fmmemory must have one write gate")


# ---------------------------------------------------------------------------
# The in-game date
# ---------------------------------------------------------------------------

class GameDateTests(unittest.TestCase):
    def setUp(self):
        self.memory = InMemoryProcessMemory(pid=1, module_base=BASE,
                                            module_size=0x1000000)
        self.game = FmMemory(self.memory)

    def test_a_valid_day_and_year_decode_to_the_real_date(self):
        build_date(self.memory, BASE + 0x57ca3b8, 181, 2027)  # 2027-06-30
        self.assertEqual(self.game.read_game_date(2494687), "2027-06-30")

    def test_the_high_bits_of_the_day_word_are_a_flag_not_the_day(self):
        # 0x1E00 is set on some dates; the day is the low nine bits.
        build_date(self.memory, BASE + 0x57ca3b8, 181, 2027, flags=0x1E00)
        self.assertEqual(self.game.read_game_date(2494687), "2027-06-30")

    def test_an_implausible_year_is_rejected(self):
        build_date(self.memory, BASE + 0x57ca3b8, 181, 1998)
        self.assertEqual(self.game.read_game_date(2494687), "")

    def test_a_day_outside_the_year_is_rejected(self):
        build_date(self.memory, BASE + 0x57ca3b8, 0, 2027)
        self.assertEqual(self.game.read_game_date(2494687), "")
        self.memory.put(BASE + 0x57ca3b8, struct.pack("<HH", 367, 2027))
        self.assertEqual(self.game.read_game_date(2494687), "")

    def test_day_366_of_a_non_leap_year_is_not_a_date(self):
        # 2027 has no day 366: it would silently become 2028-01-01.
        build_date(self.memory, BASE + 0x57ca3b8, 366, 2027)
        self.assertEqual(self.game.read_game_date(2494687), "")
        build_date(self.memory, BASE + 0x57ca3b8, 366, 2028)  # leap year
        self.assertEqual(self.game.read_game_date(2494687), "2028-12-31")

    def test_the_per_build_rva_is_preferred_over_the_fallback(self):
        build = 2493426
        current = BASE + BUILD_TO_CURRENT_DATE_RVA[build]
        fallback = BASE + KNOWN_DATE_RVAS[0]
        self.assertNotEqual(current, fallback, "the two slots must differ")
        build_date(self.memory, current, 10, 2031)      # 2031-01-10
        build_date(self.memory, fallback, 200, 2029)    # 2029-07-19
        self.assertEqual(self.game.read_game_date(build), "2031-01-10")
        # Without the build id the first known RVA wins instead.
        self.assertEqual(self.game.read_game_date(None), "2029-07-19")

    def test_the_table_relative_fallback_is_used_when_nothing_else_reads(self):
        self.game.table_base = BASE + 0x57035e8
        build_date(self.memory,
                   self.game.table_base - TABLE_TO_CURRENT_DATE_RVA, 194, 2025)
        self.assertEqual(self.game.read_game_date(None), "2025-07-13")

    def test_a_scanned_build_reads_the_slot_paired_with_its_table(self):
        """An unregistered build must not read an old build's date RVA.

        The stale slot is given a *plausible* pair on purpose: inside a 377 MiB
        image a registered build's RVA still lands on readable bytes, and four
        bytes that pass the plausibility filter used to come back as the game's
        date.  This machine runs 27.1.0+2505117, which is not in the registry.
        """
        memory = RecordingMemory(pid=1, module_base=BASE,
                                 module_size=0x1000000)
        game = FmMemory(memory)
        game.scanned = True                     # the table came from the scan
        game.table_base = BASE + 0x57035e8      # an unregistered image's table
        stale = BASE + KNOWN_DATE_RVAS[0]
        paired = game.table_base - TABLE_TO_CURRENT_DATE_RVA
        self.assertNotEqual(stale, paired)
        build_date(memory, stale, 60, 2030)     # would decode to 2030-03-01
        build_date(memory, paired, 194, 2025)   # the real date: 2025-07-13
        self.assertEqual(game._date_candidates(2599999)[0], paired)
        self.assertEqual(game.read_game_date(2599999), "2025-07-13")
        # The paired slot was read first, and the stale one never at all.
        self.assertEqual(memory.reads[0][:2], (paired, 4))
        self.assertNotIn(stale,
                         [address for address, _size, _ok in memory.reads])

    def test_a_registered_build_still_prefers_its_own_paired_rva(self):
        """A known build keeps reading its own slot before anything else."""
        memory = RecordingMemory(pid=1, module_base=BASE,
                                 module_size=0x1000000)
        game = FmMemory(memory)
        build = 2493426
        game.scanned = False                    # resolved from a known RVA
        game.table_base = BASE + BUILD_TO_TABLE_RVA[build]
        own = BASE + BUILD_TO_CURRENT_DATE_RVA[build]
        stale = BASE + KNOWN_DATE_RVAS[0]
        paired = game.table_base - TABLE_TO_CURRENT_DATE_RVA
        self.assertNotEqual(own, stale)
        build_date(memory, own, 10, 2031)       # 2031-01-10
        build_date(memory, stale, 200, 2029)    # 2029-07-19
        self.assertEqual(game._date_candidates(build)[0], own)
        self.assertEqual(game.read_game_date(build), "2031-01-10")
        self.assertEqual(memory.reads[0][:2], (own, 4))
        self.assertNotIn(stale,
                         [address for address, _size, _ok in memory.reads])
        self.assertIn(paired, game._date_candidates(build))

    def test_no_readable_date_is_an_empty_string(self):
        self.assertEqual(self.game.read_game_date(2494687), "")


class DecodeDateTests(unittest.TestCase):
    """One date decoder for the in-game date, contracts and injuries."""

    def test_a_known_pair(self):
        self.assertEqual(decode_date(struct.pack("<HH", 181, 2027)), "2027-06-30")

    def test_a_zero_pair_is_blank(self):
        self.assertEqual(decode_date(struct.pack("<HH", 0, 0)), "")

    def test_a_truncated_buffer_is_blank(self):
        self.assertEqual(decode_date(b"\x01"), "")
        self.assertEqual(decode_date(b"", 4), "")

    def test_the_offset_can_point_into_a_larger_buffer(self):
        raw = b"\x00" * 8 + struct.pack("<HH", 194, 2023)
        self.assertEqual(decode_date(raw, 8), "2023-07-13")

    def test_the_year_window_is_a_parameter(self):
        raw = struct.pack("<HH", 181, 2010)
        self.assertEqual(decode_date(raw), "2010-06-30")
        self.assertEqual(decode_date(raw, min_year=2024), "")


# ---------------------------------------------------------------------------
# The human manager's club
# ---------------------------------------------------------------------------

class ManagedClubTests(unittest.TestCase):
    def setUp(self):
        self.memory = InMemoryProcessMemory(pid=1, module_base=BASE,
                                            module_size=0x1000000)
        self.game = FmMemory(self.memory)
        self.root = BASE + BUILD_TO_HUMAN_MANAGER_RVA[2494687]
        self.allocations = [BASE + 0x200000 + index * 0x100
                           for index in range(5)]

    def test_the_manager_to_club_chain_decodes(self):
        build_club_chain(self.memory, self.root, "Arsenal", self.allocations)
        self.assertEqual(self.game.read_human_manager_club(2494687), "Arsenal")

    def test_the_per_build_root_is_preferred(self):
        build = 2493426
        other_root = BASE + BUILD_TO_HUMAN_MANAGER_RVA[build]
        self.assertNotEqual(other_root, self.root)
        build_club_chain(self.memory, other_root, "Inter", self.allocations)
        second = [BASE + 0x500000 + index * 0x100 for index in range(5)]
        build_club_chain(self.memory, self.root, "Arsenal", second)
        self.assertEqual(self.game.read_human_manager_club(build), "Inter")
        self.assertEqual(self.game.read_human_manager_club(2494687), "Arsenal")

    def test_a_broken_link_returns_an_empty_name_instead_of_raising(self):
        build_club_chain(self.memory, self.root, "Arsenal", self.allocations)
        # Cut the chain between the team and the club.
        team = self.allocations[2]
        put_u64(self.memory, team + fmmemory.TEAM_CLUB_REL, 0)
        self.assertEqual(self.game.read_human_manager_club(2494687), "")

    def test_a_club_with_no_name_pointer_returns_empty(self):
        club = build_club_chain(self.memory, self.root, "Arsenal",
                                self.allocations)
        put_u64(self.memory, club + fmmemory.CLUB_NAME_RELS[0], 0)
        put_u64(self.memory, club + fmmemory.CLUB_NAME_RELS[1], 0)
        self.assertEqual(self.game.read_human_manager_club(2494687), "")

    def test_an_unreadable_root_returns_empty(self):
        self.assertEqual(self.game.read_human_manager_club(2494687), "")

    def test_the_name_is_read_through_one_indirection_when_needed(self):
        club = build_club_chain(self.memory, self.root, "Arsenal",
                                self.allocations)
        text = self.allocations[4]
        indirect = BASE + 0x600000
        put_u64(self.memory, club + fmmemory.CLUB_NAME_RELS[0], indirect)
        put_u64(self.memory, indirect, text)
        self.assertEqual(self.game.read_human_manager_club(2494687), "Arsenal")

    def test_a_scanned_build_reads_the_root_paired_with_its_table(self):
        """A build found by scanning must not read another build's root."""
        memory = RecordingMemory(pid=1, module_base=BASE,
                                 module_size=0x1000000)
        game = FmMemory(memory)
        game.scanned = True
        game.table_base = BASE + 0x57035e8
        stale = BASE + KNOWN_HUMAN_MANAGER_RVAS[0]
        paired = game.table_base - TABLE_TO_HUMAN_MANAGER_RVA
        self.assertNotEqual(stale, paired)
        # Both roots hold a fully decodable chain: only the order decides.
        build_club_chain(memory, stale, "Stale United",
                         [BASE + 0x200000 + index * 0x100 for index in range(5)])
        build_club_chain(memory, paired, "Arsenal",
                         [BASE + 0x500000 + index * 0x100 for index in range(5)])
        self.assertEqual(game._manager_candidates(2599999)[0], paired)
        self.assertEqual(game.read_human_manager_club(2599999), "Arsenal")
        self.assertNotIn(stale,
                         [address for address, _size, _ok in memory.reads])

    def test_a_registered_build_still_prefers_its_own_manager_root(self):
        """A known build keeps reading its own root, not the newest one."""
        memory = RecordingMemory(pid=1, module_base=BASE,
                                 module_size=0x1000000)
        game = FmMemory(memory)
        build = 2493426
        game.scanned = False
        game.table_base = BASE + BUILD_TO_TABLE_RVA[build]
        own = BASE + BUILD_TO_HUMAN_MANAGER_RVA[build]
        stale = BASE + KNOWN_HUMAN_MANAGER_RVAS[0]
        self.assertNotEqual(own, stale)
        build_club_chain(memory, own, "Inter",
                         [BASE + 0x200000 + index * 0x100 for index in range(5)])
        build_club_chain(memory, stale, "Stale United",
                         [BASE + 0x500000 + index * 0x100 for index in range(5)])
        self.assertEqual(game._manager_candidates(build)[0], own)
        self.assertEqual(game.read_human_manager_club(build), "Inter")
        self.assertNotIn(stale,
                         [address for address, _size, _ok in memory.reads])
        # For every registered build the table-relative derivation lands on the
        # same root, which is what makes it a usable fallback at all.
        self.assertEqual(game.table_base - TABLE_TO_HUMAN_MANAGER_RVA, own)


# ---------------------------------------------------------------------------
# The offset table
# ---------------------------------------------------------------------------

class TableScanTests(unittest.TestCase):
    def test_scoring_a_perfect_table(self):
        memory = InMemoryProcessMemory(module_base=BASE, module_size=0x200000)
        memory.map(BASE + 0x1000, size=0x10000)
        build_table(memory, BASE + 0x1000)
        game = FmMemory(memory)
        self.assertEqual(game.score_table(BASE + 0x1000), 47)
        self.assertEqual(game.score_table(BASE + 0x90000), 0)

    def test_resolve_table_uses_a_known_rva_before_scanning(self):
        memory = RecordingMemory(module_base=BASE, module_size=0x6000000)
        rva = KNOWN_TABLE_RVAS[0]
        memory.map(BASE + rva, size=0x10000)
        build_table(memory, BASE + rva)
        game = FmMemory(memory)
        found = game.resolve_table(build=2599999)   # an unregistered build
        self.assertEqual(found, BASE + rva)
        self.assertEqual(game.table_rva, rva)
        self.assertFalse(game.scanned, "a known RVA needs no scan")
        self.assertEqual(game.best_score, 47)

    def test_resolve_table_tries_the_per_build_rva_first(self):
        build = 2493426
        memory = InMemoryProcessMemory(module_base=BASE,
                                       module_size=0x6000000)
        per_build = BUILD_TO_TABLE_RVA[build]
        decoy = KNOWN_TABLE_RVAS[0]
        self.assertNotEqual(per_build, decoy)
        memory.map(BASE + per_build, size=0x10000)
        build_table(memory, BASE + per_build)
        memory.map(BASE + decoy, size=0x10000)
        build_table(memory, BASE + decoy)
        game = FmMemory(memory)
        self.assertEqual(game.resolve_table(build=build), BASE + per_build)
        self.assertEqual(game.table_rva, per_build)

    def test_the_scan_walks_several_regions_and_finds_an_unregistered_table(self):
        memory = RecordingMemory(module_base=BASE, module_size=0x200000)
        memory.map(BASE + 0x100000, size=0x10000)     # empty readable region
        memory.map(BASE + 0x180000, size=0x10000)     # the table, further on
        build_table(memory, BASE + 0x180000)
        game = FmMemory(memory)
        regions = game.readable_regions(BASE, BASE + 0x200000)
        self.assertEqual(len(regions), 2)
        self.assertEqual(game.resolve_table(build=2599999), BASE + 0x180000)
        self.assertTrue(game.scanned)

    def test_a_region_that_spans_an_unmapped_gap_is_retried_in_64k_slices(self):
        """The 1 MiB probe fails; the 64 KiB retry is what finds the table."""
        memory = RecordingMemory(module_base=BASE, module_size=0x200000)
        # One committed range, but only its first 64 KiB is actually readable.
        memory.map(BASE + 0x100000, size=0x10000)
        memory.declare_region(BASE + 0x100000, BASE + 0x200000)
        build_table(memory, BASE + 0x100000)
        game = FmMemory(memory)
        self.assertIsNone(memory.read(BASE + 0x100000, 0x100000))
        found = game.resolve_table(build=2599999)
        self.assertEqual(found, BASE + 0x100000)
        # The first probe was the doomed 1 MiB read, then 64 KiB slices.
        self.assertEqual(memory.reads[0], (BASE + 0x100000, 0x100000, False))
        self.assertIn((BASE + 0x100000, 65536, True), memory.reads)

    def test_the_hot_band_region_is_scanned_before_a_lower_one(self):
        memory = RecordingMemory(module_base=BASE, module_size=0x6000000)
        low = BASE + 0x100000
        hot = BASE + fmmemory.HOT_BAND_START
        memory.map(low, size=0x10000)
        build_table(memory, low)
        memory.map(hot, size=0x10000)
        build_table(memory, hot)
        game = FmMemory(memory)
        ordered = game.prioritise_regions(game.readable_regions(
            BASE, BASE + 0x6000000))
        self.assertEqual([region[0] for region in ordered], [hot, low])
        # Both regions hold a perfect table; the hot one is found first, so the
        # low one is never even read.  Read in address order it would be the
        # other way round.
        self.assertEqual(game.resolve_table(build=2599999), hot)
        addresses = [address for address, _size, _ok in memory.reads]
        self.assertIn(hot, addresses)
        self.assertNotIn(low, addresses, "the hot band must be scanned first")

    def test_a_read_only_region_comes_after_a_writable_one(self):
        memory = InMemoryProcessMemory(module_base=BASE, module_size=0x200000)
        game = FmMemory(memory)
        ordered = game.prioritise_regions([
            (BASE + 0x1000, BASE + 0x2000, False),
            (BASE + 0x9000, BASE + 0xA000, True),
        ])
        self.assertEqual([region[0] for region in ordered],
                         [BASE + 0x9000, BASE + 0x1000])

    def test_no_table_at_all_is_a_plain_no_table_error(self):
        memory = InMemoryProcessMemory(module_base=BASE, module_size=0x200000)
        game = FmMemory(memory)
        with self.assertRaises(LiveError) as caught:
            game.resolve_table(build=2599999)
        self.assertEqual(caught.exception.code, "no_table")


# ---------------------------------------------------------------------------
# The people vector
# ---------------------------------------------------------------------------

class PeopleTests(unittest.TestCase):
    def setUp(self):
        self.memory = InMemoryProcessMemory(pid=1, module_base=BASE,
                                            module_size=0x800000)
        self.memory.map(BASE + 0x1000, size=0x10000)
        build_table(self.memory, BASE + 0x1000)
        self.game = FmMemory(self.memory)
        self.game.table_base = BASE + 0x1000

    def test_the_vector_bounds_and_the_ordered_pointers(self):
        pointers = build_people(self.memory, BASE + 0x1000,
                                BASE + 0x100000, BASE + 0x110000,
                                MIN_PEOPLE, uid_at={5: 92074020})
        begin, end, count = self.game.people_bounds()
        self.assertEqual(count, MIN_PEOPLE)
        self.assertEqual(end - begin, count * 8)
        self.assertEqual(self.game.people_pointers(), pointers)

    def test_find_person_reads_the_unique_id_behind_each_pointer(self):
        pointers = build_people(self.memory, BASE + 0x1000,
                                BASE + 0x100000, BASE + 0x110000,
                                MIN_PEOPLE, uid_at={5: 92074020})
        self.assertEqual(self.game.find_person(92074020), pointers[5])
        self.assertIsNone(self.game.find_person(1))

    def test_a_people_count_that_is_not_a_save_is_rejected(self):
        build_people(self.memory, BASE + 0x1000, BASE + 0x100000,
                     BASE + 0x110000, 100)
        with self.assertRaises(LiveError) as caught:
            self.game.people_bounds()
        self.assertEqual(caught.exception.code, "bad_count")


# ---------------------------------------------------------------------------
# The record decode, over the adapter
# ---------------------------------------------------------------------------

class RecordDecodeTests(unittest.TestCase):
    def test_a_record_decodes_from_mapped_memory(self):
        memory = InMemoryProcessMemory(module_base=BASE)
        raw = bytearray(0x400)
        struct.pack_into("<I", raw, BLOCK_BEFORE + 12, 92074020)
        struct.pack_into("<H", raw, BLOCK_BEFORE - 36, 158)
        struct.pack_into("<H", raw, BLOCK_BEFORE - 34, 163)
        struct.pack_into("<H", raw, BLOCK_BEFORE - 44, 9417)
        memory.map(BASE + 0x100000, data=bytes(raw))
        block = memory.read(BASE + 0x100000, 0x400)
        record = decode_record(block)
        self.assertIsNotNone(record)
        self.assertEqual((record.unique_id, record.ca, record.pa),
                         (92074020, 158, 163))
        self.assertEqual(record.condition, 9417)

    def test_a_nonsense_record_is_rejected(self):
        self.assertIsNone(decode_record(b"\x00" * 0x400))


# ---------------------------------------------------------------------------
# The public surface
# ---------------------------------------------------------------------------

# Every name live.py exported before the split, minus the import statements
# that happened to leak out of it (callable/typing/os/struct/dataclass).
REQUIRED_LIVE_NAMES = (
    "BLOCK_BEFORE", "BLOCK_SIZE", "BUILD_TO_ALTERNATE_DATE_RVA",
    "BUILD_TO_CURRENT_DATE_RVA", "BUILD_TO_HUMAN_MANAGER_RVA",
    "BUILD_TO_TABLE_RVA", "GAME_PLUGIN_SCAN_RANGE", "GameProcess",
    "INTEGRITY_NAMES", "KNOWN_DATE_RVAS", "KNOWN_HUMAN_MANAGER_RVAS",
    "KNOWN_TABLE_RVAS", "LEGACY_TABLE_RVA", "LIST_MODULES_ALL", "LiveError",
    "LivePlayer", "LiveReport", "MAX_ABILITY", "MAX_PATH", "MAX_PEOPLE",
    "MEM_COMMIT", "MIN_PEOPLE", "MIN_VALID_TABLE_SCORE", "PAGE_EXECUTE_READ",
    "PAGE_EXECUTE_READWRITE", "PAGE_EXECUTE_WRITECOPY", "PAGE_GUARD",
    "PAGE_NOACCESS", "PAGE_READONLY", "PAGE_READWRITE", "PAGE_WRITECOPY",
    "PROCESS_QUERY_INFORMATION", "PROCESS_QUERY_LIMITED_INFORMATION",
    "PROCESS_VM_OPERATION", "PROCESS_VM_READ", "PROCESS_VM_WRITE",
    "REL_CA", "REL_CONDITION", "REL_CURRENT_REP", "REL_DATE_OF_BIRTH",
    "REL_HISTORY_COPY", "REL_HOME_REP", "REL_JADEDNESS", "REL_MORALE",
    "REL_PA", "REL_SHARPNESS", "REL_UNIQUE_ID", "REL_WORLD_REP",
    "SCAN_CHUNK_BYTES", "SLOTS", "SUBTYPE_HUMAN_MANAGER", "SUBTYPE_PLAYER",
    "SUBTYPE_PLAYER_STAFF", "SUBTYPE_STAFF", "TABLE_TO_CURRENT_DATE_RVA",
    "TABLE_TO_HUMAN_MANAGER_RVA", "TABLE_VECTOR_PTR", "TH32CS_SNAPMODULE",
    "TH32CS_SNAPMODULE32", "TH32CS_SNAPPROCESS", "_MemoryBasicInformation",
    "_ModuleEntry32W", "_ModuleInfo", "_ProcessEntry32W", "_is_windows",
    "_module_base", "_module_base_toolhelp", "_parse_build_from_version",
    "_process_path", "_toolhelp_fm_pids", "_window_pids", "decode_record",
    "diagnose", "find_game_process", "game_build", "integrity_level",
    "integrity_name", "open_game", "read_live", "self_elevated",
)

# The DLL bindings only exist on Windows, exactly as they did before the split.
WINDOWS_ONLY_LIVE_NAMES = (
    "_enum_modules", "_enum_processes", "_module_base_name", "advapi32",
    "kernel32", "psapi", "user32", "w",
)

REQUIRED_LIVEWRITE_NAMES = (
    "BLOCK_BEFORE", "BLOCK_SIZE", "EditError", "LiveEditor", "LiveInjury",
    "LivePlayerRecord", "MAX_ABILITY", "MAX_ATTRIBUTE", "MAX_CONDITION",
    "RANGES", "_clamp", "_read_date", "probe_write_support", "read_players",
)


class PublicSurfaceTests(unittest.TestCase):
    """Nothing a caller imports may have moved out of the module it was in.

    The split into ``procmem`` and ``fmmemory`` was an internal rearrangement:
    ``live`` and ``livewrite`` still export every name they ever did, and the
    names are the *same objects* (re-exported, not copied), so a caller cannot
    tell the difference and two tables cannot drift apart.
    """

    def test_live_still_exports_every_name(self):
        missing = [name for name in REQUIRED_LIVE_NAMES
                   if not hasattr(live, name)]
        self.assertEqual(missing, [])

    def test_live_still_exports_the_windows_bindings(self):
        if os.name != "nt":
            self.skipTest("the DLL objects only exist on Windows")
        missing = [name for name in WINDOWS_ONLY_LIVE_NAMES
                   if not hasattr(live, name)]
        self.assertEqual(missing, [])

    def test_livewrite_still_exports_every_name(self):
        missing = [name for name in REQUIRED_LIVEWRITE_NAMES
                   if not hasattr(livewrite, name)]
        self.assertEqual(missing, [])

    def test_the_surviving_names_are_the_new_modules_objects(self):
        self.assertIs(live.open_game, fmmemory.open_game)
        self.assertIs(live.read_live, fmmemory.read_live)
        self.assertIs(live.diagnose, fmmemory.diagnose)
        self.assertIs(live.decode_record, fmmemory.decode_record)
        self.assertIs(live.decode_date, fmmemory.decode_date)
        self.assertIs(live.SLOTS, fmmemory.SLOTS)
        self.assertIs(live.LiveError, procmem.LiveError)
        self.assertIs(live.LiveError, fmmemory.LiveError)
        self.assertIs(live.find_game_process, procmem.find_game_process)
        self.assertIs(live._module_base, procmem._module_base)
        self.assertIs(live.integrity_level, procmem.integrity_level)
        self.assertEqual(live.PROCESS_VM_READ, procmem.PROCESS_VM_READ)
        self.assertEqual(live.PAGE_READWRITE, procmem.PAGE_READWRITE)
        self.assertTrue(issubclass(live.GameProcess, fmmemory.FmMemory))
        self.assertIs(livewrite.RANGES, spec.LIVE_RANGES)
        self.assertIs(livewrite.LiveInjury, fmmemory.LiveInjury)
        self.assertIs(livewrite._read_date, fmmemory.decode_date)

    def test_livewrite_no_longer_declares_its_own_ranges(self):
        """The literal table is gone; the spec's is the only one."""
        self.assertEqual(livewrite.MAX_ATTRIBUTE, spec.ATTRIBUTE_MAX)
        self.assertEqual(livewrite.MAX_ABILITY, spec.ABILITY_MAX)
        self.assertEqual(livewrite.MAX_CONDITION, spec.CONDITION_MAX)
        for column, bounds in livewrite.RANGES.items():
            self.assertEqual(spec.LIVE_RANGES[column], bounds, column)

    def test_the_offset_table_slots_have_one_authority(self):
        """``memmap.SLOTS`` is the table; ``fmmemory`` re-exports that object.

        The literal was declared twice after the split into ``procmem`` /
        ``fmmemory``, so the scanner and the live writer read two copies.  One
        object, not two equal ones: only identity keeps them from drifting.
        """
        self.assertIs(fmmemory.SLOTS, memmap.SLOTS)
        self.assertEqual(len(memmap.SLOTS), 11)
        self.assertEqual(memmap.SLOTS["PeopleOffset"], 336)

    def test_the_clamp_agrees_with_the_live_table(self):
        for column, (low, high) in spec.LIVE_RANGES.items():
            for value in (-1, 0, 1, 20, 21, 9999, 10000, 20000, 5_000_000):
                self.assertEqual(livewrite._clamp(column, value),
                                 max(low, min(high, value)), (column, value))
        # A visible attribute is stored 0..100 and shown 1..20.
        self.assertEqual(livewrite._clamp("dribbling", 99), spec.ATTRIBUTE_MAX)
        self.assertEqual(livewrite._clamp("dribbling", 0), 0)
        # A position and a personality byte are stored 1..20: 0 is not a value
        # the game offers there.
        self.assertEqual(livewrite._clamp("striker", 0), 1)
        self.assertEqual(livewrite._clamp("striker", 99), spec.ATTRIBUTE_MAX)
        self.assertEqual(livewrite._clamp("adaptability", 0), 1)
        # ca/pa: the live floor is 1, the canonical snapshot floor is 0.  That
        # difference is declared in spec.LIVE_OVERRIDES, not invented here.
        self.assertEqual(livewrite._clamp("ca", 0), 1)
        self.assertEqual(spec.clamp("ca", 0), 0)


class GameProcessCompatibilityTests(unittest.TestCase):
    """``GameProcess(pid, handle, module_base, module_size)`` still exists."""

    def test_the_handle_constructor_still_works_off_windows(self):
        game = live.GameProcess(pid=1, handle=0, module_base=BASE,
                                module_size=0x1000)
        self.assertIsInstance(game, fmmemory.FmMemory)
        self.assertIsInstance(game.memory, procmem.Win32ProcessMemory)
        self.assertEqual(game.module_base, BASE)
        self.assertEqual(game.module_size, 0x1000)
        # No handle, no reading and no write rights.
        self.assertFalse(game.can_write)
        self.assertIsNone(game.read(BASE, 8))
        self.assertFalse(game.write(BASE, b"\x00"))
        game.close()

    def test_a_handle_with_write_rights_is_writable(self):
        game = live.GameProcess(pid=1, handle=1, module_base=BASE,
                                module_size=0x1000, can_write=True)
        self.assertTrue(game.can_write)
        game.close()


if __name__ == "__main__":
    unittest.main()
