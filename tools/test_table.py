"""Checks for the live reader's table finding, without a running game.

The scan is the part that matters for a game build that is not in the
registry (the running FM27 reports 2505117, which is newer than the three
registered builds).  These tests build a fake process image in memory and
run the real scoring and scanning code against it.
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fmrte.live import (  # noqa: E402
    BUILD_TO_TABLE_RVA, MIN_VALID_TABLE_SCORE, SLOTS, TABLE_VECTOR_PTR,
    GameProcess, LiveError, decode_record,
)

BASE = 0x140000000
TABLE_AT = BASE + 0x1000


class FakeMemory:
    """A sparse little-endian address space that answers like a process.

    A real ``ReadProcessMemory`` fails when any part of the range is
    unreadable, so a read that spans a gap returns None, exactly like the
    game does when the scan walks past an unmapped page.
    """

    def __init__(self):
        self.blocks: dict[int, bytearray] = {}

    def write(self, address: int, data: bytes) -> None:
        self.blocks[address] = bytearray(data)

    def write_u64(self, address: int, value: int) -> None:
        self.write(address, struct.pack("<Q", value))

    def read(self, address: int, size: int) -> bytes | None:
        for start, data in self.blocks.items():
            if start <= address and address + size <= start + len(data):
                offset = address - start
                return bytes(data[offset:offset + size])
        return None

    def read_partial(self, address: int, size: int) -> bytes | None:
        """Return the readable prefix of a range, like a page-by-page read."""
        for start, data in self.blocks.items():
            if start <= address < start + len(data):
                offset = address - start
                return bytes(data[offset:offset + size])
        return None


class FakeImage(FakeMemory):
    """One contiguous, writable image, like a loaded module."""

    def __init__(self, base: int, size: int):
        super().__init__()
        self.base = base
        self.size = size
        self.data = bytearray(size)

    def write(self, address: int, data: bytes) -> None:
        offset = address - self.base
        self.data[offset:offset + len(data)] = data

    def read(self, address: int, size: int) -> bytes | None:
        offset = address - self.base
        if offset < 0 or offset + size > self.size:
            return None
        return bytes(self.data[offset:offset + size])

    read_partial = read


class FakeGame(GameProcess):
    """A GameProcess whose reads come from a FakeMemory.

    ``read`` returns the readable prefix of a range, which is what the scan
    needs: a real process read of a 1 MiB chunk that crosses an unmapped
    page fails outright, so the scan walks forward and tries again.

    The fake image is called ``image``, not ``memory``: ``memory`` is the
    ``ProcessMemory`` adapter the FM27 decoder reads through, and shadowing it
    would take the *region* scan (``readable_regions``) away from the process
    object and hand it to something that does not answer it.  Leaving the
    adapter in place is deliberate: a handle that is not a real process reports
    no readable regions, which is exactly the "no VirtualQueryEx" path this
    tool has always covered.
    """

    def __init__(self, image: FakeMemory, module_base: int = BASE,
                 module_size: int = 0x200000):
        super().__init__(pid=1, handle=1, module_base=module_base,
                         module_size=module_size)
        self.image = image

    def read(self, address: int, size: int) -> bytes | None:
        return self.image.read_partial(address, size)


def build_table(memory: FakeMemory, base: int, counts: dict[str, int]) -> None:
    """Lay out a plausible offset table with one collection per slot."""
    next_free = base + 0x2000
    for name, offset in SLOTS.items():
        count = counts.get(name, 0)
        collection = next_free
        vector = next_free + 0x100
        entries = next_free + 0x200
        next_free += 0x1000
        memory.write_u64(base + offset, collection)
        memory.write_u64(collection + TABLE_VECTOR_PTR, vector)
        memory.write_u64(vector, entries)
        memory.write_u64(vector + 8, entries + count * 8)


def good_counts() -> dict[str, int]:
    return {
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


def test_score() -> None:
    memory = FakeImage(BASE, 0x200000)
    build_table(memory, TABLE_AT, good_counts())
    game = FakeGame(memory)
    score = game.score_table(TABLE_AT)
    assert score >= MIN_VALID_TABLE_SCORE, score
    # A perfect table scores 47 with these ranges.
    assert score == 47, score
    # An empty address scores nothing.
    assert game.score_table(BASE + 0x900000) == 0
    print(f"table score: ok ({score}/47)")


def test_scan_finds_unregistered_build() -> None:
    """A build that is not in the registry is found by the scan."""
    memory = FakeImage(BASE, 0x200000)
    build_table(memory, TABLE_AT, good_counts())
    game = FakeGame(memory)
    # Pretend the build is unknown, so only the scan can succeed.
    found = game.resolve_table(build=2505117)
    assert found == TABLE_AT, hex(found)
    assert game.scanned is True
    assert game.best_score >= MIN_VALID_TABLE_SCORE
    print("scan finds an unregistered build: ok")


def test_scan_rejects_noise() -> None:
    """Random-looking data must not be mistaken for a table."""
    memory = FakeImage(BASE, 0x200000)
    noise = bytearray(0x100000)
    for index in range(0, len(noise) - 8, 8):
        struct.pack_into("<Q", noise, index, 0x00007FF6_00000000 + index * 3)
    memory.write(BASE, bytes(noise))
    game = FakeGame(memory)
    try:
        game.resolve_table(build=2505117)
    except LiveError as exc:
        assert exc.code == "no_table", exc.code
        print("scan rejects noise: ok")
        return
    raise AssertionError("noise was accepted as an offset table")


def test_people_bounds() -> None:
    memory = FakeImage(BASE, 0x200000)
    build_table(memory, TABLE_AT, good_counts())
    game = FakeGame(memory)
    game.table_base = TABLE_AT
    begin, end, count = game.people_bounds()
    assert count == 42344, count
    assert end - begin == count * 8
    print("people bounds: ok")


def test_decode_still_works() -> None:
    raw = bytearray(0x400)
    before = 0x1A0
    struct.pack_into("<I", raw, before + 12, 92074020)
    struct.pack_into("<H", raw, before - 36, 158)
    struct.pack_into("<H", raw, before - 34, 163)
    record = decode_record(bytes(raw))
    assert record and record.unique_id == 92074020
    assert (record.ca, record.pa) == (158, 163)
    print("record decode: ok")


def main() -> int:
    test_score()
    test_scan_finds_unregistered_build()
    test_scan_rejects_noise()
    test_people_bounds()
    test_decode_still_works()
    print("live table finding: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
