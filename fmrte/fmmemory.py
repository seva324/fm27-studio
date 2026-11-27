"""The Football Manager 27 memory layout, over any :class:`ProcessMemory`.

Everything in this module is about the *game*, not about Windows: where the
offset table hides inside ``game_plugin.dll``, how the ``people`` vector is
laid out, which bytes of a person object are the current ability and the
condition, how the injury chain hangs together, where the in-game date and the
human manager's club live.  It reaches memory through exactly one object - a
:class:`fmrte.procmem.ProcessMemory` - and through it alone, so the whole
decoder can be driven by a dictionary of bytes
(:class:`fmrte.procmem.InMemoryProcessMemory`) instead of a running game.

That is the seam this module exists for.  Before it, all of the above were
methods of the object that owned the Win32 handle, so the only way to test a
decode was to subclass the process object and override its reads; anything
that was not "read a block and decode it" - the game date, the manager's club,
the region scan, the read-only attach - went untested.

The chain (see ``MECHANICS.md`` section 4)
------------------------------------------
::

    game_plugin.dll base
      + offset-table RVA            -> the offset table
      + 336 (PeopleOffset slot)     -> the People collection object
      + 128                         -> a vector {begin, end}
      begin .. end                  -> one pointer per person object
      person + 12                   -> unique id (u32)
      person - 36 / - 34            -> current / potential ability (i16)

The numbers themselves are the person's own working reader's
(FM AI Assistant 2.1.0, classes ``FmOffsets``, ``PlayerRecordLayout`` and
``PersonIdentityLayout``), cross-checked against FMRTE's own read wrapper at
``0x1408410A0``.  Record field offsets come from :mod:`fmrte.memmap`, which is
the memory-map authority; attribute ranges come from :mod:`fmrte.spec`.

Writing
-------
There is exactly one function that moves bytes into the game
(:meth:`FmMemory.write`), and it asks :attr:`ProcessMemory.can_write` first.
A read-only attachment - the normal case for the Xbox/Game Pass build, where
Windows refuses the write handle - therefore cannot write by accident, no
matter which caller forgets to check.
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from . import memmap as M
from . import procmem
from . import spec
from .procmem import LiveError, ProcessMemory

# ---------------------------------------------------------------------------
# The offset table: FmOffsets, from FM AI Assistant 2.1.0
# ---------------------------------------------------------------------------
# Build id -> offset-table RVA inside game_plugin.dll.  The newest is tried
# first; an unregistered build falls back to KNOWN_TABLE_RVAS then the scan.
BUILD_TO_TABLE_RVA = {
    2511875: 0x53f8cc0,  # Xbox / PC Game Pass 27.1.1+2511875 (verified live)
    2505117: 0x53faca0,  # Xbox / PC Game Pass 27.1.0+2505117 (verified live)
    2494687: 92407336,   # 0x5820628 (Steam 27.0.1)
    2493426: 91248104,   # 0x57055e8
    2486695: 91239912,   # 0x57035e8
}
# Verified table RVAs across Steam & Xbox/Game Pass images (NativeModuleProfile).
KNOWN_TABLE_RVAS = (
    0x53F8CC0,  # Xbox / PC Game Pass 27.1.1+2511875
    0x5820628,  # Steam 27.0.1 / 2494687
    0x5683BD8,  # Xbox / PC Game Pass 2494687
    0x587B6D0,  # Steam 26620374
    0x57055E8,  # 2493426
    0x57035E8,  # 2486695
    0x53FACA0,  # Xbox / PC Game Pass 27.1.0+2505117
)
LEGACY_TABLE_RVA = 91239912

BUILD_TO_CURRENT_DATE_RVA = {
    2511875: 0x539bd10,  # Xbox / PC Game Pass 27.1.1+2511875 (verified live)
    2505117: 0x53a4a30,  # Xbox / PC Game Pass 27.1.0+2505117 (verified live)
    2494687: 0x57ca3b8,
    2493426: 0x56af378,
    2486695: 0x56ad378,
}
KNOWN_DATE_RVAS = (
    0x539bd10,  # Xbox / PC Game Pass 27.1.1+2511875
    0x57ca3b8,  # Steam 27.0.1 / 2494687
    0x56257c8,  # Xbox / PC Game Pass 2494687
    0x58268e0,  # Steam 26620374
    0x56af378,  # 2493426
    0x56ad378,  # 2486695
    0x53a4a30,  # Xbox / PC Game Pass 27.1.0+2505117
)
BUILD_TO_ALTERNATE_DATE_RVA = {
    2511875: 0x53f9708,
    2494687: 0x539dd10,
}
BUILD_TO_HUMAN_MANAGER_RVA = {
    2511875: 0x53e9158,  # Xbox / PC Game Pass 27.1.1+2511875 (verified live)
    2505117: 0x53eb138,  # Xbox / PC Game Pass 27.1.0+2505117 (verified live)
    2494687: 0x5810308,
    2493426: 0x56f52c8,
    2486695: 0x56f32c8,
}
KNOWN_HUMAN_MANAGER_RVAS = (
    0x53e9158,  # Xbox / PC Game Pass 27.1.1+2511875
    0x5810308,  # Steam 27.0.1 / 2494687
    0x5672618,  # Xbox / PC Game Pass 2494687
    0x586cdf8,  # Steam 26620374
    0x56f52c8,  # 2493426
    0x56f32c8,  # 2486695
    0x53eb138,  # Xbox / PC Game Pass 27.1.0+2505117
)
# Table-relative fallbacks, derived from the one build where both RVAs are
# known (2486695: table 0x57035e8, date 0x56ad378, manager 0x56f32c8). "Paired"
# is the operative word: the table-relative address is the only date/manager
# address that is known to belong to the table this session resolved, which is
# why it leads once the table had to be found by ``scan_table`` - see
# :meth:`FmMemory._date_candidates`. Every read is validated before it is
# believed.
TABLE_TO_CURRENT_DATE_RVA = 0x57035e8 - 0x56ad378
TABLE_TO_CURRENT_DATE_DELTAS = (
    TABLE_TO_CURRENT_DATE_RVA,        # 0x56270 (27.0.x & 27.1.0)
    0x53f8cc0 - 0x539bd10,            # 0x5cfb0 (27.1.1+2511875)
    0x53f8cc0 - 0x53f9708,            # -0xa48 (secondary date mirror in 27.1.1)
)
TABLE_TO_HUMAN_MANAGER_RVA = 0x57035e8 - 0x56f32c8
# Additional paired deltas observed on newer FM27 builds (e.g. 27.1.0+2505117
# where table - manager = 0x53faca0 - 0x53eb138 = 0x0FB68), so both Steam and
# Xbox builds discovered by ``scan_table`` resolve the managed club reliably.
TABLE_TO_HUMAN_MANAGER_DELTAS = (
    TABLE_TO_HUMAN_MANAGER_RVA,       # 0x10320 (27.0.x Steam/Xbox)
    0x53faca0 - 0x53eb138,            # 0x0FB68 (27.1.x Steam/Xbox)
)

# The module the offsets are relative to.
GAME_MODULE = "game_plugin.dll"

# Offset-table slots (bytes from the table base).
# ``memmap.SLOTS`` is the authority; this is a re-export, not a copy, so the
# scan and the live writer can never disagree about where a slot is.
SLOTS = M.SLOTS
TABLE_VECTOR_PTR = 128

# The scoring scan: 96 MiB of game_plugin.dll, 1 MiB per read, 35 points to
# accept a candidate table.
GAME_PLUGIN_SCAN_RANGE = 100663296
SCAN_CHUNK_BYTES = 1048576
MIN_VALID_TABLE_SCORE = 35

# Where every known FM27 offset table lives, relative to the module base: a
# window on the game's own data section.  The scanner tries it first because a
# candidate in the hot band is far likelier to be the real table than the same
# score anywhere else, and the scan must not be derailed by lookalikes in the
# Denuvo padding far above it.
HOT_BAND_START = 0x5000000
HOT_BAND_END = 0x7000000
# 160 MiB: the ceiling for the second pass, for Steam Denuvo images whose
# .data section shifts past the primary 96 MiB span (e.g. 377 MiB images).
MAX_SCAN_SPAN = 0x0A00_0000
# A 1 MiB probe that crosses an unmapped page fails outright, so a region is
# retried in slices at least this small before being given up on.
SUB_SLICE_BYTES = 65536

# -- PlayerRecordLayout.Direct (offsets relative to the person object) ------
# The numbers are memmap's (its DIRECT_FIELDS is what the live writer reads and
# writes with); these names are the ones live.py has always exported.
REL_UNIQUE_ID = M.PERSON_UNIQUE_ID_REL                     # 12
REL_DATE_OF_BIRTH = 136
REL_CA = M.DIRECT_FIELDS["ca"][0]                          # -36
REL_PA = M.DIRECT_FIELDS["pa"][0]                          # -34
REL_CONDITION = M.DIRECT_FIELDS["condition"][0]            # -44
REL_JADEDNESS = M.DIRECT_FIELDS["jadedness"][0]            # -46
REL_SHARPNESS = M.DIRECT_FIELDS["match_sharpness"][0]      # -48
REL_MORALE = M.DIRECT_FIELDS["morale"][0]                  # -28
REL_HOME_REP = M.DIRECT_FIELDS["home_reputation"][0]       # -42
REL_CURRENT_REP = M.DIRECT_FIELDS["current_reputation"][0]  # -40
REL_WORLD_REP = M.DIRECT_FIELDS["world_reputation"][0]     # -38
REL_HISTORY_COPY = -314

# -- PersonIdentityLayout ---------------------------------------------------
# The vtable pointer sits at person + 0; the RTTI descriptor is at
# vtable - 8, and its dynamic-offset field at descriptor + 4.  These four
# values tell players, staff, player/staff and human managers apart.
SUBTYPE_STAFF = M.SUBTYPE_STAFF                       # 256
SUBTYPE_PLAYER = M.SUBTYPE_PLAYER                     # 648
SUBTYPE_PLAYER_STAFF = M.SUBTYPE_PLAYER_STAFF         # 896
SUBTYPE_HUMAN_MANAGER = M.SUBTYPE_HUMAN_MANAGER       # 1088

BLOCK_BEFORE = 0x1A0
BLOCK_SIZE = 0x400

# Plausibility limits, so a wrong pointer is rejected instead of decoded.
MAX_ABILITY = spec.ABILITY_MAX
MIN_PEOPLE = 30000
MAX_PEOPLE = 500000

# The reputation fields are not in spec's edit ranges (the snapshot never edits
# them); these bounds are decode guards, not edit limits.
MAX_REPUTATION = 20000
MAX_JADEDNESS = 2000
MAX_MORALE = spec.LIVE_RANGES["morale"][1]

# -- dates ------------------------------------------------------------------
# A date is two u16: the day of the year in the low 9 bits, then the year.
# ``memmap.DATE_DAY_MASK`` is the same fact for the live writer's fields.
DATE_DAY_MASK = 0x01FF
MIN_DATE_YEAR = 1900
MAX_DATE_YEAR = 2100
# The in-game date is a Football Manager 27 clock: a 1998 date read out of the
# wrong address is not the game's date, so its window is narrower.
MIN_GAME_YEAR = 2024

# -- the human manager's club ----------------------------------------------
# manager -> employment -> team -> club -> a length-prefixed club name.  These
# five offsets are the person's own PlayerExporter chain for the same object.
MANAGER_EMPLOYMENT_REL = 0xA8
EMPLOYMENT_TEAM_REL = 0x10
TEAM_CLUB_REL = 0x30
CLUB_NAME_RELS = (0xC8, 0xC0)

# -- the in-game current screen ---------------------------------------------
# FM27's active-screen entity pointer lives inside game_plugin.dll, like the
# game date and the human manager root.  Its per-build RVA is populated once
# calibrated against a running build (or overridden with FMRTE_CURRENT_SCREEN_RVA
# for live probing via tools/probe_current_screen.py).
BUILD_TO_CURRENT_SCREEN_RVA: dict[int, int] = {}
TABLE_TO_CURRENT_SCREEN_RVA: int = 0
CURRENT_SCREEN_ENV = "FMRTE_CURRENT_SCREEN_RVA"
NATION_NAME_RELS = (0x20, 0x18)
NATIONAL_CONTAINER_NATION_REL = 0x170
NATION_NAME_LIMIT = 96

# Live person name slots (verified on FM 27.1.0+2505117):
#   person + 0x40 -> direct length-prefixed full name ("Kylian Mbappé Lottin")
#   person + 0x50 -> NameObject -> *p first name ("Kylian")
#   person + 0x58 -> NameObject -> *p second name ("Mbappé")
#   person + 0x60 -> NameObject -> *p common name ("Rodri", "Lamine Yamal", optional)
REL_FULL_NAME = 0x40
REL_FIRST_NAME = 0x50
REL_SECOND_NAME = 0x58
REL_COMMON_NAME = 0x60
PERSON_NAME_LIMIT = 96

# -- injury -----------------------------------------------------------------
# person - 0x190 -> reference; reference[0] -> item; item carries the type
# pointer, the start date and the two day counts.  The offsets are memmap's
# (INJURY_REFERENCE_REL, INJURY_TYPE_REL, ...), which is the same chain
# ``PlayerExporter.injuryStatus`` walks.
INJURY_TYPE_NAME_LIMIT = 96
CLUB_NAME_LIMIT = 128

# -- strings ----------------------------------------------------------------
# FM27 stores a string as an i32 character count followed by that many
# characters, in one of two encodings: its own wide strings (club names) and
# the narrow ``std::string`` ones (an injury type's name).  One decoder serves
# both; ``live.py`` had its own wide copy and ``livewrite.py`` its own narrow
# one, and they disagreed about the same length prefix.
STRING_UTF16 = "utf-16-le"
STRING_UTF8 = "utf-8"
STRING_CODE_UNITS = {STRING_UTF16: 2, STRING_UTF8: 1}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class LivePlayer:
    unique_id: int
    ca: int
    pa: int
    condition: int
    sharpness: int
    jadedness: int
    morale: int
    home_reputation: int
    current_reputation: int
    world_reputation: int
    display_name: str = ""
    asking_price: int | None = None
    salary_weekly_raw: int | None = None
    salary_pa: int | None = None
    contract_end_date: str | None = None
    joined_club_date: str | None = None
    transfer_listed: int | None = None
    listed_for_loan: int | None = None
    club: str | None = None
    playing_club: str | None = None
    loan_club: str | None = None
    is_loaned_out: str | None = None
    injured: int | None = None
    injury: str | None = None
    injury_start_date: str | None = None
    injury_light_training_days_remaining: Any = None
    injury_full_training_days_remaining: Any = None
    injury_expected_return: str | None = None


@dataclass
class LiveInjury:
    """One injury as the game stores it, behind the player's injury pointer."""

    reference: int
    item: int
    name: str = ""
    start_date: str = ""
    full_training_days: int = 0
    light_training_days: int = 0

    @property
    def injured(self) -> bool:
        return self.reference != 0 and self.item != 0


@dataclass
class CurrentScreen:
    """The entity FM27 is currently showing on screen.

    ``kind`` is one of ``"player"``, ``"staff"``, ``"club"``, ``"nation"``.
    ``unique_id`` is the FM unique id when readable (always set for players
    and staff); ``name`` is the decoded display name for clubs and nations.
    """

    kind: str
    unique_id: int = 0
    name: str = ""
    address: int = 0
    source: str = ""


@dataclass
class LiveReport:
    """What a read attempt found, including why it stopped."""

    ok: bool = False
    code: str = ""
    message: str = ""
    hint: str = ""
    pid: int = 0
    exe: str = ""
    elevated: bool = False
    game_elevated: bool = False
    game_level: int | None = None
    self_level: int | None = None
    module_base: int = 0
    table_rva: int = 0
    table_base: int = 0
    table_score: int = 0
    table_count: int = 0
    scanned: bool = False
    matched: int = 0
    game_date: str = ""
    managed_club: str = ""
    players: dict[int, LivePlayer] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Decoders that do not need a process
# ---------------------------------------------------------------------------

def decode_date(raw: bytes, offset: int = 0,
                min_year: int = MIN_DATE_YEAR,
                max_year: int = MAX_DATE_YEAR) -> str:
    """Decode the game's packed day-of-year + year pair into ``YYYY-MM-DD``.

    The day is stored in the low nine bits of a u16 (the rest is a flag the
    game sets on some dates), followed by the year.  A pair that is not a real
    date is reported as an empty string rather than a wrong date: day 366 of a
    non-leap year decodes to January 1st of the next year, which is not the
    date the game meant, so it is rejected too.

    This is the *one* date decoder.  ``live.py`` decoded the in-game date with
    its own copy and ``livewrite.py`` decoded contract and injury dates with
    another; they disagreed about the year window and about how to build the
    date.
    """
    try:
        day = struct.unpack_from("<H", raw, offset)[0] & DATE_DAY_MASK
        year = struct.unpack_from("<H", raw, offset + 2)[0]
    except struct.error:
        return ""
    if not (1 <= day <= 366 and min_year <= year <= max_year):
        return ""
    try:
        stamp = date.fromordinal(date(year, 1, 1).toordinal() + day - 1)
    except (ValueError, OverflowError):
        return ""
    if stamp.year != year:
        return ""
    return stamp.isoformat()


def decode_record(raw: bytes) -> LivePlayer | None:
    """Decode one 0x400-byte block starting at person - 0x1A0."""

    def u8(rel: int) -> int:
        return raw[BLOCK_BEFORE + rel]

    def u16(rel: int) -> int:
        return struct.unpack_from("<H", raw, BLOCK_BEFORE + rel)[0]

    def i16(rel: int) -> int:
        return struct.unpack_from("<h", raw, BLOCK_BEFORE + rel)[0]

    def u32(rel: int) -> int:
        return struct.unpack_from("<I", raw, BLOCK_BEFORE + rel)[0]

    uid = u32(REL_UNIQUE_ID)
    ca = u16(REL_CA)
    pa = u16(REL_PA)
    if uid <= 0 or uid > 0xFFFFFFFF:
        return None
    if ca > MAX_ABILITY or pa > MAX_ABILITY:
        return None
    return LivePlayer(
        unique_id=uid,
        ca=ca,
        pa=pa,
        condition=max(0, min(spec.CONDITION_MAX, u16(REL_CONDITION))),
        sharpness=max(0, min(spec.CONDITION_MAX, u16(REL_SHARPNESS))),
        jadedness=max(-MAX_JADEDNESS, min(MAX_JADEDNESS, i16(REL_JADEDNESS))),
        morale=max(0, min(MAX_MORALE, u8(REL_MORALE))),
        home_reputation=max(0, min(MAX_REPUTATION, u16(REL_HOME_REP))),
        current_reputation=max(0, min(MAX_REPUTATION, u16(REL_CURRENT_REP))),
        world_reputation=max(0, min(MAX_REPUTATION, u16(REL_WORLD_REP))),
    )


def _unique_addresses(addresses: Iterable[int]) -> list[int]:
    """Drop zeroes and repeats, keeping the first occurrence in order.

    The candidate lists above are built from several tables that overlap (the
    same RVA appears in a per-build dictionary and in a known-RVA tuple), and a
    duplicate would only mean reading the same failure twice.
    """
    seen: set[int] = set()
    out: list[int] = []
    for address in addresses:
        address = int(address or 0)
        if address <= 0 or address in seen:
            continue
        seen.add(address)
        out.append(address)
    return out


# ---------------------------------------------------------------------------
# The decoder
# ---------------------------------------------------------------------------

class FmMemory:
    """The FM27 decoder, attached to one :class:`ProcessMemory`.

    One instance per attachment.  Every read goes through :meth:`read` and
    every write through :meth:`write`, so a subclass (or a different adapter)
    controls the bytes without touching the layout knowledge here.
    """

    def __init__(self, memory: ProcessMemory):
        self.memory = memory
        # Where :meth:`resolve_table` settled, and how it got there.
        self.table_rva = 0
        self.table_base = 0
        self.best_score = 0
        self.scanned = False
        self.build: int | None = None
        self._people_index: dict[int, int] = {}
        self._people_index_lock = threading.Lock()

    # -- the attached process ------------------------------------------
    @property
    def pid(self) -> int:
        return int(getattr(self.memory, "pid", 0) or 0)

    @property
    def module_base(self) -> int:
        return int(getattr(self.memory, "module_base", 0) or 0)

    @property
    def module_size(self) -> int:
        return int(getattr(self.memory, "module_size", 0) or 0)

    @property
    def exe(self) -> str:
        return getattr(self.memory, "exe", "") or ""

    @property
    def can_write(self) -> bool:
        """True when this attachment may write into the game."""
        return bool(getattr(self.memory, "can_write", False))

    def close(self) -> None:
        self.memory.close()

    def readable_regions(self, start: int, limit: int) -> list[tuple[int, int, bool]]:
        """The adapter's committed readable ranges (ascending, not ordered)."""
        return self.memory.readable_regions(start, limit)

    # -- raw access -----------------------------------------------------
    def read(self, address: int, size: int) -> bytes | None:
        if address <= 0 or size <= 0:
            return None
        try:
            return self.memory.read(int(address), int(size))
        except (OSError, TypeError, ValueError, AttributeError):
            return None

    def write(self, address: int, data: bytes) -> bool:
        """The one place bytes are ever written into the game.

        Every writer in the project ends up here: ``write_value``, the live
        editor's field writes, its date writes and its heal.  Because the
        read-only check is in this one function, a caller that forgets it
        cannot write to a read-only attachment - the write simply does not
        happen.
        """
        if address <= 0 or not data:
            return False
        if not self.can_write:
            return False
        try:
            return bool(self.memory.write(int(address), bytes(data)))
        except (OSError, TypeError, ValueError, AttributeError):
            return False

    def write_value(self, address: int, fmt: str, value) -> bool:
        """Pack one value and write it through :meth:`write`."""
        try:
            return self.write(address, struct.pack(fmt, value))
        except (struct.error, TypeError, ValueError):
            return False

    def u64(self, address: int) -> int | None:
        raw = self.read(address, 8)
        return struct.unpack("<Q", raw)[0] if raw else None

    def u32(self, address: int) -> int | None:
        raw = self.read(address, 4)
        return struct.unpack("<I", raw)[0] if raw else None

    def i16(self, address: int) -> int | None:
        raw = self.read(address, 2)
        return struct.unpack("<h", raw)[0] if raw else None

    # -- strings ---------------------------------------------------------
    def read_length_string(self, address: int, max_chars: int = 256,
                           encoding: str = STRING_UTF16) -> str:
        """Decode one of the game's length-prefixed strings.

        ``max_chars`` bounds the declared length, so a garbage pointer cannot
        ask for a gigabyte.  A string that does not decode is reported as
        empty: a bad name must never break a read of everything else.
        """
        if not address or address < 0x10000:
            return ""
        head = self.read(address, 4)
        if not head:
            return ""
        length = struct.unpack("<i", head[:4])[0]
        if length <= 0 or length > max_chars:
            return ""
        try:
            unit = STRING_CODE_UNITS[encoding]
        except KeyError as exc:  # pragma: no cover - a programming error
            raise ValueError(f"unknown FM27 string encoding {encoding!r}") from exc
        raw = self.read(address + 4, length * unit)
        if not raw:
            return ""
        try:
            return raw.decode(encoding, errors="strict").strip("\x00").strip()
        except (UnicodeDecodeError, LookupError):
            return ""

    def read_auto_length_string(self, address: int,
                                max_chars: int = 256) -> str:
        """Read a length-prefixed FM27 string stored as either UTF-8 or UTF-16LE.

        FM 27.1.0 (build 2505117) stores club, nation and person names as
        ``<i byte_len`` + UTF-8 bytes + NUL terminator, whereas earlier builds
        and tests use UTF-16LE code units.  UTF-8 is accepted only when the
        byte right after the payload is NUL and every decoded character is
        printable (>= 0x20), so UTF-16LE ASCII buffers (which contain embedded
        NUL bytes) fall straight through to UTF-16LE without ambiguity.
        """
        if not address or address < 0x10000:
            return ""
        head = self.read(address, 4)
        if not head:
            return ""
        length = struct.unpack("<i", head[:4])[0]
        if length <= 0 or length > max_chars:
            return ""
        utf8_raw = self.read(address + 4, length + 1)
        if utf8_raw and len(utf8_raw) == length + 1 and utf8_raw[length] == 0:
            payload = utf8_raw[:length]
            if b"\x00" not in payload:
                try:
                    text = payload.decode(STRING_UTF8, errors="strict").strip()
                except UnicodeDecodeError:
                    text = ""
                if text and all(ord(ch) >= 32 for ch in text):
                    return text
        return self.read_length_string(address, max_chars, STRING_UTF16)

    # The name live.py has always used for the wide decoder.
    def _read_fm_len_string(self, addr: int, max_chars: int = 256) -> str:
        return self.read_length_string(addr, max_chars, STRING_UTF16)

    # -- the offset table ------------------------------------------------
    def _vector_count(self, slot_address: int) -> int | None:
        """How many entries the collection at a slot holds, if it is sane."""
        collection = self.u64(slot_address)
        if not collection:
            return None
        vector = self.u64(collection + TABLE_VECTOR_PTR)
        if not vector or vector < 0x10000:
            return None
        begin = self.u64(vector)
        end = self.u64(vector + 8)
        if not begin or not end or end < begin:
            return None
        if (end - begin) % 8:
            return None
        count = (end - begin) // 8
        if count < 0 or count > MAX_PEOPLE:
            return None
        return count

    def table_counts(self, base: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for name, offset in SLOTS.items():
            count = self._vector_count(base + offset)
            if count is None:
                return {}
            out[name] = count
        return out

    def score_table(self, base: int) -> int:
        """The 11-slot score the person's reader uses (47 is a perfect table)."""
        counts = self.table_counts(base)
        if not counts:
            return 0

        def exact(name: str, want: int, points: int) -> int:
            return points if counts.get(name) == want else 0

        def between(name: str, low: int, high: int, points: int) -> int:
            value = counts.get(name)
            return points if value is not None and low <= value <= high else 0

        return (exact("ContinentOffset", 7, 10)
                + exact("RegionOffset", 28, 10)
                + between("PeopleOffset", MIN_PEOPLE, MAX_PEOPLE, 5)
                + between("TeamOffset", 30000, 200000, 4)
                + between("ClubOffset", 10000, 100000, 4)
                + between("CompetitionOffset", 1000, 50000, 3)
                + between("NationOffset", 150, 400, 3)
                + between("CurrencyOffset", 50, 300, 2)
                + between("CityOffset", 30000, 300000, 3)
                + between("StadiumOffset", 5000, 150000, 2)
                + between("AgreementOffset", 0, 1000, 1))

    def prioritise_regions(self, regions: list[tuple[int, int, bool]]
                           ) -> list[tuple[int, int, bool]]:
        """Order a scan: writable first, then the FM27 hot band, then address.

        The hot band is where every known FM27 offset table lives, so a
        candidate there is preferred over a region that merely happens to be
        writable; read-only regions come last because a table cannot be there.
        """
        hot_lo = self.module_base + HOT_BAND_START
        hot_hi = self.module_base + HOT_BAND_END

        def _priority(region: tuple[int, int, bool]) -> tuple[int, int, int]:
            r_start, r_end, writable = region
            in_hot = 0 if (r_end > hot_lo and r_start < hot_hi) else 1
            return (0 if writable else 1, in_hot, r_start)

        return sorted(regions, key=_priority)

    def _has_count(self, descriptor: int, expected: int) -> bool:
        """Fast 3-read check matching FmOffsets.hasCount before full scoring."""
        vector = self.u64(descriptor + TABLE_VECTOR_PTR)
        if not vector or vector < 0x10000:
            return False
        raw = self.read(vector, 16)
        if not raw:
            return False
        begin, end = struct.unpack("<QQ", raw)
        return begin > 0 and end >= begin and (end - begin) == expected * 8

    def _scan_chunk(self, address: int, chunk: bytes, max_slot: int,
                    continent: int, region: int, people: int,
                    best_score: int, best_base: int,
                    progress: Callable[[str, int, int], None] | None = None
                    ) -> tuple[int, int, bool]:
        words = len(chunk) // 8
        if words <= max_slot // 8:
            return best_score, best_base, False
        values = struct.unpack(f"<{words}Q", chunk[:words * 8])
        plausible = {
            index for index, value in enumerate(values)
            if 0x10000 < value < 0x7FFFFFFFFFFF and (value & 7) == 0
        }
        last = words - max_slot // 8 - 1
        for index in range(0, last):
            if (index + continent not in plausible
                    or index + region not in plausible
                    or index + people not in plausible):
                continue
            c_ptr = values[index + continent]
            r_ptr = values[index + region]
            p_ptr = values[index + people]
            if c_ptr == r_ptr or c_ptr == p_ptr or r_ptr == p_ptr:
                continue
            # Reject ordinary pointer arrays before reading all 11 registry slots.
            if not self._has_count(c_ptr, 7):
                continue
            candidate = address + index * 8
            score = self.score_table(candidate)
            if score > best_score:
                best_score = score
                best_base = candidate
                if progress:
                    progress("Scanning the game module", score,
                             MIN_VALID_TABLE_SCORE)
                if score >= MIN_VALID_TABLE_SCORE:
                    self.best_score = score
                    self.table_base = candidate
                    return best_score, candidate, True
        return best_score, best_base, False

    def _scan_range(self, start: int, limit: int, best_score: int, best_base: int,
                    progress: Callable[[str, int, int], None] | None = None,
                    scan_state: list[int] | None = None
                    ) -> tuple[int, int, bool]:
        max_slot = max(SLOTS.values())
        span = (SCAN_CHUNK_BYTES - (max_slot + 8)) & -8
        continent = SLOTS["ContinentOffset"] // 8
        region = SLOTS["RegionOffset"] // 8
        people = SLOTS["PeopleOffset"] // 8
        address = start
        while address < limit:
            size = min(SCAN_CHUNK_BYTES, limit - address)
            if progress and scan_state is not None:
                scan_state[0] += size
                progress("Scanning game_plugin.dll (MB)",
                         scan_state[0] // 1048576,
                         max(1, scan_state[1] // 1048576))
            chunk = self.read(address, size)
            if not chunk and size > 4096:
                # A chunk spanning an unmapped boundary fails outright; try
                # smaller 64 KiB slices so readable pages aren't skipped.
                sub = address
                sub_limit = address + size
                while sub < sub_limit:
                    sub_size = min(SUB_SLICE_BYTES, sub_limit - sub)
                    sub_chunk = self.read(sub, sub_size)
                    if sub_chunk and len(sub_chunk) >= max_slot + 8:
                        best_score, best_base, hit = self._scan_chunk(
                            sub, sub_chunk, max_slot, continent, region,
                            people, best_score, best_base, progress)
                        if hit:
                            return best_score, best_base, True
                    sub += max(4096, (sub_size - (max_slot + 8)) & -8)
                address += span
                continue
            if not chunk:
                address += span
                continue
            best_score, best_base, hit = self._scan_chunk(
                address, chunk, max_slot, continent, region, people,
                best_score, best_base, progress)
            if hit:
                return best_score, best_base, True
            if size >= limit - address:
                break
            address += span
        return best_score, best_base, False

    def scan_table(self, progress: Callable[[str, int, int], None] | None = None
                   ) -> int:
        """Look for the offset table inside game_plugin.dll.

        This is the fallback for a game build or store image that is not in the
        registry.  On Windows it walks the committed readable regions (with
        writable sections in the 80-112 MiB hot band first) so unreadable page
        boundaries never abort a whole 1 MiB chunk and huge trailing Denuvo
        padding (.debug$P) is never scanned; when the adapter reports no
        regions at all it falls back to the bounded linear scan.
        """
        self.scanned = True
        best_score = 0
        best_base = 0
        start = self.module_base
        # First bound to GAME_PLUGIN_SCAN_RANGE (96 MiB), with a capped 112 MiB
        # ceiling if the module is larger (e.g. 377 MiB Denuvo images).
        primary_span = min(GAME_PLUGIN_SCAN_RANGE,
                           self.module_size or GAME_PLUGIN_SCAN_RANGE)
        limit = start + primary_span

        regions = self.prioritise_regions(self.readable_regions(start, limit))
        if regions:
            total_bytes = sum(r_end - r_start for r_start, r_end, _w in regions)
            scan_state = [0, max(1, total_bytes)]
            for r_start, r_end, _writable in regions:
                best_score, best_base, hit = self._scan_range(
                    r_start, r_end, best_score, best_base, progress, scan_state)
                if hit:
                    return best_base
            # If the table shifted slightly above 96 MiB on a newer build, check
            # 96..112 MiB as a second bounded pass.
            if self.module_size and self.module_size > primary_span:
                extra_limit = start + min(MAX_SCAN_SPAN, self.module_size)
                extra_regions = self.prioritise_regions(
                    self.readable_regions(limit, extra_limit))
                if extra_regions:
                    extra_bytes = sum(e - s for s, e, _w in extra_regions)
                    extra_state = [0, max(1, extra_bytes)]
                    for r_start, r_end, _writable in extra_regions:
                        best_score, best_base, hit = self._scan_range(
                            r_start, r_end, best_score, best_base, progress,
                            extra_state)
                        if hit:
                            return best_base
            self.best_score = best_score
            self.table_base = best_base
            return best_base

        scan_state = [0, max(1, limit - start)]
        best_score, best_base, hit = self._scan_range(
            start, limit, best_score, best_base, progress, scan_state)
        self.best_score = best_score
        self.table_base = best_base
        return best_base

    def resolve_table(self, build: int | None,
                      progress: Callable[[str, int, int], None] | None = None
                      ) -> int:
        """Find the offset table: known RVAs first, then the region scan."""
        tried: set[int] = set()
        candidates: list[int] = []
        rva = BUILD_TO_TABLE_RVA.get(build or 0)
        if rva:
            candidates.append(rva)
        for item in KNOWN_TABLE_RVAS:
            candidates.append(item)
        for item in BUILD_TO_TABLE_RVA.values():
            candidates.append(item)

        for candidate_rva in candidates:
            if not candidate_rva or candidate_rva in tried:
                continue
            tried.add(candidate_rva)
            base = self.module_base + candidate_rva
            score = self.score_table(base)
            if score >= MIN_VALID_TABLE_SCORE:
                self.table_rva = candidate_rva
                self.table_base = base
                self.best_score = score
                return base
        base = self.scan_table(progress=progress)
        if base and self.best_score >= MIN_VALID_TABLE_SCORE:
            self.table_rva = base - self.module_base
            return base
        raise LiveError(
            "The game is running, but its player table could not be found.",
            "This usually means the game build is newer than this editor "
            "knows, or the save has not finished loading yet. The scan "
            f"checked the game module and the best candidate scored "
            f"{self.best_score} of {MIN_VALID_TABLE_SCORE} needed.",
            code="no_table")

    # -- the people vector ------------------------------------------------
    def people_bounds(self) -> tuple[int, int, int]:
        slot = self.u64(self.table_base + SLOTS["PeopleOffset"])
        if not slot:
            raise LiveError("The player list is empty.",
                            "Load your save in Football Manager first.",
                            code="no_people")
        vector = self.u64(slot + TABLE_VECTOR_PTR)
        if not vector:
            raise LiveError("The player list has no entries yet.",
                            "Wait until the save has finished loading.",
                            code="no_people")
        begin = self.u64(vector)
        end = self.u64(vector + 8)
        if not begin or not end or end <= begin:
            raise LiveError("The player list is empty.",
                            "Load your save in Football Manager first.",
                            code="no_people")
        count = (end - begin) // 8
        if count < MIN_PEOPLE or count > MAX_PEOPLE:
            raise LiveError(
                f"The player list holds {count:,} entries, which is not a "
                "Football Manager save.",
                "Load your save in the game and try again.",
                code="bad_count")
        return begin, end, count

    def people_pointers(self) -> list[int]:
        """Every person pointer in the order the game keeps them."""
        start, end, count = self.people_bounds()
        raw = self.read(start, count * 8)
        if not raw:
            raise LiveError(
                "The player list could not be read.",
                "Start this editor as administrator and try again.",
                code="read_failed")
        return list(struct.unpack(f"<{count}Q", raw))

    def _build_people_index_locked(self) -> dict[int, int]:
        index: dict[int, int] = {}
        for pointer in self.people_pointers():
            if pointer < 0x10000:
                continue
            raw = self.read(pointer + REL_UNIQUE_ID, 4)
            if raw:
                uid = struct.unpack("<I", raw)[0]
                if uid:
                    index[uid] = pointer
        self._people_index = index
        return index

    def build_people_index(self) -> dict[int, int]:
        """Scan the people vector once and cache ``{unique_id: person_ptr}``.

        A live FM27 save holds ~100k+ people pointers; building this dict once
        (~0.8s) turns every subsequent :meth:`find_person` lookup from an O(N)
        linear scan into a single 4-byte O(1) verification read (~0.005ms).
        """
        with self._people_index_lock:
            return self._build_people_index_locked()

    def ensure_people_index(self) -> dict[int, int]:
        """Return the cached people index, building it on first access."""
        if self._people_index:
            return self._people_index
        with self._people_index_lock:
            if self._people_index:
                return self._people_index
            return self._build_people_index_locked()

    def invalidate_people_index(self) -> None:
        with self._people_index_lock:
            self._people_index.clear()

    def find_person(self, unique_id: int) -> int | None:
        """The person object address for one unique id.

        Uses the cached ``{unique_id: pointer}`` index built from the live
        people vector and verifies the 4-byte UID at ``pointer + REL_UNIQUE_ID``
        so a moved/reloaded object transparently triggers one index rebuild.
        """
        target = int(unique_id)
        if not target:
            return None
        index = self.ensure_people_index()
        pointer = index.get(target)
        if pointer:
            raw = self.read(pointer + REL_UNIQUE_ID, 4)
            if raw and struct.unpack("<I", raw)[0] == target:
                return pointer
            # Pointer moved (e.g. save reload): rebuild index once and re-check.
            index = self.build_people_index()
            pointer = index.get(target)
            if pointer:
                raw = self.read(pointer + REL_UNIQUE_ID, 4)
                if raw and struct.unpack("<I", raw)[0] == target:
                    return pointer
        return None

    def _slot_pointers(self, slot_name: str, max_count: int = MAX_PEOPLE) -> list[int]:
        """Read all object pointers from one offset-table collection slot."""
        if not self.table_base or slot_name not in SLOTS:
            return []
        slot = self.u64(self.table_base + SLOTS[slot_name])
        if not slot or slot < 0x10000:
            return []
        vector = self.u64(slot + TABLE_VECTOR_PTR)
        if not vector or vector < 0x10000:
            return []
        head = self.read(vector, 16)
        if not head or len(head) < 16:
            return []
        begin, end = struct.unpack("<QQ", head)
        if not begin or not end or end <= begin or (end - begin) % 8:
            return []
        count = (end - begin) // 8
        if count <= 0 or count > max_count:
            return []
        raw = self.read(begin, count * 8)
        if not raw or len(raw) < count * 8:
            return []
        return list(struct.unpack(f"<{count}Q", raw[:count * 8]))

    def read_nation_uid_map(self) -> dict[str, int]:
        """Build ``{lower_nation_name: fm_unique_id}`` from live ``NationOffset``."""
        mapping: dict[str, int] = {}
        span = max(NATION_NAME_RELS) + 8
        for pointer in self._slot_pointers("NationOffset", max_count=2000):
            if pointer < 0x10000:
                continue
            head = self.read(pointer, span)
            if not head or len(head) < span:
                continue
            uid = struct.unpack_from("<I", head, REL_UNIQUE_ID)[0]
            if not uid:
                continue
            for offset in NATION_NAME_RELS:
                name_ptr = struct.unpack_from("<Q", head, offset)[0]
                if not name_ptr or name_ptr < 0x10000:
                    continue
                name = self._read_name_slot(name_ptr, NATION_NAME_LIMIT)
                if name:
                    mapping.setdefault(name.strip().lower(), uid)
        return mapping

    def read_club_uid_map(self) -> dict[str, int]:
        """Build ``{lower_club_name: fm_unique_id}`` from live ``ClubOffset``."""
        mapping: dict[str, int] = {}
        span = max(CLUB_NAME_RELS) + 8
        for pointer in self._slot_pointers("ClubOffset", max_count=200000):
            if pointer < 0x10000:
                continue
            head = self.read(pointer, span)
            if not head or len(head) < span:
                continue
            uid = struct.unpack_from("<I", head, REL_UNIQUE_ID)[0]
            if not uid:
                continue
            for offset in CLUB_NAME_RELS:
                name_ptr = struct.unpack_from("<Q", head, offset)[0]
                if not name_ptr or name_ptr < 0x10000:
                    continue
                name = self._read_name_slot(name_ptr, CLUB_NAME_LIMIT)
                if name:
                    mapping.setdefault(name.strip().lower(), uid)
        return mapping

    # -- extra live facts -------------------------------------------------
    def _date_candidates(self, build: int | None) -> list[int]:
        """Every address :meth:`read_game_date` may read, best first.

        There are two kinds of candidate and they are *paired* with different
        things:

        * the per-build RVA is paired with ``build`` - it is the slot that
          build keeps its clock in;
        * ``table_base - TABLE_TO_CURRENT_DATE_RVA`` is paired with the offset
          table this session actually resolved.

        Which leads depends on how the table was found, and that is the fix for
        the fabricated dates.  A table resolved from a known RVA (``scanned``
        false) means the build is one this module knows, so its own RVA leads
        exactly as before.  A table found by :meth:`scan_table` means the build
        is *not* in the registry: every entry in those tables describes a
        different image, and those addresses still land inside a 377 MiB Denuvo
        build, where four plausible-looking bytes used to be returned as the
        game's date.  For a scanned build the paired address leads and the
        unpaired known RVAs are only a last resort.
        """
        base = self.module_base
        paired = [self.table_base - delta for delta in TABLE_TO_CURRENT_DATE_DELTAS] if self.table_base else []
        own: list[int] = []
        if build and build in BUILD_TO_CURRENT_DATE_RVA:
            own.append(base + BUILD_TO_CURRENT_DATE_RVA[build])
        alternates = [base + rva for rva in BUILD_TO_ALTERNATE_DATE_RVA.values()]
        if build and build in BUILD_TO_ALTERNATE_DATE_RVA:
            alternates.insert(0, base + BUILD_TO_ALTERNATE_DATE_RVA[build])
        known = [base + rva for rva in KNOWN_DATE_RVAS]
        if self.scanned:
            return _unique_addresses([*paired, *own, *known, *alternates])
        return _unique_addresses([*own, *known, *alternates, *paired])

    def read_game_date(self, build: int | None = None) -> str:
        """Best-effort live in-game date (``YYYY-MM-DD``), or empty string.

        The candidate order comes from :meth:`_date_candidates`: a registered
        build's own slot is still tried first, and a build that had to be found
        by scanning reads the address paired with its table before any other
        build's RVA.  Each candidate is validated as a real date before it is
        believed, so a wrong address reports no date rather than a wrong one.
        """
        for address in self._date_candidates(build):
            raw = self.read(address, 4)
            if not raw:
                continue
            stamp = decode_date(raw, 0, MIN_GAME_YEAR, MAX_DATE_YEAR)
            if stamp:
                return stamp
        return ""

    def _manager_candidates(self, build: int | None) -> list[int]:
        """Every address :meth:`read_human_manager_club` may read, best first.

        Same rule as :meth:`_date_candidates`, and the same reason: the root is
        paired either with ``build`` or with the resolved table, and for a
        scanned (unregistered) build only the table-relative one is known to
        belong to the image that is actually running.
        """
        base = self.module_base
        paired = [self.table_base - delta for delta in TABLE_TO_HUMAN_MANAGER_DELTAS] if self.table_base else []
        own: list[int] = []
        if build and build in BUILD_TO_HUMAN_MANAGER_RVA:
            own.append(base + BUILD_TO_HUMAN_MANAGER_RVA[build])
        known = [base + rva for rva in KNOWN_HUMAN_MANAGER_RVAS]
        if self.scanned:
            return _unique_addresses([*paired, *own, *known])
        return _unique_addresses([*own, *known, *paired])

    def read_human_manager_club(self, build: int | None = None) -> str:
        """Best-effort live managed club name from the human manager root.

        The root is a per-build RVA (the person's own ``FmOffsets`` carries one
        for each supported build), and for a build that is not in the registry
        the table-relative root is tried first (:meth:`_manager_candidates`).
        Either way the name is only returned when it reads back as a real
        length-prefixed string, so a stale root reports no club instead of a
        wrong one.
        """
        for addr in self._manager_candidates(build):
            manager = self.u64(addr)
            if not manager or manager < 0x10000:
                continue
            employment = self.u64(manager + MANAGER_EMPLOYMENT_REL)
            if not employment or employment < 0x10000:
                continue
            team = self.u64(employment + EMPLOYMENT_TEAM_REL)
            if not team or team < 0x10000:
                continue
            club = self.u64(team + TEAM_CLUB_REL)
            if not club or club < 0x10000:
                continue
            for offset in CLUB_NAME_RELS:
                pointer = self.u64(club + offset)
                if not pointer or pointer < 0x10000:
                    continue
                name = self.read_auto_length_string(pointer, CLUB_NAME_LIMIT)
                if not name:
                    indirect = self.u64(pointer)
                    if indirect and indirect >= 0x10000:
                        name = self.read_auto_length_string(indirect, CLUB_NAME_LIMIT)
                if name:
                    return name
        return ""

    def read_injury(self, reference: int) -> LiveInjury:
        """The injury behind one person's injury reference.

        In FM27, ``person - 0x190`` points to a ``std::vector<Injury*>``
        container ``{begin, end, cap}``.  A recovered player still keeps an
        allocated empty vector (``begin == end``), which MUST read as fit
        rather than injured.  When ``end > begin``, ``*begin`` is the active
        ``Injury`` item carrying type pointer, start date and day counts.
        Legacy single-pointer fixtures (where ``reference[0]`` is the item and
        ``reference[8] == 0``) remain supported for unit tests.
        """
        if reference <= 0x10000:
            return LiveInjury(0, 0)
        vec = self.read(reference, 16)
        item = 0
        if vec and len(vec) >= 16:
            beg, end = struct.unpack("<QQ", vec[:16])
            if beg <= 0x10000:
                return LiveInjury(0, 0)
            if end == beg:
                # Empty std::vector<Injury*> -> recovered / fit.
                return LiveInjury(0, 0)
            if end > beg and (end - beg) <= 512 and (end - beg) % 8 == 0:
                item = self.u64(beg) or 0
            elif end == 0:
                # Single-pointer layout (legacy / unit-test fixture).
                item = beg
            else:
                return LiveInjury(0, 0)
        else:
            head = self.read(reference, 8)
            if not head:
                return LiveInjury(0, 0)
            item = struct.unpack("<Q", head)[0]
        if item <= 0x10000:
            return LiveInjury(0, 0)
        injury = LiveInjury(reference, item)
        raw = self.read(item, 0x40)
        if not raw:
            return injury
        stamp = decode_date(raw, M.INJURY_DAY_REL)
        if stamp:
            injury.start_date = stamp
        try:
            injury.full_training_days = struct.unpack_from(
                "<H", raw, M.INJURY_FULL_TRAINING_REL)[0]
            injury.light_training_days = struct.unpack_from(
                "<H", raw, M.INJURY_LIGHT_TRAINING_REL)[0]
        except struct.error:
            pass
        if len(raw) >= M.INJURY_TYPE_REL + 8:
            type_pointer = struct.unpack_from("<Q", raw, M.INJURY_TYPE_REL)[0]
            if type_pointer > 0x10000:
                # FM27 27.1.x stores a pointer to the localized length-prefixed
                # string at type_pointer + 0x20; fall back to inline UTF-8 for
                # older builds and unit-test fixtures.
                name_ptr = self.u64(type_pointer + M.INJURY_TYPE_NAME_REL)
                name = (
                    self._read_name_slot(name_ptr, INJURY_TYPE_NAME_LIMIT)
                    if name_ptr and 0x10000 < name_ptr < 0x7FFFFFFFFFFF else ""
                )
                if not name:
                    name = self.read_length_string(
                        type_pointer + M.INJURY_TYPE_NAME_REL,
                        INJURY_TYPE_NAME_LIMIT, STRING_UTF8)
                injury.name = name
        return injury

    def read_club_finances(
        self, club_name_cache: dict[int, str] | None = None
    ) -> dict[str, dict[str, int]]:
        """Read live club balance, transfer budget & payroll budget from ``ClubOffset``.

        Detects the full vs base finance vtable dynamically via FM27's
        ``full_vtable - base_vtable == 0x4D0`` invariant so it works across
        every FM27 build without hardcoded vtable RVAs.
        """
        cptrs = self._slot_pointers("ClubOffset", max_count=200000)
        if not cptrs:
            return {}
        vt_counts: dict[int, int] = {}
        club_fblks: list[tuple[int, int, int]] = []
        for cptr in cptrs:
            if cptr < 0x10000:
                continue
            fblk = self.u64(cptr + M.CLUB_FINANCE_BLOCK_REL)
            if not fblk or fblk < 0x10000:
                continue
            vt = self.u64(fblk)
            if vt and vt > 0x10000:
                vt_counts[vt] = vt_counts.get(vt, 0) + 1
                club_fblks.append((cptr, fblk, vt))
        full_vt = 0
        for vt in vt_counts:
            if (vt - M.CLUB_FINANCE_FULL_VTABLE_DELTA) in vt_counts:
                full_vt = vt
                break
        if not full_vt:
            return {}
        name_cache = club_name_cache if club_name_cache is not None else {}
        out: dict[str, dict[str, int]] = {}
        for cptr, fblk, vt in club_fblks:
            if vt != full_vt:
                continue
            fb = self.read(fblk, M.CLUB_FINANCE_FULL_BYTES)
            if not fb or len(fb) < M.CLUB_FINANCE_FULL_BYTES:
                continue
            if cptr not in name_cache:
                name_cache[cptr] = self._read_club_name(cptr)
            cname = name_cache[cptr]
            if not cname:
                continue
            bal = struct.unpack_from("<i", fb, M.CLUB_BALANCE_REL)[0]
            tb = struct.unpack_from("<i", fb, M.CLUB_TRANSFER_BUDGET_REL)[0]
            pb = struct.unpack_from("<i", fb, M.CLUB_PAYROLL_BUDGET_REL)[0]
            out[cname.strip().lower()] = {
                "balance": int(bal),
                "transfer_budget": int(tb),
                "payroll_budget": int(pb),
            }
        return out

    def read_live_snapshot(
        self,
        known_ids: Iterable[int],
        *,
        game_date: str = "",
        include_clubs: bool = True,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[dict[int, LivePlayer], dict[str, dict[str, int]]]:
        """Read full live state for known players + club finances using this handle.

        Captures live CA/PA, condition, sharpness, jadedness, morale, asking
        price, active injuries (zeroing recovered ones), contract end/joined
        dates, weekly salary, transfer/loan listing flags, live owning & loan
        clubs, and full-finance club balances & transfer budgets.
        """
        known = [int(x) for x in known_ids if x]
        index = self.ensure_people_index()
        base_date: date | None = None
        if game_date and len(game_date) >= 10:
            try:
                base_date = date.fromisoformat(game_date[:10])
            except ValueError:
                base_date = None

        team_club_cache: dict[int, str] = {}
        club_name_cache: dict[int, str] = {}

        def _club_from_team(tptr: int) -> str:
            if not tptr or tptr < 0x10000:
                return ""
            if tptr in team_club_cache:
                return team_club_cache[tptr]
            cptr = self.u64(tptr + M.TEAM_CLUB_REL)
            if not cptr or cptr < 0x10000:
                team_club_cache[tptr] = ""
                return ""
            if cptr not in club_name_cache:
                club_name_cache[cptr] = self._read_club_name(cptr)
            cname = club_name_cache[cptr]
            team_club_cache[tptr] = cname
            return cname

        found: dict[int, LivePlayer] = {}
        total = len(known)
        for i, uid in enumerate(known):
            if progress and i % 5000 == 0:
                progress("Reading live players", i, total)
            ptr = index.get(uid)
            if not ptr:
                continue
            block = self.read(ptr - BLOCK_BEFORE, BLOCK_SIZE)
            if not block or len(block) < BLOCK_SIZE:
                continue
            rec = decode_record(block)
            if not rec or rec.unique_id != uid:
                continue

            # Live asking price / display value
            val_raw = struct.unpack_from(
                "<I", block, BLOCK_BEFORE + M.DIRECT_FIELDS["asking_price_raw"][0]
            )[0]
            if 0 < val_raw < 2_000_000_000:
                rec.asking_price = int(val_raw)

            # Live injury state (clears recovered snapshot injuries!)
            inj_ref = struct.unpack_from(
                "<Q", block, BLOCK_BEFORE + M.INJURY_REFERENCE_REL
            )[0]
            if inj_ref > 0x10000:
                inj = self.read_injury(inj_ref)
            else:
                inj = LiveInjury(0, 0)
            if inj.injured:
                rec.injured = 1
                rec.injury = inj.name or "Injured"
                rec.injury_start_date = inj.start_date
                rec.injury_light_training_days_remaining = inj.light_training_days
                rec.injury_full_training_days_remaining = inj.full_training_days
                if inj.full_training_days > 0:
                    anchor = base_date
                    if anchor is None and inj.start_date:
                        try:
                            anchor = date.fromisoformat(inj.start_date[:10])
                        except ValueError:
                            anchor = None
                    if anchor is not None:
                        rec.injury_expected_return = (
                            anchor + timedelta(days=inj.full_training_days)
                        ).isoformat()
                else:
                    rec.injury_expected_return = ""
            else:
                rec.injured = 0
                rec.injury = ""
                rec.injury_start_date = ""
                rec.injury_light_training_days_remaining = ""
                rec.injury_full_training_days_remaining = ""
                rec.injury_expected_return = ""

            # Live registration (owning club, contract, wage, transfer/loan list)
            reg = struct.unpack_from(
                "<Q", block, BLOCK_BEFORE + M.REGISTRATION_POINTER_REL
            )[0]
            own_name = ""
            if reg > 0x10000:
                rb = self.read(reg, 96)
                if rb and len(rb) >= 96:
                    salary = struct.unpack_from("<I", rb, M.SALARY_WEEKLY_REL)[0]
                    if salary < 50_000_000:
                        rec.salary_weekly_raw = int(salary)
                        rec.salary_pa = round(salary * 52 / 1000) * 1000
                    cend = decode_date(rb, M.CONTRACT_DATE_REL)
                    if cend:
                        rec.contract_end_date = cend
                    jdate = decode_date(rb, M.JOINED_DATE_REL)
                    if jdate:
                        rec.joined_club_date = jdate
                    tstat = rb[M.TRANSFER_STATUS_REL]
                    rec.transfer_listed = 1 if tstat == 1 else 0
                    rec.listed_for_loan = 1 if tstat == 2 else 0
                    own_team = struct.unpack_from("<Q", rb, M.REGISTRATION_CLUB_REL)[0]
                    own_name = _club_from_team(own_team)

            # Live playing club & loan status
            play_team = struct.unpack_from(
                "<Q", block, BLOCK_BEFORE + M.PLAYING_CLUB_REF_REL
            )[0]
            play_name = _club_from_team(play_team)
            if own_name or play_name:
                if own_name and play_name and own_name != play_name:
                    rec.club = own_name
                    rec.playing_club = play_name
                    rec.loan_club = play_name
                    rec.is_loaned_out = "yes"
                else:
                    eff = own_name or play_name
                    rec.club = eff
                    rec.playing_club = eff
                    rec.loan_club = ""
                    rec.is_loaned_out = "no"

            found[uid] = rec
        if progress:
            progress("Reading live players", total, total)
        club_fin = self.read_club_finances(club_name_cache) if include_clubs else {}
        return found, club_fin

    # -- the in-game current screen --------------------------------------
    def _read_name_slot(self, pointer: int, limit: int = PERSON_NAME_LIMIT) -> str:
        if not pointer or pointer < 0x10000:
            return ""
        text = self.read_auto_length_string(pointer, limit)
        if text:
            return text
        indirect = self.u64(pointer)
        if indirect and indirect >= 0x10000:
            return self.read_auto_length_string(indirect, limit)
        return ""

    def read_person_display_name(self, address: int) -> str:
        """Read FM27's in-game display name (common name, else first + second).

        Verified on FM 27.1.0+2505117:
        ``+0x60`` -> Common Name (e.g. ``Rodri``, ``Lamine Yamal``),
        ``+0x50`` / ``+0x58`` -> First / Second Name (``Kylian`` + ``Mbappé``,
        ``Christian`` + ``Pulisic``), ``+0x40`` -> Full Name.
        """
        if not address or address < 0x10000:
            return ""
        common = self._read_name_slot(self.u64(address + REL_COMMON_NAME) or 0)
        if common:
            return common
        first = self._read_name_slot(self.u64(address + REL_FIRST_NAME) or 0)
        second = self._read_name_slot(self.u64(address + REL_SECOND_NAME) or 0)
        if first and second:
            return f"{first} {second}"
        if first or second:
            return first or second
        return self._read_name_slot(self.u64(address + REL_FULL_NAME) or 0)

    def _read_club_name(self, club: int) -> str:
        if not club or club < 0x10000:
            return ""
        for offset in CLUB_NAME_RELS:
            pointer = self.u64(club + offset)
            if not pointer or pointer < 0x10000:
                continue
            name = self._read_name_slot(pointer, CLUB_NAME_LIMIT)
            if name:
                return name
        return ""

    def _read_nation_name(self, nation: int) -> str:
        if not nation or nation < 0x10000:
            return ""
        for offset in NATION_NAME_RELS:
            pointer = self.u64(nation + offset)
            if not pointer or pointer < 0x10000:
                continue
            name = self._read_name_slot(pointer, NATION_NAME_LIMIT)
            if name:
                return name
        return ""

    def _person_subtype(self, address: int) -> int | None:
        """Read FM27's RTTI dynamic-offset subtype behind ``person + 0``."""
        if not address or address < 0x10000:
            return None
        vtable = self.u64(address)
        if not vtable or vtable < 0x10000:
            return None
        descriptor = self.u64(vtable - 8)
        if not descriptor or descriptor < 0x10000:
            return None
        return self.u32(descriptor + 4)

    def classify_screen_object(self, address: int,
                               source: str = "") -> CurrentScreen | None:
        """Classify one live FM27 object pointer into player / staff / club / nation.

        Uses FM27's measured object identities:

        * Person RTTI subtype (``vtable - 8 -> +4``): ``SUBTYPE_PLAYER`` /
          ``SUBTYPE_PLAYER_STAFF`` -> ``"player"``; ``SUBTYPE_STAFF`` /
          ``SUBTYPE_HUMAN_MANAGER`` -> ``"staff"``, with ``uid`` at ``+12``.
        * Club object: ``uid`` at ``+12`` and readable length-prefixed name at
          ``CLUB_NAME_RELS`` (``0xC8`` / ``0xC0``).
        * Team object: ``club`` pointer at ``+TEAM_CLUB_REL`` (``0x30``) whose
          club has a readable name.
        * Nation / NationalTeam container: ``nation`` directly (name at
          ``0x18`` / ``0x20``) or container ``+0x170 -> nation``.
        """
        if not address or address < 0x10000:
            return None
        uid = self.u32(address + REL_UNIQUE_ID) or 0
        subtype = self._person_subtype(address)
        if subtype in (SUBTYPE_PLAYER, SUBTYPE_PLAYER_STAFF) and uid > 0:
            return CurrentScreen(
                kind="player", unique_id=uid, address=address, source=source)
        if subtype in (SUBTYPE_STAFF, SUBTYPE_HUMAN_MANAGER) and uid > 0:
            return CurrentScreen(
                kind="staff", unique_id=uid, address=address, source=source)

        # Club object directly
        club_name = self._read_club_name(address)
        if club_name:
            return CurrentScreen(
                kind="club", unique_id=uid, name=club_name,
                address=address, source=source)

        # Team object -> club
        team_club = self.u64(address + TEAM_CLUB_REL)
        if team_club and team_club >= 0x10000:
            team_club_name = self._read_club_name(team_club)
            if team_club_name:
                club_uid = self.u32(team_club + REL_UNIQUE_ID) or 0
                return CurrentScreen(
                    kind="club", unique_id=club_uid, name=team_club_name,
                    address=team_club, source=source)

        # NationalTeam container -> nation
        container_nation = self.u64(address + NATIONAL_CONTAINER_NATION_REL)
        if container_nation and container_nation >= 0x10000:
            nation_name = self._read_nation_name(container_nation)
            if nation_name:
                nation_uid = self.u32(container_nation + REL_UNIQUE_ID) or 0
                return CurrentScreen(
                    kind="nation", unique_id=nation_uid, name=nation_name,
                    address=container_nation, source=source)

        # Nation object directly
        nation_name = self._read_nation_name(address)
        if nation_name:
            return CurrentScreen(
                kind="nation", unique_id=uid, name=nation_name,
                address=address, source=source)
        return None

    def _current_screen_candidates(self, build: int | None,
                                   rva: int | None = None
                                   ) -> list[tuple[int, str]]:
        """Candidate root addresses holding the current-screen object pointer."""
        out: list[tuple[int, str]] = []
        seen: set[int] = set()

        def add(address: int, source: str) -> None:
            if address > 0 and address not in seen:
                seen.add(address)
                out.append((address, source))

        if rva:
            addr = rva if rva >= self.module_base and self.module_base else (
                self.module_base + rva)
            add(addr, f"rva:0x{rva:x}")
        env_raw = os.environ.get(CURRENT_SCREEN_ENV, "").strip()
        if env_raw:
            try:
                parsed = int(env_raw, 0)
            except ValueError:
                parsed = 0
            if parsed > 0:
                addr = parsed if (self.module_base and parsed >= self.module_base) else (
                    self.module_base + parsed)
                add(addr, f"env:{CURRENT_SCREEN_ENV}")
        effective_build = build if build is not None else self.build
        if effective_build and effective_build in BUILD_TO_CURRENT_SCREEN_RVA:
            known = BUILD_TO_CURRENT_SCREEN_RVA[effective_build]
            add(self.module_base + known, f"build:{effective_build}")
        if self.table_base and TABLE_TO_CURRENT_SCREEN_RVA:
            add(self.table_base - TABLE_TO_CURRENT_SCREEN_RVA, "table-relative")
        return out

    def read_current_screen(self, build: int | None = None, *,
                            rva: int | None = None) -> CurrentScreen | None:
        """Read and classify the entity FM27 is currently showing, or None.

        Checks the configured RVA (explicit ``rva``, ``FMRTE_CURRENT_SCREEN_RVA``
        env override, or ``BUILD_TO_CURRENT_SCREEN_RVA``).  Handles both a
        direct entity pointer and a one-level wrapper whose first or second
        qword points at the entity.
        """
        for root, source in self._current_screen_candidates(build, rva=rva):
            target = self.u64(root)
            if not target or target < 0x10000:
                continue
            found = self.classify_screen_object(target, source=source)
            if found is not None:
                return found
            # One-level wrapper struct: entity pointer at +0 or +8.
            for rel in (0, 8):
                nested = self.u64(target + rel)
                if nested and nested >= 0x10000 and nested != target:
                    found = self.classify_screen_object(nested, source=source)
                    if found is not None:
                        return found
        return None


# ---------------------------------------------------------------------------
# Attaching, and reading everybody
# ---------------------------------------------------------------------------
# These three live here rather than in ``live`` because they are the whole
# public attach API: they need the FM27 report and the FM27 table resolution,
# and they talk to Windows only through ``procmem``.

def open_game(progress: Callable[[str, int, int], None] | None = None,
              write: bool = False) -> tuple[FmMemory, LiveReport]:
    """Attach to the running game, or raise LiveError with a plain reason.

    With ``write=True`` the handle also carries the two write rights, which is
    what the editor needs to put an edit into the game rather than into a
    local copy.
    """
    report = LiveReport(elevated=procmem.self_elevated())
    pid, exe = procmem.find_game_process()
    report.pid = pid
    report.exe = exe
    game_level = procmem.integrity_level(pid)
    report.game_elevated = game_level is not None and game_level >= 0x3000

    memory = procmem.open_process(pid, write=write, exe=exe)
    base, size = procmem._module_base(memory.handle, GAME_MODULE, pid=pid)
    if not base:
        memory.close()
        raise LiveError(
            "The game is running, but its data module is not loaded yet.",
            "Wait until the save has finished loading, then press Load Game "
            "again.",
            code="no_module")
    memory.module_base = base
    memory.module_size = size

    game = FmMemory(memory)
    report.module_base = base
    # The caller already found the executable, so hand it over rather than
    # making the version-resource reader discover the same process again.
    build = procmem.game_build(exe)
    game.build = build
    if progress:
        progress("Finding the player table", 0, 1)
    try:
        game.resolve_table(build, progress=progress)
    except LiveError:
        game.close()
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw ctypes error
        game.close()
        raise LiveError(
            "The game's player table could not be read.",
            "This build of the game may not be supported yet. The snapshot "
            "still works: press \"Use last snapshot\".",
            code="read_failed") from exc
    report.table_rva = game.table_rva
    report.table_base = game.table_base
    report.table_score = game.best_score
    report.scanned = game.scanned
    report.game_date = game.read_game_date(build)
    report.managed_club = game.read_human_manager_club(build)
    return game, report


def read_live(known_ids: Iterable[int],
              progress: Callable[[str, int, int], None] | None = None
              ) -> tuple[dict[int, LivePlayer], LiveReport]:
    """Read the live values for the players this snapshot already knows."""
    known = {int(x) for x in known_ids if x}
    game, report = open_game(progress)
    try:
        _start, _end, count = game.people_bounds()
        report.table_count = count
        found, _club_fin = game.read_live_snapshot(
            known, game_date=report.game_date, include_clubs=False, progress=progress
        )
        report.ok = True
        report.matched = len(found)
        report.players = found
        if not found:
            raise LiveError(
                "The game was read, but none of its players matched this "
                "snapshot.",
                "The save loaded in the game is probably a different one. "
                "Load the same save, or use the snapshot that matches it.",
                code="no_match")
        return found, report
    except LiveError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw ctypes error
        raise LiveError(
            "The game's player list could not be read.",
            "The snapshot still works: press \"Use last snapshot\".",
            code="read_failed") from exc
    finally:
        game.close()


def diagnose() -> LiveReport:
    """Check, without reading anything, whether a live read can work."""
    report = LiveReport(elevated=procmem.self_elevated())
    try:
        pid, exe = procmem.find_game_process()
    except LiveError as exc:
        report.code = exc.code or "not_running"
        report.message = str(exc)
        report.hint = exc.hint
        return report
    except Exception as exc:  # noqa: BLE001 - never leak a raw ctypes error
        report.code = "not_running"
        report.message = "Football Manager could not be found."
        report.hint = str(exc)
        return report
    report.pid = pid
    report.exe = exe
    level = procmem.integrity_level(pid)
    report.game_elevated = level is not None and level >= 0x3000
    report.game_level = level
    report.self_level = procmem.integrity_level(os.getpid())
    try:
        memory = procmem.open_process(pid, write=False)
    except LiveError as exc:
        # An OpenProcess that fails for any other reason still means "Windows
        # will not let this editor look at the game", which is the code the
        # window already explains.
        report.code = "denied" if exc.code == "open_failed" else (exc.code or "denied")
        report.hint = exc.hint
        if report.code == "needs_admin":
            # The window shows the two integrity levels it is comparing.
            report.message = (
                f"Football Manager runs at "
                f"{procmem.integrity_name(report.game_level)} permission and "
                f"this editor at {procmem.integrity_name(report.self_level)}, "
                f"so Windows will not let it read the game.")
        else:
            report.message = str(exc)
        return report
    memory.close()
    report.ok = True
    report.message = "Football Manager is running and can be read."
    return report
