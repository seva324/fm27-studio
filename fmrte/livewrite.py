"""Reading and writing one player record in the running game.

The original FMRTE edits the live game: it opens the game process with read
and write rights and changes the values where the game keeps them. This build
now does the same. Every offset comes from ``memmap.py``, which records where
each one was read in the person's own working reader; every range comes from
``spec.py``, which is the single authority for what the game accepts.

The editor is deliberately careful:

* it re-checks the unique id before writing, so a stale pointer can never
  change an unrelated object;
* it writes the smallest possible field (a byte, two bytes, four bytes);
* it reads the value back and only reports success when the game returned the
  value that was asked for;
* it keeps the previous bytes, so an edit can be undone.

What it does not claim: that Football Manager keeps the value forever. The game
recomputes some numbers itself (training, match sharpness, value), so a written
value is real but not necessarily permanent. That is the same behaviour the
original has.

Read-only attachments
---------------------
When Windows refuses the write handle - the normal case for the Xbox/Game Pass
build, where the game runs at a higher integrity level than this editor - the
editor attaches read-only and *everything readable still reads*. Only the four
methods that call ``WriteProcessMemory`` (``write``, ``write_date``, ``heal``,
``restore_injury``) refuse. This used to be wrong: ``read_live_values`` and
``read_injury`` refused too, so a read-only attach could not show live injury
data at all and the player screen silently kept the snapshot's stale values.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Callable

from . import memmap as M
from . import spec
from .fmmemory import (BLOCK_BEFORE, BLOCK_SIZE, CurrentScreen, FmMemory,
                       LiveError, LiveInjury, decode_date, decode_record,
                       open_game, read_live)

# The names this module has always exported.  The values are the spec's, so
# there is one table and one clamp for the whole build (spec.py).
MAX_ATTRIBUTE = spec.ATTRIBUTE_MAX          # 20
MAX_ABILITY = spec.ABILITY_MAX              # 200
MAX_CONDITION = spec.CONDITION_MAX          # 10000
RANGES = spec.LIVE_RANGES

# The FM27 date decoder, under the name this module and its tests use.
_read_date = decode_date

# How many replaced byte ranges the editor keeps for :meth:`LiveEditor.undo`.
#
# The edit journal in ``db.Snapshot`` is the window's undo authority now, so
# this stack is only the fallback that ``Snapshot`` reaches for and the thing
# one ``write()``/``heal()`` in a test is undone with.  Nothing reads it in
# bulk, but nothing ever trimmed it either, so a long session of live edits
# grew it without limit.  Keeping the most recent writes is enough for a
# fallback, and bounded memory is worth more than an undo history no caller
# walks.
MAX_UNDO_STEPS = 128


class EditError(RuntimeError):
    """An edit that could not be put into the game, with a plain reason."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _bounds(column: str) -> tuple[int, int]:
    """The live storage bounds for one writable column.

    ``spec.LIVE_RANGES`` answers for every column the game stores as a plain
    number.  Two families are deliberately not in it, because their stored
    domain differs from an edit range:

    * a position and a personality byte are stored 1..20 directly (a position
      the player cannot play is 1, not 0), so they floor at 1;
    * a visible attribute is stored 0..100 and shown 1..20, so its edit range
      is ``spec.ATTRIBUTE_RANGE``.

    Anything else is a raw contract value (the asking price's u32 field).
    """
    if column in M.POSITION_COLUMNS or column in M.DIRECT_POSITIVE_FIELDS:
        return 1, spec.ATTRIBUTE_MAX
    if column in spec.LIVE_RANGES:
        return spec.LIVE_RANGES[column]
    if column in M.ATTRIBUTE_OFFSETS:
        return spec.ATTRIBUTE_RANGE
    return 0, 0xFFFFFFFF


def _clamp(column: str, value: int) -> int:
    """Clamp an edited value to what the live game accepts.

    The bounds come from ``spec`` (through :func:`_bounds`), never from a table
    declared here.  It is ``spec.LIVE_RANGES`` rather than ``spec.clamp``
    because the two disagree on purpose and spec declares where: the live
    writer floors current/potential ability at 1 (``spec.LIVE_OVERRIDES``) and
    clamps the raw u32 asking price, while ``spec.clamp`` answers from the
    canonical ``spec.RANGES`` (floor 0, price unclamped) because the snapshot
    leaves those alone.
    """
    low, high = _bounds(column)
    return max(low, min(high, int(value)))


def _shown_attribute(column: str, stored: int) -> int:
    """Turn one byte of the attribute block into the number the screen shows.

    ``memmap.ATTRIBUTE_STORAGE_SCALE`` is the whole rule: visible attributes
    are stored 0..100 and shown 1..20, positions and personality bytes are
    stored 1..20 already.
    """
    if column in M.POSITION_COLUMNS:
        shown = stored
    else:
        shown = round(stored * spec.ATTRIBUTE_MAX / 100)
    return max(1, min(spec.ATTRIBUTE_MAX, shown))


@dataclass
class LivePlayerRecord:
    unique_id: int
    address: int
    values: dict[str, Any] = field(default_factory=dict)
    registration: int = 0
    injury_reference: int = 0
    injury_item: int = 0


class LiveEditor:
    """One attached game process, with the player record readers and writers.

    ``game`` is an :class:`fmrte.fmmemory.FmMemory` (the production path) or
    anything that answers the same calls - the tests drive this class with a
    fake game over :class:`fmrte.procmem.InMemoryProcessMemory`.
    """

    def __init__(self, game: FmMemory):
        self.game = game
        self.undo_stack: list[tuple[int, bytes, str]] = []
        self.writes = 0
        # A read-only attachment is decided by the process itself: the handle
        # either carries the write rights or it does not.
        self.read_only = not getattr(game, "can_write", True)
        self.live_date = ""
        self.managed_club = ""
        self.build = getattr(game, "build", None)

    # -- opening ---------------------------------------------------------
    @classmethod
    def attach(cls, progress: Callable[[str, int, int], None] | None = None
               ) -> tuple["LiveEditor", Any]:
        read_only = False
        try:
            game, report = open_game(progress, write=True)
        except LiveError as exc:
            if exc.code not in ("needs_admin", "denied"):
                raise
            # If Windows allows VM_READ but refuses VM_WRITE, still attach
            # read-only so live memory data loads instead of failing.
            game, report = open_game(progress, write=False)
            read_only = True
        editor = cls(game)
        editor.read_only = read_only or not getattr(game, "can_write", False)
        editor.live_date = getattr(report, "game_date", "") or ""
        editor.managed_club = getattr(report, "managed_club", "") or ""
        return editor, report

    def close(self) -> None:
        self.game.close()

    def _remember(self, address: int, old: bytes, label: str) -> None:
        """Record the bytes one write replaced, keeping the stack bounded.

        Capping here rather than at the call sites is deliberate: there are
        four writers (``write``, ``write_date``, ``heal``, ``restore_injury``)
        and a fifth must not be able to forget the bound.  Only the newest
        :data:`MAX_UNDO_STEPS` ranges survive, which is all a fallback undo
        needs - the journal in ``db.Snapshot`` is what actually walks history.
        """
        self.undo_stack.append((address, old, label))
        overflow = len(self.undo_stack) - MAX_UNDO_STEPS
        if overflow > 0:
            del self.undo_stack[:overflow]

    def _require_write(self) -> None:
        """Refuse a write on a read-only attach, in words the window can show.

        Only the four writing methods call this.  Reads must keep working
        read-only: that attach is the normal one for the Xbox/Game Pass build,
        and refusing reads is what used to hide live injury data.
        """
        if self.read_only:
            raise EditError(
                "Attached in read-only mode.",
                "Start this editor as administrator and press Load Game to write "
                "changes into the game.")

    # -- reading ---------------------------------------------------------
    def read_player(self, unique_id: int) -> LivePlayerRecord | None:
        address = self.game.find_person(unique_id)
        if not address:
            return None
        raw = self.game.read(address - BLOCK_BEFORE, BLOCK_SIZE)
        if not raw:
            return None
        record = LivePlayerRecord(unique_id=unique_id, address=address)
        uid = struct.unpack_from("<I", raw, BLOCK_BEFORE + 12)[0]
        if uid != unique_id:
            return None

        def direct(offset: int, fmt: str):
            return struct.unpack_from(fmt, raw, BLOCK_BEFORE + offset)[0]

        for column, (offset, fmt) in M.DIRECT_FIELDS.items():
            try:
                record.values[column] = direct(offset, fmt)
            except struct.error:
                continue
        for column, (offset, fmt) in M.DIRECT_POSITIVE_FIELDS.items():
            try:
                record.values[column] = direct(offset, fmt)
            except struct.error:
                continue

        # The attribute block: 71 bytes at person - 314, indexed at offset - 40.
        block = BLOCK_BEFORE + M.ATTRIBUTE_BLOCK_REL
        for column, offset in M.ATTRIBUTE_OFFSETS.items():
            index = block + offset - M.ATTRIBUTE_INDEX_BASE
            if 0 <= index < len(raw):
                record.values[column] = _shown_attribute(column, raw[index])

        # Registration: contract end and weekly wage.
        registration = struct.unpack_from("<Q", raw, BLOCK_BEFORE + M.REGISTRATION_POINTER_REL)[0]
        record.registration = registration
        # The injury reference sits inside the same block, so it costs nothing.
        try:
            record.injury_reference = struct.unpack_from(
                "<Q", raw, BLOCK_BEFORE + M.INJURY_REFERENCE_REL)[0]
        except struct.error:
            record.injury_reference = 0
        if registration > 0x10000:
            head = self.game.read(registration, 120)
            if head:
                salary = struct.unpack_from("<I", head, M.SALARY_WEEKLY_REL)[0]
                record.values["salary_weekly_raw"] = salary
                record.values["salary_pa"] = round(salary * 52 / 1000) * 1000
                record.values["contract_end_date"] = decode_date(
                    head, M.CONTRACT_DATE_REL)
                record.values["joined_club_date"] = decode_date(
                    head, M.JOINED_DATE_REL)
                record.values["transfer_status"] = head[M.TRANSFER_STATUS_REL]
        return record

    def read_live_values(self, unique_id: int) -> dict[str, Any] | None:
        """Every live value for one player, or None when he is not found.

        A pure read: it works on a read-only attach (see the module docstring).
        """
        record = self.read_player(unique_id)
        return record.values if record else None

    # -- injuries --------------------------------------------------------
    def read_injury(self, unique_id: int) -> LiveInjury | None:
        """The injury behind the player's pointer, or None when he is not found.

        A pure read: it works on a read-only attach, because injury data is
        display data and the read-only attach is the normal one.  A player who
        is fit reads as a :class:`LiveInjury` with ``injured`` false, and a
        broken chain reads the same way instead of raising - that is the
        contract ``db.refresh_injury`` relies on to fill the screen.
        """
        record = self.read_player(unique_id)
        if record is None:
            return None
        return self.game.read_injury(record.injury_reference)

    def read_current_screen(self, build: int | None = None, *,
                            rva: int | None = None) -> CurrentScreen | None:
        """Read the entity currently open in FM27, or None if unavailable.

        Pure read: works on a read-only attach too.
        """
        reader = getattr(self.game, "read_current_screen", None)
        if not callable(reader):
            return None
        return reader(build if build is not None else self.build, rva=rva)

    def read_person_display_name(self, unique_id: int) -> str:
        """Read FM27's live display name (common name or first + second name).

        Pure read: works on a read-only attach too.
        """
        reader = getattr(self.game, "read_person_display_name", None)
        if not callable(reader):
            return ""
        address = self.game.find_person(int(unique_id))
        if not address:
            return ""
        return reader(address) or ""

    # -- writing ---------------------------------------------------------
    def heal(self, unique_id: int) -> LiveInjury:
        """Clear the player's injury the way the original's Heal preset does.

        The preset writes ``IsInjured = false``. In the game that flag *is* the
        injury reference: the editor's own reader treats a non-zero reference
        with a readable item as injured, so the only honest way to clear it is
        to zero the reference. The item itself is left untouched, so the
        injury's history is not destroyed - it is simply no longer attached.

        Returns the injury that was cleared, so the caller can put it back.
        """
        self._require_write()
        record = self.read_player(unique_id)
        if record is None:
            raise EditError(
                "That player could not be found in the running game.",
                "Press Load Game again, then reopen the player.")
        previous = self.read_injury(unique_id) or LiveInjury(0, 0)
        address = record.address + M.INJURY_REFERENCE_REL
        old = self.game.read(address, 8)
        if old is None:
            raise EditError(
                "The game would not give this player's injury back, so it was "
                "not changed.")
        if old == b"\x00" * 8:
            return previous
        if not self.game.write(address, b"\x00" * 8):
            raise EditError(
                "Windows would not let this editor write into the game.",
                "Start this editor as administrator and try again.")
        back = self.game.read(address, 8)
        if back is None or back != b"\x00" * 8:
            self.game.write(address, old)
            raise EditError(
                "The game did not accept the heal.",
                "The original injury was put back.")
        self._remember(
            address, old, f"injury: cleared {previous.name or 'injury'}")
        self.writes += 1
        return previous

    def restore_injury(self, unique_id: int, reference: int) -> None:
        """Put a cleared injury reference back, for undo."""
        self._require_write()
        record = self.read_player(unique_id)
        if record is None or reference <= 0x10000:
            return
        address = record.address + M.INJURY_REFERENCE_REL
        old = self.game.read(address, 8)
        if old is None:
            return
        payload = struct.pack("<Q", reference)
        if self.game.write(address, payload):
            self._remember(address, old, "injury: restored")

    def read_many(self, known_ids, progress=None):
        """Read the live values for the players this snapshot knows.

        ``progress`` is called once per 5,000 pointers and once at the end, and
        it is optional.  That cadence is the contract, not an optimisation
        detail: the Load Game dialog pushes every call onto the Tk queue, so a
        per-player message would swamp it (a 42,344-player save would mean
        42,344 queue entries), and a missing callback must simply read.
        """
        known = {int(x) for x in known_ids if x}
        start, _end, count = self.game.people_bounds()
        raw = self.game.read(start, count * 8)
        if not raw:
            raise EditError("The game's player list could not be read.")
        pointers = struct.unpack(f"<{count}Q", raw)
        found: dict[int, Any] = {}
        for index, pointer in enumerate(pointers):
            if progress and index % 5000 == 0:
                progress("Reading players", index, count)
            if pointer < 0x10000:
                continue
            uid_raw = self.game.read(pointer + 12, 4)
            if not uid_raw:
                continue
            uid = struct.unpack("<I", uid_raw)[0]
            if uid not in known:
                continue
            block = self.game.read(pointer - BLOCK_BEFORE, BLOCK_SIZE)
            if not block:
                continue
            record = decode_record(block)
            if record:
                found[record.unique_id] = record
        if progress:
            progress("Reading players", count, count)
        return found

    # -- writing ---------------------------------------------------------
    def _target(self, record: LivePlayerRecord, column: str
                ) -> tuple[int, str, int] | None:
        """Address, struct format and storage multiplier for one column."""
        if column in M.DIRECT_FIELDS:
            offset, fmt = M.DIRECT_FIELDS[column]
            return record.address + offset, fmt, 1
        if column in M.DIRECT_POSITIVE_FIELDS:
            offset, fmt = M.DIRECT_POSITIVE_FIELDS[column]
            return record.address + offset, fmt, 1
        if column in M.ATTRIBUTE_OFFSETS:
            offset = M.ATTRIBUTE_OFFSETS[column]
            address = record.address + M.ATTRIBUTE_BLOCK_REL + offset - M.ATTRIBUTE_INDEX_BASE
            scale = 1 if column in M.POSITION_COLUMNS else M.ATTRIBUTE_STORAGE_SCALE
            return address, "<B", scale
        if column == "salary_weekly_raw":
            if record.registration <= 0x10000:
                return None
            return record.registration + M.SALARY_WEEKLY_REL, "<I", 1
        return None

    def write(self, unique_id: int, column: str, value: int,
              label: str = "") -> tuple[LivePlayerRecord, int]:
        """Write one integer field into the game and verify it stuck.

        Returns the record and the value that is now in memory.
        """
        self._require_write()
        record = self.read_player(unique_id)
        if record is None:
            raise EditError(
                "That player could not be found in the running game.",
                "Press Load Game again, then reopen the player.")
        target = self._target(record, column)
        if target is None:
            raise EditError(
                f"{label or column} is not a field this build can write.")
        address, fmt, scale = target

        value = _clamp(column, value)
        stored = value * scale

        size = struct.calcsize(fmt)
        old = self.game.read(address, size)
        if old is None:
            raise EditError(
                "The game would not give this value back, so it was not changed.",
                "Try reloading the game's data.")
        if not self.game.write_value(address, fmt, stored):
            raise EditError(
                "Windows would not let this editor write into the game.",
                "Start this editor as administrator and try again.")
        back = self.game.read(address, size)
        if back is None or struct.unpack(fmt, back)[0] != stored:
            # Put the old bytes back so a half-finished write cannot stay.
            self.game.write(address, old)
            raise EditError(
                "The game did not accept the new value.",
                "The original value was put back.")
        self._remember(address, old, f"{label or column}: {value}")
        self.writes += 1
        return record, value

    def undo(self) -> str | None:
        """Put the last recorded bytes back.  Only ever undoes a real write.

        Returns the label of the write it undid, or ``None`` when nothing was
        recorded - ``db.Snapshot`` calls this first and falls back to its own
        journal, so a read-only attach with no writes must return ``None``
        rather than raise.
        """
        if not self.undo_stack:
            return None
        address, old, label = self.undo_stack.pop()
        self.game.write(address, old)
        return label

    def write_date(self, unique_id: int, column: str, when: _date,
                   label: str = "") -> None:
        self._require_write()
        record = self.read_player(unique_id)
        if record is None or record.registration <= 0x10000:
            raise EditError("That contract could not be found in the game.")
        if column == "contract_end_date":
            base = record.registration + M.CONTRACT_DATE_REL
        elif column == "joined_club_date":
            base = record.registration + M.JOINED_DATE_REL
        else:
            raise EditError(f"{label or column} is not a date this build writes.")
        old = self.game.read(base, 4)
        if old is None:
            raise EditError("The game would not give this date back.")
        day = when.timetuple().tm_yday
        payload = struct.pack("<HH", day, when.year)
        if not self.game.write(base, payload):
            raise EditError("Windows would not let this editor write the date.")
        back = self.game.read(base, 4)
        if back is None or back[:2] != payload[:2] or back[2:4] != payload[2:4]:
            self.game.write(base, old)
            raise EditError("The game did not accept the new date.")
        self._remember(base, old, f"{label or column}: {when}")


def probe_write_support() -> tuple[bool, str]:
    """Can this session attach to the game with write rights?

    This is a cheap check used by the window: it opens the process with the
    write rights and closes it again without touching a single byte.
    """
    try:
        game, _report = open_game(write=True)
    except LiveError as exc:
        return False, exc.hint or str(exc)
    except Exception as exc:  # noqa: BLE001 - never leak a ctypes error
        return False, str(exc)
    game.close()
    return True, ""


def read_players(unique_ids) -> dict[int, dict[str, Any]]:
    """Read the live values for the players this snapshot already knows."""
    records, _report = read_live(unique_ids)
    return {
        uid: {
            "ca": r.ca, "pa": r.pa, "condition": r.condition,
            "match_sharpness": r.sharpness, "jadedness": r.jadedness,
            "morale": r.morale, "home_reputation": r.home_reputation,
            "current_reputation": r.current_reputation,
            "world_reputation": r.world_reputation,
        }
        for uid, r in records.items()
    }
