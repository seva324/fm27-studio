"""Access to the real FM27 snapshot, the live session and the edit journal.

This module is the data layer, and it owns three things so that no caller has
to own them itself:

**Reads.**  Every player, club, staff and fixture query the window needs,
already carrying the live overlay and the local edit overlay.  ``squad()`` and
``search_players()`` are the narrow projections the list screens draw;
``players_for_club()`` and ``query_players()`` are the full-row queries a role
fit needs (all fifty-four attributes, not the twenty a list shows).  The
filters cover what the screens and ``fmtools`` ask for - club, name or search
text, a position minimum, age and ability ranges, nationality, the
transfer-listed / injured flags, ordering and a limit - so a caller never has
to reach for ``db.conn``.  Reaching past this interface is what made the
live overlay easy to lose (``HANDOFF.md``: "不要绕过 db.py 查库", the value on
screen silently reverts to the database's copy).

**The live session.**  :meth:`Snapshot.attach_live`,
:meth:`Snapshot.detach_live` and :meth:`Snapshot.reload` own the lifecycle.
It used to be assembled by hand from the Load Game dialog - set ``editor``,
build ``live_meta``, assign it, ``apply_live``, ``flush_pending`` - and
``App.reload_snapshot`` therefore carried only the editor across a reload, so
after "Use last snapshot" every field the game had supplied fell back to the
snapshot's value and the assistant's game date and managed club reverted.

**The edit journal.**  One :class:`EditAction` per user action, however many
fields it touched.  ``Snapshot.history`` (per column) and
``LiveEditor.undo_stack`` (raw previous bytes) used to be pushed
independently: a plain edit pushed one to each, but ``heal_player`` pushed
three to one and one to the other, so Undo popped mismatched pairs; and
``redo()`` only re-applied to the local overlay, so Ctrl+Y after a live edit
looked right while the game kept the new value.

**Threads.**  One ``Snapshot`` is shared by the window's thread and the
assistant's worker thread (``assistant.ChatWorker`` runs the model call and its
tool calls on a ``threading.Thread``).  The SQLite connection is therefore
opened with ``check_same_thread=False`` and every read and every overlay
mutation holds one ``threading.RLock`` for its whole duration.  All SQL goes
through :meth:`Snapshot._fetchall` / :meth:`Snapshot._fetchone` /
:meth:`Snapshot._cursor`, so a new query cannot be written that forgets the
lock.  Before that, every worker-thread read raised ``ProgrammingError``:
``meta()`` swallowed it and returned ``{}`` (no game date, no build), and every
tool that touched players reported "no players are stored".

What this module does *not* do: it never opens the game process (``livewrite``
owns that) and it never writes the shipped database (edits are either written
into the running game or held in the in-memory overlay).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from typing import Any

from . import spec

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
DATA = os.path.join(APP, "data")
DB_PATH = os.path.join(DATA, "fm_snapshot.db")
SAVE_DIR = os.path.join(APP, "saves")
RELOAD_DIR = os.path.join(SAVE_DIR, "reloads")
EDITS_PATH = os.path.join(SAVE_DIR, "edits.json")

# The shipped snapshot, also used if a database is missing a table.
FALLBACK_COUNTS = {
    "players": 42344,
    "staff": 23309,
    "clubs": 42129,
    "competitions": 6625,
}
FALLBACK_GAME_DATE = "2026-07-20"

# The editable attributes the screens show: (display name, column, group), in
# screen order.  Declared once, in spec.py; this name is kept because the
# player page and the assistant import it from here.
ATTRIBUTE_COLUMNS = spec.ATTRIBUTE_COLUMNS

# The position strip: (short label, column), in the order the screens print.
# Derived from spec.POSITIONS, which is the one position table.
POSITIONS = [(position.label, position.column) for position in spec.POSITIONS]

# The twenty-odd columns a list screen draws.  ``squad()`` and
# ``search_players()`` project to these because a 400-row table does not need
# all 120 columns; a role fit uses ``players_for_club()`` instead.
#
# ``match_sharpness`` and ``jadedness`` are in the projection because the
# assistant prints them: ``aiprompt.save_context`` writes "sharp <value>" on
# every squad line, and while the column was missing every line read
# "sharp None" even though S5-SHARPNESS-FIRST is one of the always-on rules.
SQUAD_COLUMNS: tuple[str, ...] = (
    "id", "unique_id", "name", "club", "playing_club", "loan_club", "is_loaned_out",
    "age", "nationality", "ca", "pa",
    "asking_price", "salary_weekly_raw", "contract_end_date",
) + tuple(position.column for position in spec.POSITIONS) + (
    "condition", "match_sharpness", "jadedness", "morale", "injured",
)

SEARCH_COLUMNS: tuple[str, ...] = SQUAD_COLUMNS

# Every column a filter, an ordering or a projection may name.  The three
# interpolated identifiers (the filter column, the ORDER BY column and the
# projection) are checked against this, so a caller can never smuggle SQL in
# through a position name or a sort key.
SORTABLE_COLUMNS: frozenset[str] = frozenset(spec.KNOWN_COLUMN_SET) | frozenset({
    "id", "unique_id", "name", "club", "nationality", "age", "height_cm",
    "transfer_listed", "listed_for_loan", "transfer_agreed", "injured",
    "injury", "injury_start_date", "injury_expected_return",
    "contract_end_date", "asking_price", "salary_pa", "salary_weekly_raw",
    "date_of_birth", "playing_club", "loan_club", "is_loaned_out",
    "on_duty", "condition", "match_sharpness", "jadedness", "morale",
})

# The live-overlay columns ``heal`` clears, and the journal column it is
# recorded under.  One list, so undo/redo put exactly back what heal took.
INJURY_COLUMN = "injury"
INJURY_LIVE_COLUMNS: tuple[str, ...] = (
    "injured", "injury", "injury_start_date",
    "injury_light_training_days_remaining",
    "injury_full_training_days_remaining",
    "injury_min_days_remaining",
    "injury_max_days_remaining",
    "injury_expected_return",
)

# The contract dates the game keeps in the registration block.  They are ISO
# strings on screen and a (day-of-year, year) pair in the game, so they need
# the editor's own ``write_date`` instead of the numeric ``write``.
DATE_COLUMNS: tuple[str, ...] = ("contract_end_date", "joined_club_date")

# Every player column the live overlay can rewrite: the nine ``apply_live``
# fills from one player record plus the injury block ``heal`` clears.  A filter
# on one of these can be invalidated *after* SQLite has applied its ``LIMIT``,
# which is why ``query_players`` stops pushing the limit down for them; see the
# note there (D5: ``injured_players(6)`` returning three rows).
LIVE_OVERLAY_COLUMNS: frozenset[str] = frozenset({
    "ca", "pa", "condition", "match_sharpness", "jadedness", "morale",
    "home_reputation", "current_reputation", "world_reputation",
    "display_name", "asking_price", "salary_weekly_raw", "salary_pa",
    "contract_end_date", "joined_club_date", "transfer_listed",
    "listed_for_loan",
}) | frozenset(INJURY_LIVE_COLUMNS)
LIVE_CLUB_OVERLAY_COLUMNS: frozenset[str] = frozenset({
    "club", "playing_club", "loan_club", "is_loaned_out",
})

CLUB_DISPLAY = {
    "Milan": "AC Milan",
    "Inter": "Inter",
    "Juventus": "Juventus",
    "Roma": "AS Roma",
    "Lazio": "Lazio",
    "Napoli": "Napoli",
    "Leicester": "Leicester City",
    "Manchester City": "Manchester City",
    "Manchester United": "Manchester United",
    "Liverpool": "Liverpool",
    "Arsenal": "Arsenal",
    "Chelsea": "Chelsea",
    "Tottenham": "Tottenham Hotspur",
    "Newcastle": "Newcastle United",
    "Real Madrid": "Real Madrid",
    "Barcelona": "FC Barcelona",
    "Atlético Madrid": "Atlético Madrid",
    "Bayern Munich": "Bayern Munich",
    "Dortmund": "Borussia Dortmund",
    "Paris Saint-Germain": "Paris Saint-Germain",
}

SHIRT_HINTS = {"92074020": 11}

STATUS_LABELS = {
    "friendly": "Friendly",
    "友谊赛": "Friendly",
}


def _split_name(full: str) -> tuple[str, str, str]:
    text = (full or "").strip()
    if not text:
        return "", "", ""
    parts = text.split()
    if len(parts) == 1:
        return parts[0], "", text
    return parts[0], " ".join(parts[1:]), text


def _fmt_date(value: str | None) -> str:
    if not value:
        return "—"
    return value.replace("-", "/")


def _fmt_dob(value: str | None) -> str:
    if not value:
        return "—"
    try:
        stamp = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return value
    return f"{stamp.year}/{stamp.month}/{stamp.day}"


def _as_date(value) -> _date | None:
    """One screen date as a real date, or None.

    The contract boxes accept ``2027-06-30``, ``2027/06/30`` and
    ``2027.06.30`` (the same three spellings ``_commit_date`` parses), and a
    staged overlay value is always ISO.
    """
    if isinstance(value, _date):
        return value
    text = str(value or "").strip().replace("/", "-").replace(".", "-")
    if not text:
        return None
    try:
        return _date.fromisoformat(text)
    except ValueError:
        return None


def _position_column(name: str) -> str:
    """Resolve a position column, short label or template name to its column.

    All three spellings are in use: ``striker`` in the database,
    ``ST`` on the screens (``spec.POSITIONS``) and ``Striker`` in a tactic
    template (``fmtools.POSITION_FIELDS``).  ``spec`` is the one table, so a
    new position is known here automatically.
    """
    text = str(name or "").strip()
    if text in spec.POSITION_COLUMNS:
        return text
    if text in spec.POSITION_LABEL_TO_COLUMN:
        return spec.POSITION_LABEL_TO_COLUMN[text]
    if text in spec.POSITION_FIELDS:
        return spec.POSITION_FIELDS[text]
    lowered = {key.lower(): value for key, value in spec.POSITION_FIELDS.items()}
    if text.lower() in lowered:
        return lowered[text.lower()]
    raise ValueError(
        f"{name!r} is not a position: use a column ({', '.join(sorted(spec.POSITION_COLUMNS))}),"
        " a label (ST) or a template name (Striker)")


def _ordered(rows: list[dict[str, Any]], order: str,
             descending: bool) -> list[dict[str, Any]]:
    """Sort the overlaid rows, with the rows that have no value last.

    Sorting happens twice on purpose: SQLite selects the candidate rows in the
    stored order, then this fixes the order the *screen* sees.  A live value
    that rewrites the sort column (CA, condition) would otherwise leave the
    list in the database's order.
    """
    present = [row for row in rows if row.get(order) is not None]
    missing = [row for row in rows if row.get(order) is None]
    try:
        present.sort(key=lambda row: row[order], reverse=descending)
    except TypeError:  # a mixed-type column (never expected): compare as text
        present.sort(key=lambda row: str(row[order]), reverse=descending)
    return present + missing


@dataclass
class EditChange:
    """One column one user action touched, and everything undo needs.

    ``old`` is the value the screen showed *before* the edit - the live value
    when the game supplied one - and ``new`` is the value that landed.  Both
    are in the screen's units, so undo/redo can go back through
    ``LiveEditor.write`` (which re-checks the player's identity, clamps and
    reads the value back) instead of poking raw addresses.

    ``in_game`` says the new value went into the running game through the
    editor; ``False`` means it lives in the local ``edits`` overlay because no
    game was attached or the column has no address.
    """

    key: str
    table: str
    row_id: int
    column: str
    old: Any
    new: Any
    uid: int | None = None
    in_game: bool = False
    # True when a later edit to the same field was flushed while this one was
    # still pending, so the game received this value as part of that chain.
    # Undo/redo then has to go through the editor as well: the game is holding
    # this field, and putting the old value in the local overlay alone would
    # leave the game and the screen disagreeing.
    superseded: bool = False
    # A heal clears the injury reference; undo puts it back.
    injury_reference: int = 0
    # A heal also replaces the player's live injury fields; these are the
    # values before and after, so undo/redo can put the screen right too.
    live_before: dict[str, Any] | None = None
    live_after: dict[str, Any] | None = None


@dataclass
class EditAction:
    """One user action: every field it touched, in the order it applied them.

    This is the unit of undo.  ``heal`` touches four things (condition,
    sharpness, jadedness and the injury reference) and is one action, so the
    undo stack can no longer drift away from the number of things written.
    """

    label: str
    changes: list[EditChange] = field(default_factory=list)
    uid: int | None = None

    @property
    def key(self) -> str:
        """``table:row:column`` for one field, ``table:row:label`` for many.

        ``App.undo``/``App.redo`` print ``key.split(":")[-1]``, so a heal
        reads "heal" and a plain edit reads the column name.
        """
        if len(self.changes) == 1:
            return self.changes[0].key
        first = self.changes[0] if self.changes else None
        if first is None:
            return self.label
        return f"{first.table}:{first.row_id}:{self.label}"


_FOLD_EXTRA = str.maketrans({
    "ø": "o", "Ø": "o",
    "đ": "d", "Đ": "d",
    "ð": "d", "Ð": "d",
    "ł": "l", "Ł": "l",
    "ß": "ss",
    "æ": "ae", "Æ": "ae",
    "œ": "oe", "Œ": "oe",
    "þ": "th", "Þ": "th",
    "ı": "i", "İ": "i",
    "’": "'", "‘": "'",
})


def fold_text(value: str | None) -> str:
    """Fold Unicode accents/diacritics and case for football name search.

    ``"Luka Modrić"`` -> ``"luka modric"``, ``"Kylian Mbappé"`` -> ``"kylian mbappe"``,
    ``"Martin Ødegaard"`` -> ``"martin odegaard"``.
    """
    if not value:
        return ""
    translated = str(value).translate(_FOLD_EXTRA)
    decomposed = unicodedata.normalize("NFKD", translated)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower().strip()


def _damerau_within(a: str, b: str, max_dist: int) -> int:
    """Bounded Damerau-Levenshtein distance; returns ``max_dist + 1`` when exceeded."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    if a == b:
        return 0
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev2 = list(range(lb + 1))
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        ai = a[i - 1]
        row_min = i
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            val = min(
                prev[j] + 1,          # deletion
                curr[j - 1] + 1,      # insertion
                prev[j - 1] + cost,   # substitution
            )
            if i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == b[j - 1]:
                trans = prev2[j - 2] + 1
                if trans < val:
                    val = trans
            curr[j] = val
            if val < row_min:
                row_min = val
        if row_min > max_dist:
            return max_dist + 1
        prev2, prev = prev, curr
    return prev[lb]


def _token_fuzzy_score(token: str, words: Sequence[str]) -> float:
    """Score one query token against a target's folded words (0.0 = no match)."""
    tlen = len(token)
    if tlen == 0:
        return 1.0
    best = 0.0
    for w in words:
        if token == w:
            return 1.25
        if w.startswith(token):
            best = max(best, 1.1)
            continue
        if token in w:
            best = max(best, 1.0)
            continue
    if best >= 1.0 or tlen < 3:
        return best
    max_d = 1 if tlen <= 5 else 2
    t0 = token[0]
    for w in words:
        wlen = len(w)
        if wlen < 2:
            continue
        # Fast prefilter: must share first char (or second char for 1st-char typo)
        # and length within tolerance.
        cand_word = w
        if wlen > tlen + max_d:
            # Also check prefix of longer word (e.g. "modrics" vs "modricovic")
            cand_word = w[:tlen]
        if t0 != cand_word[0] and (tlen < 2 or token[1] != cand_word[0]) and (len(cand_word) < 2 or t0 != cand_word[1]):
            continue
        dist = _damerau_within(token, cand_word, max_d)
        if dist <= max_d:
            score = 0.92 - 0.14 * dist - (0.04 if cand_word != w else 0.0)
            if score > best:
                best = score
    return best


def fuzzy_match_score(query_folded: str, *folded_targets: str) -> float:
    """Match a folded query against folded name/club strings with accent & typo tolerance.

    Returns ``>= 1.0`` for exact/diacritic-folded substring or word matches,
    ``0.5..0.95`` for typo-tolerant fuzzy matches (e.g. ``Modrics`` -> ``Luka Modrić``),
    or ``0.0`` when unmatched.
    """
    if not query_folded:
        return 1.0
    for target in folded_targets:
        if target and query_folded in target:
            if target == query_folded:
                return 1.3
            if target.startswith(query_folded) or f" {query_folded}" in target:
                return 1.15
            return 1.0
    tokens = [t for t in query_folded.split() if t]
    if not tokens:
        return 1.0
    words: list[str] = []
    for target in folded_targets:
        if target:
            words.extend(target.replace("-", " ").split())
    if not words:
        return 0.0
    total = 0.0
    for tok in tokens:
        sc = _token_fuzzy_score(tok, words)
        if sc <= 0.0:
            return 0.0
        total += sc
    return total / len(tokens)


class Snapshot:
    """The snapshot database, the live session and the edit journal."""

    def __init__(self, path: str = DB_PATH):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        self.path = path
        # One connection, shared by the window's thread and the assistant's
        # worker thread.  ``check_same_thread=False`` is what makes that legal;
        # ``_lock`` is what makes it safe.  Every read and every overlay
        # mutation holds it for its whole duration (see the gate below), so two
        # threads can never interleave a ``fetchone`` with a write to the same
        # connection, and a reader always sees a complete overlay.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Re-entrant: ``reload`` holds it and calls ``meta``/``_load_edits``,
        # which take it again.
        self._lock = threading.RLock()
        self.edits: dict[str, Any] = {}
        # One entry per user action; this is the only undo stack.
        self.journal: list[EditAction] = []
        # Actions that were undone and can be applied again, newest last.
        self.redo_stack: list[EditAction] = []
        self.saved_edits: dict[str, Any] = {}
        self.live: dict[int, dict[str, Any]] = {}
        self.club_live: dict[str, dict[str, Any]] = {}
        self._live_has_clubs: bool = False
        self.live_meta: dict[str, str] = {}
        # The attached game, when the person has pressed Load Game. Edits go
        # into the game through this; the snapshot is only the reading copy.
        # ``attach_live``/``detach_live`` own it; it stays a plain attribute so
        # a caller (and the tests) can look at what is attached.
        self.editor = None
        # What the last flush into the game did: (written, failed).
        self.last_flush: tuple[int, int] = (0, 0)
        self.legacy_edits = 0
        self.last_edit = "local"
        self.last_error = ""
        # The value the data layer actually stored for the last successful
        # edit - the clamped value, or the value the game read back.  The
        # screens show this rather than the number that was typed, so typing
        # 99 into a position shows 20, and a live morale of 80 stays 80.
        self.last_value: Any = None
        # Lazy folded-text search caches (invalidated on reload).
        self._player_search_index: list[tuple[int, str, str, str]] | None = None
        self._staff_search_index: list[tuple[int, str, str, str]] | None = None
        self._club_search_index: list[tuple[int, str, str]] | None = None
        self._load_edits()

    # -- the one SQL gate -------------------------------------------------
    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Every row of one query, with the connection's lock held.

        This - and ``_fetchone`` - is the only place this module touches
        ``self.conn``.  One gate means a new query cannot be written that
        forgets the lock, which is what "SQLite objects ... can only be used in
        that same thread" used to look like from the assistant.
        """
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """One row of one query, or None, with the connection's lock held."""
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _cursor(self, sql: str, params: tuple = ()):
        """A query's cursor, for a read that is too big to materialise at once.

        The caller must hold ``self._lock`` for as long as it iterates: the
        cursor is a view over the connection, not a copy.
        """
        return self.conn.execute(sql, params)

    @staticmethod
    def _close_editor(editor) -> None:
        """Release the game handle one attach owned; never raise.

        ``LiveEditor.close`` closes the process handle (the Win32 handle on a
        real attach).  Forgetting it leaked one handle per "Load Game", and the
        game's own process object is not garbage-collected deterministically.
        """
        if editor is None:
            return
        close = getattr(editor, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - a handle we cannot close is not fatal
            pass

    # -- metadata ---------------------------------------------------------
    def meta(self) -> dict[str, str]:
        with self._lock:
            out = {}
            try:
                for row in self._fetchall(
                        "SELECT meta_key, meta_value FROM metadata"):
                    out[row["meta_key"]] = row["meta_value"]
            except sqlite3.Error:
                pass
            if self.live_meta:
                out.update({k: v for k, v in self.live_meta.items() if v})
            return out

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("players", "clubs", "competitions", "staff"):
            try:
                value = self._fetchone(
                    f"SELECT COUNT(*) FROM {table}")[0]
            except sqlite3.Error:
                value = 0
            out[table] = value or FALLBACK_COUNTS.get(table, 0)
        return out

    # -- reads ------------------------------------------------------------
    def player(self, key: int) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM players WHERE unique_id=? OR id=? ORDER BY "
            "CASE WHEN unique_id=? THEN 0 ELSE 1 END LIMIT 1",
            (key, key, key))
        return self.apply_overlay("players", dict(row)) if row else None

    def player_by_id(self, row_id: int) -> dict[str, Any] | None:
        row = self._fetchone("SELECT * FROM players WHERE id=?", (row_id,))
        return self.apply_overlay("players", dict(row)) if row else None

    def _loaned_in_players(self, club: str, columns: Sequence[str] | str = "*") -> list[dict[str, Any]]:
        """Players currently on loan IN to ``club`` (owned by another club)."""
        name = (club or "").strip()
        if not name:
            return []
        wanted = self._projection(columns)
        need_cols = ("id", "unique_id", "club", "playing_club", "loan_club")
        selected = "*" if wanted is None else ", ".join(
            f'"{col}"' for col in dict.fromkeys((*need_cols, *wanted)))
        with self._lock:
            live_loan_uids = [
                uid for uid, entry in self.live.items()
                if (entry.get("playing_club") == name or entry.get("loan_club") == name)
                and entry.get("club") != name
            ]
            params: list[Any] = [name, name, name]
            extra_clause = ""
            if live_loan_uids and len(live_loan_uids) <= 5000:
                ph = ",".join("?" for _ in live_loan_uids)
                extra_clause = f" OR unique_id IN ({ph})"
                params.extend(live_loan_uids)
            sql = (
                f"SELECT {selected} FROM players "
                f"WHERE ((playing_club=? OR loan_club=?) AND (club IS NULL OR club!=?)){extra_clause}"
            )
            rows = []
            for r in self._fetchall(sql, tuple(params)):
                row = self.apply_overlay("players", dict(r))
                if (row.get("playing_club") == name or row.get("loan_club") == name) and row.get("club") != name:
                    rows.append(row)
        if wanted is not None:
            rows = [{k: row.get(k) for k in wanted} for row in rows]
        return rows

    def squad(self, club: str, limit: int = 300) -> list[dict[str, Any]]:
        """One club's players (owned + loaned in) in list columns, best first."""
        name = (club or "").strip()
        if not name:
            return []
        owned = self.query_players(club=name, limit=limit, columns=SQUAD_COLUMNS)
        seen = {row.get("id") for row in owned}
        loaned_in = [r for r in self._loaned_in_players(name, columns=SQUAD_COLUMNS)
                     if r.get("id") not in seen]
        if not loaned_in:
            return owned
        combined = _ordered(owned + loaned_in, "ca", True)
        return combined[:limit] if limit is not None else combined

    def search_players(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        """The search box's own rule: the text in the player's name or club."""
        return self.query_players(search=query, limit=limit,
                                  columns=SEARCH_COLUMNS)

    def players_for_club(self, club: str, limit: int = 300,
                         order: str = "ca",
                         descending: bool = True) -> list[dict[str, Any]]:
        """Every column for one club's players (owned + loaned in), with overlays applied.

        ``squad()`` selects the twenty columns a list needs; a role fit needs
        all fifty-four attributes, so this returns the whole row - which is
        what ``fmtools._squad_rows`` used to do with its own SQL.
        """
        name = (club or "").strip()
        if not name:
            return []
        owned = self.query_players(club=name, limit=limit, order=order,
                                   descending=descending, columns="*")
        seen = {row.get("id") for row in owned}
        loaned_in = [r for r in self._loaned_in_players(name, columns="*")
                     if r.get("id") not in seen]
        if not loaned_in:
            return owned
        combined = _ordered(owned + loaned_in, order, descending)
        return combined[:limit] if limit is not None else combined

    def _ensure_player_search_index(self) -> list[tuple[int, str, str, str]]:
        if self._player_search_index is None:
            rows = self._fetchall("SELECT id, unique_id, name, club FROM players")
            idx: list[tuple[int, str, str, str]] = []
            for r in rows:
                uid = int(r["unique_id"] or 0)
                live = self.live.get(uid) if uid else None
                raw_club = (live.get("club") if live and live.get("club") else None) or r["club"] or ""
                play_club = (live.get("playing_club") if live and live.get("playing_club") else None) or ""
                disp_club = CLUB_DISPLAY.get(raw_club, raw_club)
                extra = disp_club if disp_club != raw_club else ""
                if play_club and play_club != raw_club:
                    extra = f"{extra} {play_club}".strip()
                idx.append((
                    int(r["id"]),
                    fold_text(r["name"]),
                    fold_text(raw_club),
                    fold_text(extra) if extra else "",
                ))
            self._player_search_index = idx
        return self._player_search_index

    def _match_player_ids(self, text: str, *, include_club: bool) -> dict[int, float]:
        q = fold_text(text)
        if not q:
            return {}
        index = self._ensure_player_search_index()
        tokens = [t for t in q.split() if t]
        exact: dict[int, float] = {}
        for row_id, fn, fc, fcd in index:
            if q in fn:
                exact[row_id] = 1.2 if (fn == q or fn.startswith(q) or f" {q}" in fn) else 1.05
                continue
            if include_club and ((fc and q in fc) or (fcd and q in fcd)):
                exact[row_id] = 1.0
                continue
            if len(tokens) > 1:
                hay = f"{fn} {fc} {fcd}" if include_club else fn
                if all(t in hay for t in tokens):
                    exact[row_id] = 1.02
        if exact or len(q) < 3:
            return exact
        # Typo-tolerant fuzzy fallback when no exact/diacritic-folded match exists
        # (e.g. "Modrics" -> "Luka Modrić", "Mbape" -> "Kylian Mbappé Lottin")
        fuzzy: dict[int, float] = {}
        for row_id, fn, fc, fcd in index:
            sc = fuzzy_match_score(q, fn, fc, fcd) if include_club else fuzzy_match_score(q, fn)
            if sc > 0.0:
                fuzzy[row_id] = sc
        return fuzzy

    def _ensure_staff_search_index(self) -> list[tuple[int, str, str, str]]:
        if self._staff_search_index is None:
            rows = self._fetchall("SELECT id, name, club, job FROM staff")
            self._staff_search_index = [
                (int(r["id"]), fold_text(r["name"]), fold_text(r["club"]), fold_text(r["job"]))
                for r in rows
            ]
        return self._staff_search_index

    def _ensure_club_search_index(self) -> list[tuple[int, str, str]]:
        if self._club_search_index is None:
            rows = self._fetchall("SELECT id, name FROM clubs")
            idx: list[tuple[int, str, str]] = []
            for r in rows:
                raw = r["name"] or ""
                disp = CLUB_DISPLAY.get(raw, raw)
                idx.append((int(r["id"]), fold_text(raw), fold_text(disp) if disp != raw else ""))
            self._club_search_index = idx
        return self._club_search_index

    def query_players(self, *,
                      club: str | None = None,
                      name: str | None = None,
                      search: str | None = None,
                      position: str | None = None,
                      position_min: int | None = None,
                      min_age: int | None = None,
                      max_age: int | None = None,
                      min_ca: int | None = None,
                      max_ca: int | None = None,
                      min_pa: int | None = None,
                      max_pa: int | None = None,
                      nationality: str | None = None,
                      transfer_listed: bool | None = None,
                      injured: bool | None = None,
                      exclude_free_agents: bool = False,
                      attr_filters: Sequence[tuple[str, int | None, int | None]] | None = None,
                      order: str = "ca",
                      descending: bool = True,
                      limit: int | None = 200,
                      columns: Sequence[str] | str = "*"
                      ) -> list[dict[str, Any]]:
        """Players matching every filter, with the live overlay applied.

        ``name`` and ``search`` are accent-insensitive (``Modric`` finds
        ``Luka Modrić``) and fall back to typo-tolerant fuzzy matching when
        exact folded matches are scarce (``Modrics`` also finds ``Luka Modrić``).
        """
        if order not in SORTABLE_COLUMNS:
            raise ValueError(f"{order!r} is not a column that can be sorted on")
        if limit is not None and int(limit) <= 0:
            raise ValueError("limit must be a positive integer, or None")
        wanted = self._projection(columns)

        filters: list[tuple[str, str, Any]] = []
        text_scores: dict[int, float] | None = None
        if club is not None:
            filters.append(("club", "eq", club))
        if position is not None:
            filters.append((_position_column(position), "min",
                            1 if position_min is None else int(position_min)))
        for column, value, op in (("age", min_age, "min"), ("age", max_age, "max"),
                                  ("ca", min_ca, "min"), ("ca", max_ca, "max"),
                                  ("pa", min_pa, "min"), ("pa", max_pa, "max")):
            if value is not None:
                filters.append((column, op, int(value)))
        if attr_filters:
            for item in attr_filters:
                if not isinstance(item, (list, tuple)) or len(item) != 3:
                    continue
                col, lo, hi = str(item[0] or "").strip().lower(), item[1], item[2]
                if col not in spec.KNOWN_COLUMN_SET:
                    continue
                if lo is not None:
                    filters.append((col, "min", int(lo)))
                if hi is not None:
                    filters.append((col, "max", int(hi)))
        if nationality is not None:
            filters.append(("nationality", "eq", nationality))
        if transfer_listed is not None:
            filters.append(("transfer_listed", "eq", 1 if transfer_listed else 0))
        if injured is not None:
            filters.append(("injured", "eq", 1 if injured else 0))
        if exclude_free_agents:
            filters.append(("club", "not_blank", None))

        with self._lock:
            if name and name.strip():
                matched = self._match_player_ids(name, include_club=False)
                if not matched:
                    return []
                text_scores = matched
            if search and search.strip():
                matched = self._match_player_ids(search, include_club=True)
                if not matched:
                    return []
                if text_scores is None:
                    text_scores = matched
                else:
                    text_scores = {
                        k: min(text_scores[k], matched[k])
                        for k in text_scores.keys() & matched.keys()
                    }
                    if not text_scores:
                        return []

            clauses, params = [], []
            for column, op, value in filters:
                if self.live and op == "eq" and column in ("club", "injured", "transfer_listed", "listed_for_loan"):
                    if column in ("injured", "transfer_listed", "listed_for_loan") and value == 0:
                        # Live overlay can clear snapshot flags (e.g. recovered injuries);
                        # skip SQL pushdown so _overlay_page + _row_matches sees them.
                        continue
                    live_uids = [
                        uid for uid, entry in self.live.items()
                        if entry.get(column) == value
                    ]
                    if len(live_uids) > 5000:
                        continue
                    clause, values = self._sql_condition(column, op, value)
                    if live_uids:
                        ph = ",".join("?" for _ in live_uids)
                        clauses.append(f"({clause} OR unique_id IN ({ph}))")
                        params.extend(values)
                        params.extend(live_uids)
                    else:
                        clauses.append(clause)
                        params.extend(values)
                    continue
                clause, values = self._sql_condition(column, op, value)
                clauses.append(clause)
                params.extend(values)

            id_set_filter: set[int] | None = None
            if text_scores is not None:
                if len(text_scores) <= 850:
                    placeholders = ",".join("?" for _ in text_scores)
                    clauses.append(f"id IN ({placeholders})")
                    params.extend(text_scores.keys())
                else:
                    id_set_filter = set(text_scores.keys())

            # When the overlay or fuzzy text ranking applies, cut after Python pass.
            after_overlay = (
                limit is not None
                and (self._overlay_can_change(filters) or id_set_filter is not None)
            )
            has_fuzzy = bool(text_scores and any(v < 1.0 for v in text_scores.values()))
            selected = "*" if wanted is None else ", ".join(
                f'"{column}"' for column in
                self._scan_columns(wanted, filters, order))
            sql = f"SELECT {selected} FROM players"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += f' ORDER BY "{order}" {"DESC" if descending else "ASC"}'

            if id_set_filter is not None:
                rows: list[dict[str, Any]] = []
                for record in self._cursor(sql, tuple(params)):
                    if int(record["id"]) not in id_set_filter:
                        continue
                    row = self.apply_overlay("players", dict(record))
                    if all(self._row_matches(row, c, o, v) for c, o, v in filters):
                        rows.append(row)
                        if limit is not None and not self._overlay_can_change(filters) and len(rows) >= int(limit):
                            break
                rows = _ordered(rows, order, descending)
                if limit is not None:
                    rows = rows[:int(limit)]
                if wanted is not None:
                    rows = [{k: row.get(k) for k in wanted} for row in rows]
                return rows

            if after_overlay and not has_fuzzy:
                return self._overlay_page(sql, tuple(params), filters, order,
                                          descending, int(limit), wanted)
            if limit is not None and not has_fuzzy:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = self._scan_rows(sql, tuple(params), filters)[0]
            rows = _ordered(rows, order, descending)
            if has_fuzzy and text_scores is not None:
                # Exact/diacritic-folded matches first (in requested order), then
                # fuzzy matches sorted by match score then requested order.
                exact_rows = [r for r in rows if text_scores.get(int(r["id"]), 0.0) >= 1.0]
                fuzzy_rows = [r for r in rows if text_scores.get(int(r["id"]), 0.0) < 1.0]
                fuzzy_rows.sort(
                    key=lambda r: round(text_scores.get(int(r["id"]), 0.0), 3),
                    reverse=True,
                )
                rows = exact_rows + fuzzy_rows
            if limit is not None:
                rows = rows[:int(limit)]
            if wanted is not None:
                rows = [{k: row.get(k) for k in wanted} for row in rows]
            return rows

    def _scan_rows(self, sql: str, params: tuple,
                   filters: Sequence[tuple[str, str, Any]]
                   ) -> tuple[list[dict[str, Any]], int]:
        """Run one scan and keep the rows the screen would accept.

        Returns ``(rows, examined)``: ``examined`` counts the rows SQLite
        handed back, which is how :meth:`_overlay_page` tells a full window
        apart from a scan that simply had no more rows.
        """
        rows: list[dict[str, Any]] = []
        examined = 0
        for record in self._cursor(sql, params):
            examined += 1
            row = self.apply_overlay("players", dict(record))
            if all(self._row_matches(row, column, op, value)
                   for column, op, value in filters):
                rows.append(row)
        return rows, examined

    def _overlay_page(self, sql: str, params: tuple,
                      filters: Sequence[tuple[str, str, Any]],
                      order: str, descending: bool, limit: int,
                      wanted: Sequence[str] | None) -> list[dict[str, Any]]:
        """One page of ``limit`` rows, cut after the overlay (D5).

        The first query reads a window of ``max(2 * limit, limit + 64)`` rows:
        enough spare rows for the usual few disqualifications, and still a
        small read because only the columns the query needs are selected.  When
        the window was full and still too few rows survived, the scan is
        repeated without a limit: anything less could return a short page that
        a full scan would fill.
        """
        window = max(limit * 2, limit + 64)
        rows, examined = self._scan_rows(sql + " LIMIT ?",
                                         params + (window,), filters)
        if len(rows) < limit and examined >= window:
            rows, _examined = self._scan_rows(sql, params, filters)
        rows = _ordered(rows, order, descending)[:limit]
        if wanted is not None:
            rows = [{name: row.get(name) for name in wanted} for row in rows]
        return rows

    @staticmethod
    def _scan_columns(wanted: Sequence[str],
                      filters: Sequence[tuple[str, str, Any]],
                      order: str) -> list[str]:
        """The columns one query still needs after the projection narrows it.

        A projected query only ever reads its own columns back, so the SQL can
        select exactly those plus the ones the filters, the overlay (``id``,
        ``unique_id``) and the ordering need.  That is what keeps a full scan
        affordable: a dozen columns instead of a hundred and twenty.
        """
        names: list[str] = ["id", "unique_id", order]
        names.extend(wanted)
        for column, _op, _value in filters:
            names.extend(("name", "club") if column == "name|club"
                         else (column,))
        return list(dict.fromkeys(names))

    def _overlay_can_change(self, filters: Sequence[tuple[str, str, Any]]) -> bool:
        """True when the overlay or a staged edit can rewrite a filtered field.

        Coarse on purpose: any live value at all makes the whole
        :data:`LIVE_OVERLAY_COLUMNS` set mutable, because the per-player check
        would cost a dictionary lookup per filter and per row for a decision
        that is the same for the whole query.
        """
        if not filters:
            return False
        mutable: set[str] = set()
        if self.live:
            mutable |= LIVE_OVERLAY_COLUMNS
            if self._live_has_clubs:
                mutable |= LIVE_CLUB_OVERLAY_COLUMNS
        if self.edits:
            mutable |= {key.rsplit(":", 1)[-1] for key in self.edits}
        if not mutable:
            return False
        return any(column in mutable for column, _op, _value in filters)

    def injured_players(self, limit: int = 6) -> list[dict[str, Any]]:
        """The most notable injured players, for the assistant's context.

        "Notable" is the same rule the assistant used in its own SQL: the
        injured players with the highest current ability.
        """
        return self.query_players(
            injured=True, order="ca", limit=limit,
            columns=("unique_id", "name", "club", "injury", "injured", "ca",
                     "pa", "age", "injury_start_date",
                     "injury_expected_return", "condition",
                     "match_sharpness"))

    def top_player(self, order: str = "ca") -> dict[str, Any] | None:
        """The leading player by current ability, for ``App.focus_player``.

        The candidates are the database's leading fifty by the same column;
        the winner is the best of those once the live overlay is applied, so a
        player the game has improved can win it.
        """
        rows = self.query_players(order=order, limit=50)
        return rows[0] if rows else None

    @staticmethod
    def _projection(columns: Sequence[str] | str) -> tuple[str, ...] | None:
        """Validate a requested projection; ``None`` means every column."""
        if columns == "*" or columns is None:
            return None
        names = tuple(str(column) for column in columns)
        unknown = [name for name in names if name not in SORTABLE_COLUMNS]
        if unknown:
            raise ValueError(f"not player columns: {', '.join(sorted(unknown))}")
        return names

    @staticmethod
    def _sql_condition(column: str, op: str, value: Any) -> tuple[str, list[Any]]:
        """One filter as SQL, with its parameters."""
        if op == "eq":
            return f'"{column}" = ?', [value]
        if op == "min":
            return f'"{column}" >= ?', [value]
        if op == "max":
            return f'"{column}" <= ?', [value]
        if op == "contains":
            text = f"%{str(value).strip()}%"
            if column == "name|club":
                return "(name LIKE ? OR club LIKE ?)", [text, text]
            return f'"{column}" LIKE ?', [text]
        if op == "not_blank":
            return f'("{column}" IS NOT NULL AND TRIM("{column}") <> \'\')', []
        raise ValueError(f"unknown filter {op!r}")

    @staticmethod
    def _row_matches(row: dict[str, Any], column: str, op: str, value: Any) -> bool:
        """The same filter, applied to a row the screen is about to show.

        This is the second half of the two-pass filter: SQLite narrows the
        table, this makes sure a value the live overlay rewrote still obeys
        the filter the person typed.
        """
        if op == "not_blank":
            return bool(str(row.get(column) or "").strip())
        if op == "eq":
            actual = row.get(column)
            if actual is None:
                return False
            return actual == value
        if op == "contains":
            needle = str(value).strip().lower()
            if not needle:
                return True
            columns = ("name", "club") if column == "name|club" else (column,)
            return any(needle in str(row.get(name) or "").lower()
                       for name in columns)
        actual = row.get(column)
        if actual is None:
            return False
        try:
            return (int(actual) >= int(value) if op == "min"
                    else int(actual) <= int(value))
        except (TypeError, ValueError):
            return False

    def club(self, key) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM clubs WHERE id=? LIMIT 1", (key,))
        if row:
            return self.apply_overlay("clubs", dict(row))
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            row = self._fetchone(
                "SELECT * FROM clubs WHERE unique_id=? LIMIT 1", (int(key),))
            if row:
                return self.apply_overlay("clubs", dict(row))
        row = self._fetchone(
            "SELECT * FROM clubs WHERE name=? AND competition NOT LIKE '%Primavera%' "
            "AND competition NOT LIKE '%Under%' ORDER BY reputation DESC LIMIT 1",
            (str(key),))
        return self.apply_overlay("clubs", dict(row)) if row else None

    def club_by_unique_id(self, unique_id: int) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM clubs WHERE unique_id=? LIMIT 1", (int(unique_id),))
        return self.apply_overlay("clubs", dict(row)) if row else None

    def club_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM clubs WHERE name=? AND competition NOT LIKE '%Primavera%' "
            "AND competition NOT LIKE '%Under%' ORDER BY reputation DESC LIMIT 1",
            (name,))
        return self.apply_overlay("clubs", dict(row)) if row else None

    def clubs(self, query: str = "", limit: int = 200):
        q = fold_text(query)
        if not q:
            rows = self._fetchall(
                "SELECT id, unique_id, name, nation, competition, reputation, balance, "
                "transfer_budget, payroll_budget, training_facilities, "
                "youth_facilities, youth_coaching, youth_recruitment, "
                "corporate_facilities FROM clubs "
                "ORDER BY reputation DESC LIMIT ?", (int(limit),))
            return [self.apply_overlay("clubs", dict(r)) for r in rows]
        with self._lock:
            index = self._ensure_club_search_index()
            scores: dict[int, float] = {}
            for cid, fn, fd in index:
                if q in fn or (fd and q in fd):
                    scores[cid] = 1.15 if (fn == q or fd == q or fn.startswith(q)) else 1.0
            if not scores and len(q) >= 3:
                for cid, fn, fd in index:
                    sc = fuzzy_match_score(q, fn, fd)
                    if sc > 0.0:
                        scores[cid] = sc
            if not scores:
                return []
            if len(scores) <= 850:
                ph = ",".join("?" for _ in scores)
                rows = self._fetchall(
                    "SELECT id, unique_id, name, nation, competition, reputation, balance, "
                    "transfer_budget, payroll_budget, training_facilities, "
                    "youth_facilities, youth_coaching, youth_recruitment, "
                    f"corporate_facilities FROM clubs WHERE id IN ({ph}) "
                    "ORDER BY reputation DESC", tuple(scores.keys()))
            else:
                id_set = set(scores.keys())
                rows = [
                    r for r in self._cursor(
                        "SELECT id, unique_id, name, nation, competition, reputation, balance, "
                        "transfer_budget, payroll_budget, training_facilities, "
                        "youth_facilities, youth_coaching, youth_recruitment, "
                        "corporate_facilities FROM clubs ORDER BY reputation DESC")
                    if int(r["id"]) in id_set
                ]
            out = [self.apply_overlay("clubs", dict(r)) for r in rows]
            exact = [r for r in out if scores.get(int(r["id"]), 0.0) >= 1.0]
            fuzzy = [r for r in out if scores.get(int(r["id"]), 0.0) < 1.0]
            fuzzy.sort(key=lambda r: round(scores.get(int(r["id"]), 0.0), 3), reverse=True)
            return (exact + fuzzy)[:max(1, int(limit))]

    def staff_counts(self, club: str) -> dict[str, int]:
        out = {}
        try:
            for row in self._fetchall(
                    "SELECT job, COUNT(*) AS n FROM staff WHERE club=? GROUP BY job",
                    (club,)):
                out[(row["job"] or "").lower()] = row["n"]
        except sqlite3.Error:
            pass
        return out

    def staff(self, club: str, limit: int = 300):
        return self._fetchall(
            "SELECT * FROM staff WHERE club=? ORDER BY ca DESC LIMIT ?",
            (club, limit))

    def staff_member(self, key: int) -> dict[str, Any] | None:
        """One staff member by ``unique_id`` or row ``id``, overlay applied."""
        row = self._fetchone(
            "SELECT * FROM staff WHERE unique_id=? OR id=? ORDER BY "
            "CASE WHEN unique_id=? THEN 0 ELSE 1 END LIMIT 1",
            (int(key), int(key), int(key)))
        return self.apply_overlay("staff", dict(row)) if row else None

    def staff_jobs(self, limit: int = 30) -> list[str]:
        """The most common staff roles recorded in the snapshot."""
        try:
            rows = self._fetchall(
                "SELECT job, COUNT(*) AS n FROM staff "
                "WHERE job IS NOT NULL AND TRIM(job) <> '' "
                "GROUP BY job ORDER BY n DESC LIMIT ?", (int(limit),))
            return [str(r["job"]) for r in rows]
        except sqlite3.Error:
            return []

    def query_staff(self, *,
                    query: str = "",
                    club: str | None = None,
                    nationality: str | None = None,
                    job: str | None = None,
                    min_age: int | None = None,
                    max_age: int | None = None,
                    min_ca: int | None = None,
                    max_ca: int | None = None,
                    min_pa: int | None = None,
                    max_pa: int | None = None,
                    order: str = "ca",
                    descending: bool = True,
                    limit: int = 200) -> list[dict[str, Any]]:
        """Filterable staff search over the 23k+ staff table."""
        allowed = {"id", "unique_id", "name", "club", "job", "nationality",
                   "age", "ca", "pa"}
        if order not in allowed:
            raise ValueError(f"{order!r} is not a staff column that can be sorted on")
        clauses: list[str] = []
        params: list[Any] = []
        staff_scores: dict[int, float] | None = None
        if query and query.strip():
            q = fold_text(query)
            if q:
                with self._lock:
                    index = self._ensure_staff_search_index()
                    scores: dict[int, float] = {}
                    for sid, fn, fc, fj in index:
                        if q in fn or (fc and q in fc) or (fj and q in fj):
                            scores[sid] = 1.1 if q in fn else 1.0
                    if not scores and len(q) >= 3:
                        for sid, fn, fc, fj in index:
                            sc = fuzzy_match_score(q, fn, fc, fj)
                            if sc > 0.0:
                                scores[sid] = sc
                    if not scores:
                        return []
                    staff_scores = scores
        if club is not None and club.strip():
            clauses.append("club = ?")
            params.append(club.strip())
        if nationality is not None and nationality.strip():
            clauses.append("nationality = ?")
            params.append(nationality.strip())
        if job is not None and job.strip():
            clauses.append("job = ?")
            params.append(job.strip())
        if min_age is not None:
            clauses.append("age >= ?")
            params.append(int(min_age))
        if max_age is not None:
            clauses.append("age <= ?")
            params.append(int(max_age))
        if min_ca is not None:
            clauses.append("ca >= ?")
            params.append(int(min_ca))
        if max_ca is not None:
            clauses.append("ca <= ?")
            params.append(int(max_ca))
        if min_pa is not None:
            clauses.append("pa >= ?")
            params.append(int(min_pa))
        if max_pa is not None:
            clauses.append("pa <= ?")
            params.append(int(max_pa))
        id_set: set[int] | None = None
        if staff_scores is not None:
            if len(staff_scores) <= 850:
                ph = ",".join("?" for _ in staff_scores)
                clauses.append(f"id IN ({ph})")
                params.extend(staff_scores.keys())
            else:
                id_set = set(staff_scores.keys())
        sql = "SELECT * FROM staff"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f' ORDER BY "{order}" {"DESC" if descending else "ASC"}, ca DESC'
        cap = max(1, int(limit))
        if id_set is not None:
            with self._lock:
                out: list[dict[str, Any]] = []
                for r in self._cursor(sql, tuple(params)):
                    if int(r["id"]) in id_set:
                        out.append(self.apply_overlay("staff", dict(r)))
                        if len(out) >= cap:
                            break
                return out
        has_fuzzy = bool(staff_scores and any(v < 1.0 for v in staff_scores.values()))
        if not has_fuzzy:
            sql += " LIMIT ?"
            params.append(cap)
        rows = self._fetchall(sql, tuple(params))
        out = [self.apply_overlay("staff", dict(r)) for r in rows]
        if has_fuzzy and staff_scores is not None:
            exact = [r for r in out if staff_scores.get(int(r["id"]), 0.0) >= 1.0]
            fuzzy = [r for r in out if staff_scores.get(int(r["id"]), 0.0) < 1.0]
            fuzzy.sort(key=lambda r: round(staff_scores.get(int(r["id"]), 0.0), 3), reverse=True)
            return (exact + fuzzy)[:cap]
        return out[:cap]

    def nations(self, query: str = "", limit: int = 250) -> list[dict[str, Any]]:
        """Nations present in the snapshot with player, club and staff counts."""
        text = (query or "").strip().lower()
        by_name: dict[str, dict[str, Any]] = {}
        for row in self._fetchall(
                "SELECT nationality AS name, COUNT(*) AS n, MAX(ca) AS best_ca "
                "FROM players WHERE nationality IS NOT NULL AND TRIM(nationality) <> '' "
                "GROUP BY nationality"):
            name = str(row["name"]).strip()
            entry = by_name.setdefault(name, {
                "name": name, "players": 0, "clubs": 0, "staff": 0,
                "best_ca": 0,
            })
            entry["players"] = int(row["n"] or 0)
            entry["best_ca"] = max(entry["best_ca"], int(row["best_ca"] or 0))
        for row in self._fetchall(
                "SELECT nation AS name, COUNT(*) AS n FROM clubs "
                "WHERE nation IS NOT NULL AND TRIM(nation) <> '' GROUP BY nation"):
            name = str(row["name"]).strip()
            entry = by_name.setdefault(name, {
                "name": name, "players": 0, "clubs": 0, "staff": 0,
                "best_ca": 0,
            })
            entry["clubs"] = int(row["n"] or 0)
        for row in self._fetchall(
                "SELECT nationality AS name, COUNT(*) AS n FROM staff "
                "WHERE nationality IS NOT NULL AND TRIM(nationality) <> '' "
                "GROUP BY nationality"):
            name = str(row["name"]).strip()
            entry = by_name.setdefault(name, {
                "name": name, "players": 0, "clubs": 0, "staff": 0,
                "best_ca": 0,
            })
            entry["staff"] = int(row["n"] or 0)
        rows = list(by_name.values())
        if text:
            rows = [r for r in rows if text in r["name"].lower()]
        rows.sort(key=lambda r: (-r["players"], -r["clubs"], r["name"]))
        return rows[:max(1, int(limit))]

    def nation_summary(self, name: str) -> dict[str, Any] | None:
        """Full nation / national-team view assembled from the snapshot."""
        wanted = (name or "").strip()
        if not wanted:
            return None
        matches = self.nations(query=wanted, limit=50)
        chosen = next((m for m in matches if m["name"].lower() == wanted.lower()), None)
        if chosen is None and matches:
            chosen = matches[0]
        if chosen is None:
            return None
        canonical = chosen["name"]
        players = self.query_players(
            nationality=canonical, order="ca", descending=True,
            limit=80, columns=SQUAD_COLUMNS)
        clubs = [
            self.apply_overlay("clubs", dict(r))
            for r in self._fetchall(
                "SELECT id, unique_id, name, nation, competition, reputation, "
                "balance, transfer_budget, training_facilities, youth_facilities "
                "FROM clubs WHERE nation=? ORDER BY reputation DESC LIMIT 60",
                (canonical,))
        ]
        staff = self.query_staff(nationality=canonical, limit=60)
        competitions = [
            dict(r) for r in self._fetchall(
                "SELECT id, name, nation, reputation, gender FROM competitions "
                "WHERE nation=? ORDER BY reputation DESC LIMIT 40",
                (canonical,))
        ]
        return {
            "name": canonical,
            "counts": {
                "players": chosen["players"],
                "clubs": chosen["clubs"],
                "staff": chosen["staff"],
                "competitions": len(competitions),
            },
            "best_ca": chosen["best_ca"],
            "players": players,
            "clubs": clubs,
            "staff": staff,
            "competitions": competitions,
        }

    def future_transfers(self, *, query: str = "", club: str | None = None,
                         limit: int = 400) -> list[dict[str, Any]]:
        """Players with an agreed future transfer, with overlays applied."""
        clauses = [
            "(transfer_agreed = 1 OR (future_transfer_club IS NOT NULL "
            "AND TRIM(future_transfer_club) <> ''))"
        ]
        params: list[Any] = []
        if query and query.strip():
            text = f"%{query.strip()}%"
            clauses.append("(name LIKE ? OR club LIKE ? OR future_transfer_club LIKE ?)")
            params.extend([text, text, text])
        if club and club.strip():
            clauses.append("(club = ? OR future_transfer_club = ?)")
            params.extend([club.strip(), club.strip()])
        sql = (
            "SELECT * FROM players WHERE " + " AND ".join(clauses)
            + " ORDER BY future_transfer_date ASC, ca DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        with self._lock:
            rows = [self.apply_overlay("players", dict(r))
                    for r in self._fetchall(sql, tuple(params))]
        return rows

    def transfer_market(self, *, kind: str = "listed", query: str = "",
                        club: str | None = None,
                        limit: int = 400) -> list[dict[str, Any]]:
        """Players listed for transfer/loan or with expiring contracts."""
        kind_low = (kind or "listed").strip().lower()
        if kind_low == "listed":
            return self.query_players(
                search=query or None, club=club or None, transfer_listed=True,
                order="ca", descending=True, limit=limit, columns=SQUAD_COLUMNS)
        clauses: list[str] = []
        params: list[Any] = []
        if kind_low == "loan":
            clauses.append("listed_for_loan = 1")
        elif kind_low == "expiring":
            game_date = (self.meta().get("game_date") or FALLBACK_GAME_DATE)[:10]
            try:
                year = int(game_date[:4]) + 1
                cutoff = f"{year}{game_date[4:]}"
            except ValueError:
                cutoff = "2027-07-20"
            clauses.append(
                "(contract_end_date IS NOT NULL AND TRIM(contract_end_date) <> '' "
                "AND contract_end_date <= ?)"
            )
            params.append(cutoff)
        else:
            clauses.append("(transfer_listed = 1 OR listed_for_loan = 1)")
        if query and query.strip():
            text = f"%{query.strip()}%"
            clauses.append("(name LIKE ? OR club LIKE ?)")
            params.extend([text, text])
        if club and club.strip():
            clauses.append("club = ?")
            params.append(club.strip())
        cols = ", ".join(SQUAD_COLUMNS)
        sql = (
            f"SELECT {cols} FROM players WHERE " + " AND ".join(clauses)
            + " ORDER BY ca DESC LIMIT ?"
        )
        params.append(max(1, int(limit)))
        with self._lock:
            rows = [self.apply_overlay("players", dict(r))
                    for r in self._fetchall(sql, tuple(params))]
        return rows

    def retirements(self, *, min_age: int = 35, query: str = "",
                    include_staff: bool = False,
                    limit: int = 400) -> list[dict[str, Any]]:
        """Players or staff approaching retirement (sorted oldest / highest CA)."""
        if include_staff:
            return self.query_staff(
                query=query, min_age=int(min_age),
                order="age", descending=True, limit=limit)
        return self.query_players(
            search=query or None, min_age=int(min_age),
            order="age", descending=True, limit=limit, columns=SQUAD_COLUMNS)

    def read_current_game_screen(self, *, rva: int | None = None
                                 ) -> tuple[Any, str]:
        """Read FM27's currently open screen entity through the attached editor.

        Returns ``(CurrentScreen | None, reason)``.  Never raises on read
        failure so the toolbar button can report a plain status message.
        """
        with self._lock:
            editor = self.editor
        if editor is None:
            return None, (
                "No running game is attached. Press Load Game to attach to "
                "FM27 first.")
        reader = getattr(editor, "read_current_screen", None)
        if not callable(reader):
            return None, "The attached live reader does not support Current Screen."
        try:
            screen = reader(rva=rva)
        except Exception as exc:  # noqa: BLE001 - never leak ctypes
            return None, f"Could not read FM27's current screen: {exc}"
        if screen is not None:
            return screen, ""
        build = getattr(editor, "build", None) or self.meta().get("fm_build") or "unknown"
        return None, (
            f"FM27 is attached (build {build}), but no current-screen entity "
            "was resolved. Set FMRTE_CURRENT_SCREEN_RVA or run "
            "tools/probe_current_screen.py while FM27 is open to calibrate "
            "this build.")

    def next_fixture(self, club: str, after: str | None = None) -> dict[str, Any] | None:
        after = after or self.meta().get("game_date") or "1970-01-01"
        row = self._fetchone(
            "SELECT * FROM fixtures WHERE (home_club=? OR away_club=?) "
            "AND kickoff_date>=? ORDER BY kickoff_date, kickoff_time LIMIT 1",
            (club, club, after))
        return dict(row) if row else None

    def fixtures(self, club: str, limit: int = 20):
        after = self.meta().get("game_date") or "1970-01-01"
        return self._fetchall(
            "SELECT * FROM fixtures WHERE (home_club=? OR away_club=?) "
            "AND kickoff_date>=? ORDER BY kickoff_date, kickoff_time LIMIT ?",
            (club, club, after, limit))

    def leaderboard(self, limit: int = 20):
        return self._fetchall(
            "SELECT id, unique_id, name, club, ca, pa, age, nationality, "
            "asking_price FROM players ORDER BY ca DESC LIMIT ?", (limit,))

    # -- the live session -------------------------------------------------
    @property
    def attached(self) -> bool:
        """True when a running game is attached, so edits go into it."""
        return self.editor is not None

    @property
    def read_only(self) -> bool:
        """True when the attach cannot write (Windows refused the write handle).

        False when nothing is attached at all: there is no read-only *session*
        then, the edits are simply staged.  This is the ``getattr(editor,
        "read_only", False)`` that the Load Game dialog used to spell out.
        """
        if self.editor is None:
            return False
        return bool(getattr(self.editor, "read_only", False))

    def attach_live(self, editor, records=None, report=None,
                    club_finances: Mapping[str, Mapping[str, Any]] | None = None) -> int:
        """Own the live session: store the editor and apply what was read.

        ``records`` is what the caller read out of the game - an iterable of
        ``LivePlayer`` records or a ``{unique_id: record}`` mapping.  The
        reading itself happens on the dialog's worker thread (a 42,000-player
        scan belongs there), so the data layer takes the result instead of
        starting its own scan on the window's thread.  ``None`` means "no new
        records": the overlay that is already there is kept and nothing is
        applied.

        ``report`` is the :class:`fmrte.fmmemory.LiveReport`, when the caller
        has one; the game date and the managed club are taken from it, falling
        back to the editor's own ``live_date`` / ``managed_club``.  They are
        what makes the assistant and the status bar talk about the *running*
        save rather than the snapshot.

        The staged edits are then written into the game (unless the attach is
        read-only, where they stay staged); the counts are left in
        :attr:`last_flush` as ``(written, failed)``, because the window prints
        them.

        Returns how many players were applied.  Attaching while a game is
        already attached closes the old handle first: the editor owns a Win32
        process handle, and one leaked handle per "Load Game" is a real resource
        leak, not a theoretical one.
        """
        with self._lock:
            previous = self.editor
            self.editor = editor
            if previous is not None and previous is not editor:
                self._close_editor(previous)
            self.live_meta = dict(self._live_meta(editor, report))
            self.last_flush = (0, 0)
            count = 0
            if records is not None:
                # A fresh attach is a fresh reading of the game: stale live
                # values for a player the game no longer reports must not
                # survive it.
                self.live.clear()
                self._live_has_clubs = False
                if club_finances is not None:
                    self.club_live.clear()
                count = self.apply_live(records, club_finances=club_finances)
            elif club_finances is not None:
                self.club_live.clear()
                self.apply_live((), club_finances=club_finances)
            if not self.read_only:
                self.flush_pending()
        return count

    def detach_live(self) -> None:
        """Forget the game: no editor, no live overlay, no live metadata.

        The snapshot rows are untouched - they are the database - and the
        staged edits and the journal stay, because they are the person's own
        work rather than the process's.  The editor's process handle is closed:
        forgetting the editor without closing it leaked one handle per detach.
        """
        with self._lock:
            editor = self.editor
            self.editor = None
            self.live.clear()
            self.club_live.clear()
            self._live_has_clubs = False
            self.live_meta.clear()
            self._player_search_index = None
            # Inside the lock: a write that is still running on another thread
            # must not have the handle pulled out from under it.
            self._close_editor(editor)

    @staticmethod
    def _live_meta(editor, report=None) -> dict[str, str]:
        """The live game date / managed club, from whichever source has them."""
        date = (getattr(report, "game_date", "")
                or getattr(editor, "live_date", "") or "")
        club = (getattr(report, "managed_club", "")
                or getattr(editor, "managed_club", "") or "")
        meta: dict[str, str] = {}
        if date:
            meta["game_date"] = str(date)
        if club:
            meta["managed_club"] = str(club)
        return meta

    def reload(self, path: str | None = None, keep_live: bool = True) -> "Snapshot":
        """Re-read the snapshot file in place, keeping the live session.

        The connection is reopened on the same object (``path`` chooses another
        file), so the window keeps one data layer instead of replacing it.
        With ``keep_live`` - the default - the attached editor, the live
        overlay and the live metadata are carried across, which is the point:
        replacing the object used to drop them, and every field the game had
        supplied fell back to the database's copy.

        Staged edits and the journal are *not* carried across: they belong to
        the reading that was just discarded.  ``keep_live=False`` drops the
        live session as well - the same thing as :meth:`detach_live`, plus a
        fresh reading - and closes the editor it drops.

        The new connection is opened and checked *before* the old one is
        closed.  It used to be the other way round, so a bad path or a file
        that is not a snapshot left this object permanently closed; now a
        failed reload raises and the old connection is still exactly what it
        was (``self.conn``, ``self.path``, the overlay and the journal).
        """
        target = path or self.path
        if not os.path.exists(target):
            raise FileNotFoundError(target)
        with self._lock:
            old_conn = self.conn
            old_path = self.path
            new_conn = None
            try:
                new_conn = sqlite3.connect(target, check_same_thread=False)
                new_conn.row_factory = sqlite3.Row
                # A file that is not a snapshot connects lazily and only fails
                # on use, so prove the reading works before swapping it in.
                new_conn.execute("SELECT COUNT(*) FROM players").fetchone()
            except sqlite3.Error:
                # Close the failed handle immediately so Windows does not keep
                # the file locked (which used to leak fmrte-reload-* folders).
                if new_conn is not None:
                    try:
                        new_conn.close()
                    except sqlite3.Error:
                        pass
                raise
            editor = self.editor
            live = dict(self.live)
            club_live = dict(self.club_live)
            live_meta = dict(self.live_meta)
            self.path = target
            self.conn = new_conn
            self.edits.clear()
            self.journal.clear()
            self.redo_stack.clear()
            self.saved_edits.clear()
            self.last_edit = "local"
            self.last_error = ""
            self.last_value = None
            self.last_flush = (0, 0)
            self.legacy_edits = 0
            self._player_search_index = None
            self._staff_search_index = None
            self._club_search_index = None
            if keep_live and editor is not None:
                self.editor = editor
                self.live.clear()
                self.live.update(live)
                self.club_live.clear()
                self.club_live.update(club_live)
                self.live_meta.clear()
                self.live_meta.update(live_meta)
            else:
                self.editor = None
                self.live.clear()
                self.club_live.clear()
                self.live_meta.clear()
                self._close_editor(editor)
            self._load_edits()
            try:
                old_conn.close()
            except sqlite3.Error:
                pass
        return self

    def close(self) -> None:
        """Close the SQLite connection; the live session is left alone.

        Safe to call twice, and from either thread: the lock serialises it
        against a read that is still running, and a connection that is already
        closed is not an error.
        """
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # -- edits ------------------------------------------------------------
    def _key(self, table: str, row_id: int, column: str) -> str:
        return f"{table}:{row_id}:{column}"

    def _base(self, table: str, row_id: int, column: str):
        try:
            row = self._fetchone(
                f"SELECT {column} FROM {table} WHERE id=?", (row_id,))
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def apply_overlay(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """Merge the live values and the local edits into one row.

        The live overlay wins over the database (it is what the running game
        holds) and a staged edit wins over both (it is what the person asked
        for).  Mutates and returns ``row``; call it with a copy.  Takes the
        connection's lock, so a caller outside a read method sees one complete
        overlay rather than half of one being updated on another thread.
        """
        with self._lock:
            if table == "players":
                live = self.live.get(int(row.get("unique_id") or 0))
                if live:
                    row.update(live)
            elif table == "clubs" and self.club_live:
                c_key = str(row.get("name") or "").strip().lower()
                clive = self.club_live.get(c_key) if c_key else None
                if clive:
                    row.update(clive)
            if not self.edits:
                return row
            prefix = f"{table}:{row.get('id')}:"
            for key, value in self.edits.items():
                if key.startswith(prefix):
                    column = key[len(prefix):]
                    if column in row:
                        row[column] = value
            return row

    def apply_live(self, records, *,
                   club_finances: Mapping[str, Mapping[str, Any]] | None = None) -> int:
        """Merge values read from the running game; never marks an edit."""
        if isinstance(records, Mapping):
            records = records.values()
        count = 0
        club_changed = False
        _optional_fields = (
            "asking_price", "salary_weekly_raw", "salary_pa",
            "contract_end_date", "joined_club_date",
            "transfer_listed", "listed_for_loan",
            "club", "playing_club", "loan_club", "is_loaned_out",
            "injured", "injury", "injury_start_date",
            "injury_light_training_days_remaining",
            "injury_full_training_days_remaining",
            "injury_expected_return",
        )
        with self._lock:
            for record in records:
                entry: dict[str, Any] = {
                    "ca": record.ca,
                    "pa": record.pa,
                    "condition": record.condition,
                    "match_sharpness": record.sharpness,
                    "jadedness": record.jadedness,
                    "morale": record.morale,
                    "home_reputation": record.home_reputation,
                    "current_reputation": record.current_reputation,
                    "world_reputation": record.world_reputation,
                }
                live_name = (getattr(record, "display_name", "") or "").strip()
                if live_name:
                    entry["display_name"] = live_name
                for fname in _optional_fields:
                    val = getattr(record, fname, None)
                    if val is not None:
                        entry[fname] = val
                        if fname == "club":
                            club_changed = True
                if "injury_light_training_days_remaining" in entry:
                    entry["injury_min_days_remaining"] = entry["injury_light_training_days_remaining"]
                if "injury_full_training_days_remaining" in entry:
                    entry["injury_max_days_remaining"] = entry["injury_full_training_days_remaining"]
                self.live[int(record.unique_id)] = entry
                count += 1
            if club_finances:
                for cname, fvals in club_finances.items():
                    key = str(cname or "").strip().lower()
                    if key and isinstance(fvals, Mapping):
                        self.club_live[key] = dict(fvals)
            if club_changed:
                self._live_has_clubs = True
                self._player_search_index = None
        return count

    def flush_pending(self) -> tuple[int, int]:
        """Write the edits made while no game was attached into the game.

        Returns (written, failed). Every edit that went to a column this build
        knows how to write is applied now; the rest stay on screen as staged
        values because they have no address to write to.  A staged contract
        date goes through the editor's date writer, not the numeric one.

        An edit that lands is also marked as written in the journal, so a later
        Undo restores it *in the game* rather than leaving a local overlay
        behind that the game knows nothing about.
        """
        with self._lock:
            if self.editor is None:
                self.last_flush = (0, 0)
                return 0, 0
            written = failed = 0
            for key in list(self.edits):
                parts = key.split(":", 2)
                if len(parts) != 3 or parts[0] != "players":
                    continue
                row = self._fetchone(
                    "SELECT unique_id FROM players WHERE id=?", (parts[1],))
                if row is None:
                    continue
                uid = int(row[0])
                column = parts[2]
                staged = self.edits[key]
                try:
                    if column in DATE_COLUMNS:
                        when = _as_date(staged)
                        if when is None:
                            raise ValueError("not a date")
                        self.editor.write_date(
                            uid, column, when, self._column_label(column))
                        applied = when.isoformat()
                    else:
                        _record, applied = self.editor.write(
                            uid, column, int(staged),
                            self._column_label(column))
                except Exception:  # noqa: BLE001 - a failed write must not stop the rest
                    failed += 1
                    continue
                written += 1
                self.edits.pop(key, None)
                self.live.setdefault(uid, {})[column] = applied
                self._mark_written(key, applied)
            self.saved_edits = dict(self.edits)
            self.last_flush = (written, failed)
            return written, failed

    def _mark_written(self, key: str, applied) -> None:
        """Record that a staged edit reached the game, for later undo.

        Only the newest journal entry for ``key`` describes the value that was
        written: a flush sends the *net* staged value, so with two offline
        edits to one field (16 -> 18 -> 20) it is the second entry's ``new``
        that landed.  This used to rewrite ``new`` on every entry with that
        key, which turned both entries into "-> 20" and made a later undo/redo
        jump over the 18.

        The older entries in that chain are marked ``superseded`` instead of
        ``in_game``: they were not written by the editor themselves, but the
        game is holding their value as part of the chain, so a replay has to
        move the game too (``_replay`` treats both flags the same way).
        """
        matches = [change
                   for action in self.journal
                   for change in action.changes
                   if change.key == key]
        for change in matches[:-1]:
            if not change.in_game:
                change.superseded = True
        if matches:
            last = matches[-1]
            last.in_game = True
            last.new = applied

    def live_count(self) -> int:
        with self._lock:
            return len(self.live)

    def unique_ids(self) -> list[int]:
        return [int(row[0]) for row in self._fetchall(
            "SELECT unique_id FROM players WHERE unique_id IS NOT NULL")]

    def raw_value(self, table: str, row: dict[str, Any], column: str):
        with self._lock:
            key = self._key(table, row["id"], column)
            if key in self.edits:
                return self.edits[key]
        return row.get(column)

    def set_value(self, table: str, row: dict[str, Any], column: str, value):
        """Change one field, into the game when attached, else into the overlay.

        One call is one journal entry, so undo takes exactly this change back.
        The value the data layer stored is left in :attr:`last_value`, because
        the clamp and the game's own read-back can both differ from what the
        caller passed.
        """
        with self._lock:
            status, change = self._attempt(table, row, column, value)
            if status != "changed":
                return False
            self._push(EditAction(label=column, changes=[change],
                                  uid=change.uid))
            return True

    def set_date(self, unique_id: int, column: str,
                 iso_date: str) -> tuple[bool, str]:
        """Change one contract date, journaled and undoable like any edit.

        ``playersections._commit_date`` used to reach past the data layer into
        ``db.editor.write_date``.  That had two consequences: the write never
        entered :attr:`journal`, so Ctrl+Z could not take it back, and the live
        overlay never learned the new date, so the page redrew the snapshot's
        old one the moment it was rebuilt.

        Returns ``(stored, message)``.  ``stored`` is False when the date was
        rejected (and ``message`` says why, in plain words); True when it is in
        the game, or staged in the overlay when no game is attached.  A date
        that is already the one on screen is a success with a message that
        says nothing needed to change.
        """
        if column not in DATE_COLUMNS:
            return False, (f"{self._column_label(column)} is not a date this "
                           "build can change.")
        when = _as_date(iso_date)
        if when is None:
            return False, f"{iso_date!r} is not a date this build understands."
        iso = when.isoformat()
        uid = int(unique_id)
        with self._lock:
            row = self._fetchone(
                f"SELECT id, {column} FROM players WHERE unique_id=?", (uid,))
            if row is None:
                return False, f"That player ({uid}) is not in this snapshot."
            row_id = int(row["id"])
            key = self._key("players", row_id, column)
            old = row[column]
            overlay = self.live.get(uid, {})
            if column in overlay:
                old = overlay[column]
            if key in self.edits:
                old = self.edits[key]
            self.last_value = None
            if old == iso:
                return True, f"{self._column_label(column)} is already {iso}."
            change = EditChange(key=key, table="players", row_id=row_id,
                                column=column, old=old, new=iso, uid=uid)
            self.last_edit = "local"
            self.last_error = ""
            if self.editor is not None:
                try:
                    self.editor.write_date(uid, column, when,
                                           self._column_label(column))
                except Exception as exc:  # noqa: BLE001 - never leak ctypes
                    self.last_edit = "error"
                    self.last_error = str(exc) or (
                        f"{self._column_label(column)} could not be written.")
                    return False, self.last_error
                change.in_game = True
                self.live.setdefault(uid, {})[column] = iso
                self.last_edit = "game"
                message = (f"{self._column_label(column)} written to the game: "
                           f"{iso}.")
            else:
                self.edits[key] = iso
                self.last_edit = "waiting"
                message = (f"{self._column_label(column)} staged as {iso}. "
                           "Press Load Game to write it into the game.")
            self.last_value = iso
            self._push(EditAction(label=column, changes=[change], uid=uid))
            return True, message

    def _attempt(self, table: str, row: dict[str, Any], column: str,
                 value) -> tuple[str, EditChange | None]:
        """Apply one field and describe it, without touching the journal.

        Returns ``("changed", change)``, ``("same", None)`` when the value is
        already the one on screen, or ``("error", None)`` when the game refused
        it (``last_error`` then holds the reason in words).  ``set_value`` and
        ``heal_player`` share this, which is how a four-field heal still ends
        up as one undo entry.
        """
        key = self._key(table, row["id"], column)
        old = self.raw_value(table, row, column)
        self.last_value = None
        # A value that is on its way into the game is *not* clamped with the
        # snapshot's table first: the live writer owns the game's own domain
        # (morale is a 0..100 byte there, a position is 1..20) and returns what
        # the game read back.  Clamping here first is what turned a live morale
        # of 80 into 20 on an edit or an undo.  The local overlay keeps the
        # snapshot's domain, so the clamp applies to that path only.
        anchored = table == "players" and self.editor is not None
        if (table == "players" and not anchored
                and isinstance(value, int) and not isinstance(value, bool)):
            # The original clamps every editable player value in its own code.
            # The ranges are declared once, in spec.py (see MECHANICS.md
            # section 9.5), so an edit made here and an edit made on the live
            # side cannot disagree about what is legal.
            value = spec.clamp(column, value)
        if old == value:
            return "same", None

        uid = None
        if table == "players" and row.get("unique_id") is not None:
            uid = int(row["unique_id"])
        change = EditChange(key=key, table=table, row_id=int(row["id"]),
                            column=column, old=old, new=value, uid=uid)

        # With the game attached, the edit goes straight into the game.
        self.last_edit = "local"
        self.last_error = ""
        if table == "players" and self.editor is not None:
            from .livewrite import EditError

            before = self.live.get(uid, {}).get(column, old)
            try:
                _record, applied = self.editor.write(
                    uid, column, int(value), self._column_label(column))
            except EditError as exc:
                self.last_edit = "error"
                self.last_error = str(exc)
                return "error", None
            except Exception as exc:  # noqa: BLE001 - never leak ctypes
                self.last_edit = "error"
                self.last_error = str(exc)
                return "error", None
            if applied == before:
                self.live.setdefault(uid, {})[column] = applied
                row[column] = applied
                self.last_value = applied
                return "same", None
            change.old = before
            change.new = applied
            change.in_game = True
            self.live.setdefault(uid, {})[column] = applied
            row[column] = applied
            self.last_value = applied
            self.last_edit = "game"
            return "changed", change

        self.edits[key] = value
        row[column] = value
        self.last_value = value
        if table == "players":
            self.last_edit = "waiting"
        return "changed", change

    def _push(self, action: EditAction) -> None:
        """Record one user action; a new action makes the redo stack stale."""
        self.journal.append(action)
        self.redo_stack.clear()

    def heal_player(self, row: dict[str, Any]) -> tuple[bool, str]:
        """Apply the original's Heal preset to one player, as ONE action.

        The preset is four actions (``Presets/Heal.json``): Condition 10000,
        MatchSharpness 10000, Jadedness 0 and ``IsInjured = false``. The first
        three are ordinary fields; the fourth is the injury reference, which is
        why writing only the numbers left the player injured.

        All four go into a single journal entry, so one Undo puts back exactly
        what this one press changed - it used to push three entries to the
        column history and one to the editor's byte stack, and Undo then popped
        mismatched pairs.

        Returns (healed, message). When no game is attached the numbers are
        staged and the injury is reported as not yet cleared, so the person is
        never told something happened that did not.
        """
        with self._lock:
            return self._heal_player(row)

    def _heal_player(self, row: dict[str, Any]) -> tuple[bool, str]:
        """The body of :meth:`heal_player`, with the lock already held."""
        from .livewrite import EditError

        uid = int(row.get("unique_id") or 0)
        action = EditAction(label="heal", uid=uid)
        failure = ""
        for column, value in (("condition", 10000), ("match_sharpness", 10000),
                              ("jadedness", 0)):
            status, change = self._attempt("players", row, column, value)
            if change is not None:
                action.changes.append(change)
            if status == "error" and not failure:
                failure = self.last_error
        if failure:
            # Whatever did land stays journaled as its own action, so Undo is
            # never offered for something that never happened.
            if action.changes:
                self._push(action)
            return False, failure
        if self.editor is None:
            if action.changes:
                self._push(action)
            return False, ("Condition, sharpness and jadedness are staged. "
                           "Press Load Game to clear the injury in the game.")

        before_live = {column: self.live.get(uid, {}).get(column)
                       for column in INJURY_LIVE_COLUMNS}
        try:
            cleared = self.editor.heal(uid)
        except EditError as exc:
            self.last_edit = "error"
            self.last_error = str(exc)
            if action.changes:
                self._push(action)
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - never leak ctypes
            self.last_edit = "error"
            self.last_error = str(exc)
            if action.changes:
                self._push(action)
            return False, str(exc)

        before_injury = row.get("injury") or "Injured"
        cleared_live = {
            "injured": 0, "injury": "", "injury_start_date": "",
            "injury_light_training_days_remaining": "",
            "injury_full_training_days_remaining": "",
            "injury_min_days_remaining": "",
            "injury_max_days_remaining": "",
            "injury_expected_return": "",
        }
        self.live.setdefault(uid, {}).update(cleared_live)
        row.update(cleared_live)
        if getattr(cleared, "injured", False):
            action.changes.append(EditChange(
                key=self._key("players", int(row.get("id") or 0), INJURY_COLUMN),
                table="players", row_id=int(row.get("id") or 0),
                column=INJURY_COLUMN,
                old=cleared.name or before_injury, new="Fit", uid=uid,
                in_game=True,
                injury_reference=int(getattr(cleared, "reference", 0) or 0),
                live_before=before_live, live_after=dict(cleared_live)))
        if action.changes:
            self._push(action)
        self.last_edit = "game"
        self.last_error = ""
        name = cleared.name or "injury"
        return True, (f"Healed in the game: {name} cleared, condition and "
                      f"sharpness 10000, jadedness 0.")

    def refresh_injury(self, row: dict[str, Any]) -> None:
        """Read the player's injury from the game into the row, when attached."""
        with self._lock:
            if self.editor is None:
                return
            uid = int(row.get("unique_id") or 0)
            values = self._injury_overlay(uid)
            if values is None:
                return
            self.live.setdefault(uid, {}).update(values)
            row.update(values)

    def refresh_display_name(self, row: dict[str, Any]) -> str:
        """Read FM27's live in-game display name into the overlay, when attached."""
        with self._lock:
            editor = self.editor
        if editor is None:
            return ""
        uid = int(row.get("unique_id") or 0)
        if not uid:
            return ""
        reader = getattr(editor, "read_person_display_name", None)
        if not callable(reader):
            return ""
        try:
            name = (reader(uid) or "").strip()
        except Exception:  # noqa: BLE001 - never break page open
            return ""
        if not name:
            return ""
        with self._lock:
            self.live.setdefault(uid, {})["display_name"] = name
        row["display_name"] = name
        return name

    @staticmethod
    def _injury_values(injury) -> dict[str, Any]:
        """One ``LiveInjury`` as the overlay/row fields the screen shows.

        The decoded record carries no expected return - the game keeps that
        elsewhere - so that one field is left alone here and restored from the
        journal when an action is replayed.
        """
        return {
            "injured": 1 if injury.injured else 0,
            "injury": injury.name,
            "injury_start_date": injury.start_date,
            "injury_light_training_days_remaining": (
                injury.light_training_days if injury.injured else ""),
            "injury_full_training_days_remaining": (
                injury.full_training_days if injury.injured else ""),
            "injury_min_days_remaining": (
                injury.light_training_days if injury.injured else ""),
            "injury_max_days_remaining": (
                injury.full_training_days if injury.injured else ""),
        }

    def _injury_overlay(self, uid: int) -> dict[str, Any] | None:
        """The game's injury for one player as overlay values, or None."""
        if self.editor is None:
            return None
        try:
            injury = self.editor.read_injury(uid)
        except Exception:  # noqa: BLE001 - a read must never break the page
            return None
        return None if injury is None else self._injury_values(injury)

    @staticmethod
    def _column_label(column: str) -> str:
        return column.replace("_", " ").title()

    # -- the edit journal -------------------------------------------------
    @property
    def history(self) -> list[tuple[str, Any, Any]]:
        """The per-field view of the journal, for the Edit History page.

        One ``(key, old, new)`` row per field, oldest first, so a heal shows
        the four fields it touched while the journal - and the Undo button -
        still treats it as one action.  Read-only: add to the journal, not to
        this; it is rebuilt from the journal on every read, so the two can
        never disagree.
        """
        with self._lock:
            return [(change.key, change.old, change.new)
                    for action in self.journal
                    for change in action.changes]

    def undo(self):
        """Take the last action back.

        Returns ``(key, old, new)`` for the field a plain edit touched, or
        ``(key, olds, news)`` for an action that touched several (``heal``),
        with ``key`` reading ``table:row:label`` then - or ``None`` when there
        is nothing to undo.  ``App.undo`` prints ``key.split(":")[-1]``.

        With a game attached, every field of the action that went into the game
        is put back *in the game* through the editor's own write path (identity
        re-check, clamp, read-back), and a cleared injury gets its reference
        back.  Fields that live in the local overlay are put back there.

        A replay the game refused leaves **both stacks unchanged**: popping the
        action anyway recorded a failure as if it had succeeded, and the next
        Undo then acted on the wrong entry.  ``last_error`` carries the reason
        and ``last_edit`` reads ``"error"``, which is what ``App.undo`` shows.
        """
        with self._lock:
            if not self.journal:
                return None
            action = self.journal[-1]
            problems = self._replay(action, forward=False)
            self._settle(action, problems)
            # When an attached game refused the write, neither the game nor the
            # screen changed, so keep the action on the journal for retry.  When
            # no game is attached, ``_replay`` already reverted the screen
            # overlay, so advance the stacks instead of wedging Undo on one row.
            if problems and self.editor is not None:
                return self._result(action)
            self.journal.pop()
            self.redo_stack.append(action)
            return self._result(action)

    def redo(self):
        """Apply the last undone action again, into the game when attached.

        This is the half that was missing: ``redo`` used to re-apply to the
        local overlay only, so Ctrl+Y after a live edit looked right on screen
        while the game kept the new value.  As with :meth:`undo`, a refused
        replay leaves both stacks alone.
        """
        with self._lock:
            if not self.redo_stack:
                return None
            action = self.redo_stack[-1]
            problems = self._replay(action, forward=True)
            self._settle(action, problems)
            if problems and self.editor is not None:
                return self._result(action)
            self.redo_stack.pop()
            self.journal.append(action)
            return self._result(action)

    @staticmethod
    def _written(change: EditChange) -> bool:
        """True when replaying this change has to go through the game.

        ``in_game`` is the normal case (the editor wrote this value).
        ``superseded`` is the older half of a flushed edit chain: the editor
        wrote the chain's net value, so this field is in the game too and a
        replay that only touched the overlay would leave the two disagreeing.
        """
        return (change.uid is not None
                and (change.in_game or change.superseded))

    def _write_back(self, change: EditChange, value) -> str:
        """Put one value into the game; return "" or the reason it failed."""
        try:
            if change.column in DATE_COLUMNS:
                when = _as_date(value)
                if when is None:
                    return (f"{value!r} is not a date this build can write.")
                self.editor.write_date(change.uid, change.column, when,
                                       self._column_label(change.column))
                applied = when.isoformat()
            else:
                _record, applied = self.editor.write(
                    change.uid, change.column, int(value),
                    self._column_label(change.column))
        except Exception as exc:  # noqa: BLE001 - never leak ctypes
            return str(exc) or f"{change.column} could not be written."
        self.live.setdefault(change.uid, {})[change.column] = applied
        self.edits.pop(change.key, None)
        return ""

    def _restore_overlay(self, change: EditChange, value) -> None:
        """Put one value back in the local overlay, or take the edit out."""
        if value is None or value == self._base(change.table, change.row_id,
                                                change.column):
            # Putting the database's own value back is not an edit.  The live
            # overlay is cleared as well: it used to keep the value that was
            # just undone, so the screen stayed on the new number even though
            # the row underneath was already the database's again.
            self.edits.pop(change.key, None)
            if change.table != "players" or change.uid is None:
                return
            overlay = self.live.get(change.uid)
            if overlay is None:
                return
            overlay.pop(change.column, None)
            if not overlay:
                self.live.pop(change.uid, None)
            return
        self.edits[change.key] = value

    def _replay(self, action: EditAction, *, forward: bool) -> list[str]:
        """Put one action's fields back (undo) or in again (redo).

        Undo walks the changes in reverse, so an action that touched several
        fields is unwound in the order it was applied.  Returns the problems
        the game reported - empty when everything landed.
        """
        problems: list[str] = []
        changes = list(action.changes if forward else reversed(action.changes))
        for change in changes:
            value = change.new if forward else change.old
            if change.column == INJURY_COLUMN and change.uid is not None:
                problem = self._replay_injury(change, forward)
                if problem:
                    problems.append(problem)
                continue
            if self._written(change) and self.editor is not None:
                problem = self._write_back(change, value)
                if problem:
                    problems.append(problem)
                continue
            if self._written(change):
                problems.append(
                    "The game is not attached, so "
                    f"{self._column_label(change.column)} was only changed on "
                    "screen.")
            self._restore_overlay(change, value)
        return problems

    def _replay_injury(self, change: EditChange, forward: bool) -> str:
        """Restore (undo) or re-clear (redo) the injury behind one heal."""
        problem = ""
        if self.editor is None or change.uid is None:
            problem = "The game is not attached, so the injury was left as it is."
        else:
            try:
                if forward:
                    self.editor.heal(change.uid)
                    injury = self.editor.read_injury(change.uid)
                    if injury is not None and injury.injured:
                        problem = "The game did not clear the injury again."
                else:
                    self.editor.restore_injury(change.uid,
                                               change.injury_reference)
                    injury = self.editor.read_injury(change.uid)
                    if injury is None or not injury.injured:
                        problem = "The game did not take the injury back."
            except Exception as exc:  # noqa: BLE001 - never leak ctypes
                problem = str(exc) or "The injury could not be changed."
        # Put the game's own record on the screen, not a remembered guess.  With
        # no game attached the record comes from what the action stored, so an
        # undo still takes the "Fit" row back to the injury it replaced.
        values = (self._injury_overlay(change.uid) or {}) \
            if self.editor is not None else {}
        overlay = self.live.setdefault(change.uid, {})
        overlay.update(values)
        # Whatever the decoded record cannot report (the expected return) comes
        # from what the action recorded.
        recorded = (change.live_after if forward else change.live_before) or {}
        for column in INJURY_LIVE_COLUMNS:
            if column in values:
                continue
            remembered = recorded.get(column)
            if remembered is None:
                overlay.pop(column, None)
            else:
                overlay[column] = remembered
        return problem

    def _settle(self, action: EditAction, problems: list[str]) -> None:
        """Record how the replay went, for ``App.undo``/``App.redo`` to show."""
        if problems:
            self.last_edit = "error"
            self.last_error = " ".join(problem for problem in problems if problem)
            return
        self.last_edit = ("game" if any(c.in_game or c.superseded
                                        for c in action.changes) else "local")
        self.last_error = ""

    @staticmethod
    def _result(action: EditAction):
        if not action.changes:
            return None
        if len(action.changes) == 1:
            change = action.changes[0]
            return change.key, change.old, change.new
        return (action.key,
                tuple(change.old for change in action.changes),
                tuple(change.new for change in action.changes))

    def dirty(self) -> int:
        with self._lock:
            return sum(1 for key, value in self.edits.items()
                       if self.saved_edits.get(key) != value)

    @staticmethod
    def reload_dir() -> str:
        """Directory under ``saves/`` that holds temporary reload/snapshot staging folders."""
        os.makedirs(RELOAD_DIR, exist_ok=True)
        return RELOAD_DIR

    @classmethod
    def create_reload_dir(cls, prefix: str = "reload-") -> str:
        """Create a temporary reload directory inside ``saves/reloads/``."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-")
        return tempfile.mkdtemp(prefix=f"{prefix}{stamp}", dir=cls.reload_dir())

    @classmethod
    def prune_reloads(cls, *, max_age_hours: float = 24.0, keep_latest: int = 3,
                      include_legacy_root: bool = True) -> dict[str, int]:
        """Remove stale reload staging folders under ``saves/reloads/`` (and legacy root)."""
        removed = 0
        now = time.time()
        cutoff = now - max(0.0, float(max_age_hours)) * 3600.0
        keep = max(0, int(keep_latest))

        if include_legacy_root and os.path.isdir(APP):
            try:
                for entry in os.scandir(APP):
                    if entry.is_dir() and entry.name.startswith("fmrte-reload-"):
                        try:
                            shutil.rmtree(entry.path)
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                pass

        remaining = 0
        if os.path.isdir(RELOAD_DIR):
            entries: list[tuple[float, str]] = []
            try:
                for entry in os.scandir(RELOAD_DIR):
                    if not entry.is_dir():
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    entries.append((mtime, entry.path))
            except OSError:
                entries = []
            entries.sort(key=lambda item: item[0], reverse=True)
            for idx, (mtime, path) in enumerate(entries):
                expired = (max_age_hours <= 0.0) or (mtime < cutoff)
                excess = idx >= keep
                if expired or excess:
                    try:
                        shutil.rmtree(path)
                        removed += 1
                    except OSError:
                        remaining += 1
                else:
                    remaining += 1
        return {"removed": removed, "remaining": remaining}

    def save(self) -> str:
        os.makedirs(SAVE_DIR, exist_ok=True)
        with self._lock:
            payload = {
                "snapshot": self.meta().get("snapshot_id", ""),
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "edits": dict(self.edits),
            }
            self.saved_edits = dict(self.edits)
        with open(EDITS_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        self.prune_reloads(max_age_hours=24.0, keep_latest=3, include_legacy_root=True)
        return EDITS_PATH

    def _load_edits(self) -> None:
        """Do not load the old local overlay.

        Earlier versions kept edits in ``saves/edits.json``. That is exactly
        what the person asked to stop: a change has to reach the game, not a
        local copy. The file is left on disk untouched, but it is never applied
        again, so the screen always shows the game's own values. The count is
        kept only so the window can mention that an old file exists.
        """
        if not os.path.exists(EDITS_PATH):
            return
        try:
            with open(EDITS_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("snapshot") == self.meta().get("snapshot_id", ""):
                self.legacy_edits = len(payload.get("edits", {}) or {})
        except (OSError, ValueError):
            pass

    def reset_edits(self) -> None:
        with self._lock:
            self.edits.clear()
            self.journal.clear()
            self.redo_stack.clear()
            self.saved_edits.clear()

    # -- derived ----------------------------------------------------------
    @staticmethod
    def display_name(player: dict[str, Any]) -> str:
        live = (player.get("display_name") or "").strip()
        if live:
            return live
        full = (player.get("name") or "").strip()
        return full

    @staticmethod
    def split_name(player: dict[str, Any]) -> tuple[str, str, str]:
        return _split_name(player.get("name") or "")

    @staticmethod
    def display_club(name: str | None) -> str:
        if not name:
            return "Free agent"
        return CLUB_DISPLAY.get(name, name)

    @staticmethod
    def positions(player: dict[str, Any], threshold: int = 15) -> list[tuple[str, int]]:
        # The rule lives in spec.py, so ratings can use it without importing
        # this module (that import used to close a db <-> ratings cycle).
        return spec.player_positions(player, threshold)

    @staticmethod
    def position_text(player: dict[str, Any]) -> str:
        return spec.position_text(player)

    @staticmethod
    def role_name(player: dict[str, Any]) -> str:
        text = Snapshot.position_text(player)
        primary = text.split("/")[0] if text and text != "—" else ""
        return {
            "GK": "Goalkeeper",
            "DL": "Full Back", "DR": "Full Back", "WBL": "Wing Back",
            "WBR": "Wing Back", "DC": "Centre Back",
            "DM": "Defensive Midfielder", "ML": "Wide Midfielder",
            "MR": "Wide Midfielder", "MC": "Central Midfielder",
            "AML": "Wide Forward", "AMR": "Wide Forward",
            "AMC": "Attacking Midfielder", "ST": "Striker",
        }.get(primary, "Player")

    @staticmethod
    def shirt_number(player: dict[str, Any]) -> int | None:
        return SHIRT_HINTS.get(str(player.get("unique_id")))

    @staticmethod
    def age(player: dict[str, Any]) -> int:
        return int(player.get("age") or 0)

    @staticmethod
    def dob(player: dict[str, Any]) -> str:
        return _fmt_dob(player.get("date_of_birth"))

    @staticmethod
    def date(value: str | None) -> str:
        return _fmt_date(value)

    @staticmethod
    def rating(player: dict[str, Any], potential: bool = False) -> float:
        """The original's "Rating cur / pot".

        This is the CA-share blend described by the original's own settings
        ("How much CA counts in Actual Rating and Potential Rating. The rest
        comes from the player's best role. Default: 50%."), not CA/200 and not
        a plain key-attribute average. CA_SHARE * CA + (1 - CA_SHARE) * the
        best role's 16/4 weighted score, held in a float32 and truncated to two
        decimals, which is why Pulisic reads 77.49 % and not 77.50 %. See
        MECHANICS.md section 2 and fmrte/ratings.py.
        """
        from .ratings import role_rating

        return role_rating(player)

    @staticmethod
    def initials(player: dict[str, Any]) -> str:
        first, last, _ = _split_name(player.get("name") or "")
        return ((first[:1] if first else "") + (last[:1] if last else "")).upper() or "?"

    @staticmethod
    def condition_pct(player: dict[str, Any]) -> float:
        try:
            return max(0.0, min(100.0, float(player.get("condition") or 0) / 100.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def sharpness_pct(player: dict[str, Any]) -> float:
        try:
            return max(0.0, min(100.0, float(player.get("match_sharpness") or 0) / 100.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def game_date(meta: dict[str, str]) -> str:
        value = meta.get("game_date") or ""
        return value.replace("-", "/") if value else "—"

    @staticmethod
    def game_version(meta: dict[str, str]) -> str:
        return meta.get("game_version") or "27.1.0"


def find_snapshot_paths() -> list[str]:
    roots = [DATA]
    out = []
    for root in roots:
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                if name.endswith(".db"):
                    out.append(os.path.join(root, name))
    return out
