"""Single authority for FM27 player field and position metadata.

One field, one place.  Before this module existed the same facts were written
out by hand in six modules, and they had already drifted: three attribute
tables (``db``, ``ratings``, and the screens' own column lists), four position
tables (``db``, ``memmap``, ``fmtools``, ``pages/player``), and two range tables
that disagreed about what the game accepts.  Changing a display name or a
column in one copy left the others pointing at a column that no longer existed.

Everything below is the one copy.  The other modules derive from it and
re-export the public names they used to declare, so no caller and no screen
changed:

    spec.ATTRIBUTE_COLUMNS       the 52 editable attributes the screens show
                                 -> db.ATTRIBUTE_COLUMNS
    spec.ATTRIBUTE_COLUMN_NAMES  the wider 62-column set the ratings maths and
                                 the live writer clamp -> ratings.ATTRIBUTE_COLUMNS
    spec.POSITIONS               the 14 positions (column, template name, short
                                 label, role-table group) -> db.POSITIONS,
                                 fmtools.POSITION_FIELDS / POSITION_LABELS /
                                 LABEL_TO_GENERAL, memmap.POSITION_COLUMNS,
                                 ratings.POSITION_TO_GENERAL
    spec.RANGES / spec.clamp     the edit ranges -> ratings.EDIT_RANGES,
                                 ratings.clamp
    spec.LIVE_RANGES             the live-memory ranges -> livewrite.RANGES

This module imports **nothing** from the project and nothing heavy from the
standard library (no sqlite3, ctypes or tkinter).  That is deliberate: both
``db`` and ``ratings`` need it, so it must not need either of them back.  That
is what breaks the old ``db`` <-> ``ratings`` import cycle, which used to be
worked around with deferred imports inside functions.

Provenance: the numbers are the running FM27 record layout (``memmap.py``) and
the original FMRTE's own data and machine code.  See MECHANICS.md sections 9.5,
13.5 and 14 for where each one was read.  The contract tests in
``tests/test_snapshot.py`` fail if any consumer drifts away from this table.
"""

from __future__ import annotations

from collections import namedtuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The maximums the original's own property setters are called with, each found
# as ``mov r8d, <max>`` followed by ``xor r9d, r9d`` (minimum 0) and a call:
#
#   0x1411782B8  mov r8d, 0x14    -> 20      attribute
#   0x1411D0A48  mov r8d, 0xC8    -> 200     current / potential ability
#   0x14102D4E0  mov r8d, 0x2710  -> 10000   condition / sharpness / reputation
#
# Honest note (MECHANICS.md 13.5): 0x1404446B0 is a virtual-dispatch wrapper,
# not a clamp, so these are observed from those call sites rather than proven
# by a helper.
ATTRIBUTE_MAX = 20
ABILITY_MAX = 200
CONDITION_MAX = 10000

# The original stores a visible attribute as 0..100 and shows it as 1..20:
#   norm20(v) = clamp(round(v * 20 / 100), 1, 20)
# Positions and the hidden personality attributes are stored 1..20 directly.
ATTRIBUTE_STORAGE_SCALE = 5


# ---------------------------------------------------------------------------
# Attributes: the 52 the screens show
# ---------------------------------------------------------------------------
# (display name, snapshot/live column, screen group).  Order, display names and
# groups are what the player page and the assistant print, so they are part of
# the contract: change them here and every screen changes at once.
#
# The five "Hidden" ones sit between Physical and Goalkeeping because that is
# where the screens have always put them; they are real attributes the game
# clamps to 0..20, just not ones a scout would be shown.

Attribute = namedtuple("Attribute", "label column group")

ATTRIBUTES: tuple[Attribute, ...] = (
    # -- Technical (14) -----------------------------------------------------
    Attribute("Corners", "corners", "Technical"),
    Attribute("Crossing", "crossing", "Technical"),
    Attribute("Dribbling", "dribbling", "Technical"),
    Attribute("Finishing", "finishing", "Technical"),
    Attribute("First Touch", "first_touch", "Technical"),
    Attribute("Free Kick Taking", "free_kicks", "Technical"),
    Attribute("Heading", "heading", "Technical"),
    Attribute("Long Shots", "long_shots", "Technical"),
    Attribute("Long Throws", "long_throws", "Technical"),
    Attribute("Marking", "marking", "Technical"),
    Attribute("Passing", "passing", "Technical"),
    Attribute("Penalty Taking", "penalties", "Technical"),
    Attribute("Tackling", "tackling", "Technical"),
    Attribute("Technique", "technique", "Technical"),
    # -- Mental (14) --------------------------------------------------------
    Attribute("Aggression", "aggression", "Mental"),
    Attribute("Anticipation", "anticipation", "Mental"),
    Attribute("Bravery", "bravery", "Mental"),
    Attribute("Composure", "composure", "Mental"),
    Attribute("Concentration", "concentration", "Mental"),
    Attribute("Decisions", "decisions", "Mental"),
    Attribute("Determination", "determination", "Mental"),
    Attribute("Flair", "flair", "Mental"),
    Attribute("Leadership", "leadership", "Mental"),
    Attribute("Off The Ball", "off_the_ball", "Mental"),
    Attribute("Positioning", "positioning", "Mental"),
    Attribute("Team Work", "teamwork", "Mental"),
    Attribute("Vision", "vision", "Mental"),
    Attribute("Work Rate", "work_rate", "Mental"),
    # -- Physical (8) -------------------------------------------------------
    Attribute("Acceleration", "acceleration", "Physical"),
    Attribute("Agility", "agility", "Physical"),
    Attribute("Balance", "balance", "Physical"),
    Attribute("Jumping Reach", "jumping_reach", "Physical"),
    Attribute("Natural Fitness", "natural_fitness", "Physical"),
    Attribute("Pace", "pace", "Physical"),
    Attribute("Stamina", "stamina", "Physical"),
    Attribute("Strength", "strength", "Physical"),
    # -- Hidden (5) ---------------------------------------------------------
    Attribute("Consistency", "consistency", "Hidden"),
    Attribute("Dirtiness", "dirtiness", "Hidden"),
    Attribute("Important Matches", "important_matches", "Hidden"),
    Attribute("Injury Proneness", "injury_proneness", "Hidden"),
    Attribute("Versatility", "versatility", "Hidden"),
    # -- Goalkeeping (11) ---------------------------------------------------
    Attribute("Handling", "handling", "Goalkeeping"),
    Attribute("Aerial Reach", "aerial_ability", "Goalkeeping"),
    Attribute("Command Of Area", "command_of_area", "Goalkeeping"),
    Attribute("Communication", "communication", "Goalkeeping"),
    Attribute("Kicking", "kicking", "Goalkeeping"),
    Attribute("One On Ones", "one_on_ones", "Goalkeeping"),
    Attribute("Reflexes", "reflexes", "Goalkeeping"),
    Attribute("Rushing Out", "rushing_out", "Goalkeeping"),
    Attribute("Tendency To Punch", "tendency_to_punch", "Goalkeeping"),
    Attribute("Throwing", "throwing", "Goalkeeping"),
    Attribute("Eccentricity", "eccentricity", "Goalkeeping"),
)

# The same 52 rows in the shape ``db.ATTRIBUTE_COLUMNS`` has always had:
# (display name, column, group).  The namedtuple unpacks exactly like the old
# plain 3-tuple, so ``for name, column, group in ATTRIBUTE_COLUMNS`` is
# unchanged at every call site.
ATTRIBUTE_COLUMNS = ATTRIBUTES


# ---------------------------------------------------------------------------
# Attributes: the wider 62-column set
# ---------------------------------------------------------------------------
# ``ratings`` (and the live writer) clamp more columns than the screens show:
# the two preferred-foot strengths and the eight personality drivers the game
# keeps beside the record.  These are real attribute columns stored 0..20 /
# 1..20; they are listed here so a range, a role table or a memory offset can
# never name a column this build does not know.

HIDDEN_ATTRIBUTE_COLUMNS: tuple[str, ...] = (
    "controversy",
    "left_foot",
    "loyalty",
    "pressure",
    "professionalism",
    "right_foot",
    "sportsmanship",
    "temperament",
    "adaptability",
    "ambition",
)

# The exact order ``ratings.ATTRIBUTE_COLUMNS`` has always used: alphabetical,
# with the two personality drivers (adaptability, ambition) last.  Keeping the
# order keeps the key order of serialised per-player attribute maps stable, so
# a tool response is byte-for-byte what it was before the consolidation.
ATTRIBUTE_COLUMN_NAMES: tuple[str, ...] = (
    tuple(sorted(
        ({attribute.column for attribute in ATTRIBUTES}
         | set(HIDDEN_ATTRIBUTE_COLUMNS))
        - {"adaptability", "ambition"}
    ))
    + ("adaptability", "ambition")
)

# Just the columns the screens display, in screen order.
DISPLAYED_ATTRIBUTE_COLUMNS: tuple[str, ...] = tuple(
    attribute.column for attribute in ATTRIBUTES)


# ---------------------------------------------------------------------------
# Positions: the 14 a player can be competent in
# ---------------------------------------------------------------------------
# (snapshot/live column, tactic-template name, short label, role-table group).
#
# * the template name is what a tactic template and ``fmtools`` call the slot
#   ("DefenderCentral");
# * the short label is what the screens and the plans print ("DC");
# * the group is the general position the original's role table files that
#   position under ("DefenderCentral", "FullBack", "WingBack", ...).  Several
#   positions share one group: DL and DR are both FullBack, and every wide
#   midfield/attacking slot is a Winger.
#
# The order is the UI's own (GK, DL, DC, DR, WBL, DM, WBR, ML, MC, MR, AML,
# AMC, AMR, ST) because ``db.POSITIONS`` keeps it for the position strip; the
# live/memory side only ever asks "is this column a position", so it does not
# care about order.

Position = namedtuple("Position", "column template label general")

POSITIONS: tuple[Position, ...] = (
    Position("goalkeeper", "Goalkeeper", "GK", "Goalkeeper"),
    Position("defender_left", "DefenderLeft", "DL", "FullBack"),
    Position("defender_central", "DefenderCentral", "DC", "DefenderCentral"),
    Position("defender_right", "DefenderRight", "DR", "FullBack"),
    Position("wing_back_left", "WingBackLeft", "WBL", "WingBack"),
    Position("defensive_midfielder", "DefensiveMidfielder", "DM",
             "DefensiveMidfielder"),
    Position("wing_back_right", "WingBackRight", "WBR", "WingBack"),
    Position("midfielder_left", "MidfielderLeft", "ML", "Winger"),
    Position("midfielder_central", "MidfielderCentral", "MC",
             "MidfielderCentral"),
    Position("midfielder_right", "MidfielderRight", "MR", "Winger"),
    Position("attacking_midfielder_left", "AttackingMidfielderLeft", "AML",
             "Winger"),
    Position("attacking_midfielder_central", "AttackingMidfielderCentral",
             "AMC", "AttackingMidfielderCentral"),
    Position("attacking_midfielder_right", "AttackingMidfielderRight", "AMR",
             "Winger"),
    Position("striker", "Striker", "ST", "Striker"),
)

# Derived lookups.  Every position table in the build is one of these.
POSITION_COLUMNS: frozenset[str] = frozenset(
    position.column for position in POSITIONS)

# Template name -> snapshot column (``fmtools.POSITION_FIELDS``).
POSITION_FIELDS: dict[str, str] = {
    position.template: position.column for position in POSITIONS}

# Template name -> short label (``fmtools.POSITION_LABELS``).
POSITION_LABELS: dict[str, str] = {
    position.template: position.label for position in POSITIONS}

# Short label -> snapshot column (``db.POSITIONS`` and ``pages/player.py``).
POSITION_LABEL_TO_COLUMN: dict[str, str] = {
    position.label: position.column for position in POSITIONS}

# Short label -> general position in the original's role table
# (``ratings.POSITION_TO_GENERAL`` and ``fmtools.LABEL_TO_GENERAL``).
LABEL_TO_GENERAL: dict[str, str] = {
    position.label: position.general for position in POSITIONS}

# The order the original's own position list uses (config.json
# ColumnsSettings "Positions.*").  It is not the table order above: it is the
# order roles are offered in, and the tie-break when two positions are equally
# good, so it is its own fact and stays here once.
GENERAL_ORDER: tuple[str, ...] = (
    "Goalkeeper", "DefenderCentral", "FullBack", "WingBack",
    "DefensiveMidfielder", "MidfielderCentral", "Winger",
    "AttackingMidfielderCentral", "Striker",
)

# Which snapshot columns hold competence in each general position.  Grouped
# from the table above, so a new position joins its group automatically.
GENERAL_TO_COMPETENCE: dict[str, tuple[str, ...]] = {
    general: tuple(position.column for position in POSITIONS
                   if position.general == general)
    for general in GENERAL_ORDER
}


def player_positions(player: dict, threshold: int = 15) -> list[tuple[str, int]]:
    """The positions a player is at least ``threshold`` good at, best first.

    Moved here from ``db.Snapshot.positions`` so ``ratings`` can read a
    player's displayed position without importing ``db`` (that import was half
    of the old cycle).  ``db.Snapshot.positions`` now calls this, so there is
    one copy of the rule.
    """
    found: list[tuple[str, int]] = []
    for position in POSITIONS:
        try:
            number = int(player.get(position.column))
        except (TypeError, ValueError):
            number = 0
        if number >= threshold:
            found.append((position.label, number))
    found.sort(key=lambda item: -item[1])
    return found


def position_text(player: dict) -> str:
    """The position strip the screens print, e.g. ``AML/AMC/AMR``.

    Moved here from ``db.Snapshot.position_text`` for the same reason as
    ``player_positions``: ``ratings.best_general_position`` needs the primary
    label when a player has no competence data at all, and it must not import
    ``db`` to get it.  ``db.Snapshot.position_text`` calls this, so both the
    header and the rating maths agree by construction.
    """
    found = player_positions(player)
    if not found:
        return "—"
    wide = [item for item in found if item[0] in ("AML", "AMC", "AMR")]
    wide.sort(key=lambda item: ("AML", "AMC", "AMR").index(item[0]))
    striker = [item for item in found if item[0] == "ST"]
    if wide:
        chosen = wide[:3]
    else:
        best = found[0][0]
        if best in ("GK",):
            chosen = found[:1]
        elif best in ("DL", "DC", "DR", "WBL", "WBR"):
            chosen = [item for item in found
                      if item[0] in ("DL", "DC", "DR", "WBL", "WBR")][:3]
        elif best in ("DM", "ML", "MC", "MR"):
            chosen = [item for item in found
                      if item[0] in ("DM", "ML", "MC", "MR")][:3]
        else:
            chosen = (striker or found)[:3]
    return "/".join(label for label, _ in chosen)


# ---------------------------------------------------------------------------
# Direct record fields (no attribute block)
# ---------------------------------------------------------------------------
# The player record keeps these outside the attribute block; ``memmap.py`` owns
# their offsets and struct formats, this table owns their names.  ``unique_id``
# is identity and is never edited.  ``asking_price_raw`` is the raw u32 price:
# it is deliberately not in ``RANGES`` (the snapshot leaves it alone), while
# the live writer does clamp it to its u32 domain - see ``LIVE_RANGES``.

DIRECT_COLUMNS: tuple[str, ...] = (
    "ca",
    "pa",
    "condition",
    "match_sharpness",
    "jadedness",
    "morale",
    "home_reputation",
    "current_reputation",
    "world_reputation",
    "asking_price_raw",
    "height_cm",
    "unique_id",
)


# ---------------------------------------------------------------------------
# Value ranges, and the one clamp
# ---------------------------------------------------------------------------
# ``RANGES`` is the canonical table: the valid (low, high) for the values the
# snapshot and the screens edit.  Every attribute column shares
# ``ATTRIBUTE_RANGE`` and every position shares ``POSITION_RANGE``;
# ``range_for`` answers for all of them, so no caller has to keep its own
# default.
#
# The minimums are 0 for every value the original writes through its clamp
# helper, except jadedness, which is signed: its Destroy preset sets 1000 and
# the person's save holds -125.  ``height_cm`` is a real height, not a raw
# field, so it is bounded like one.
#
# ``morale`` is the one entry whose canonical ceiling is *not* the game's:
# the snapshot and the screens use the 1..20 scale (Pulisic reads 14), while
# the running game stores the same field as a 0..100 byte (see
# ``LIVE_OVERRIDES`` below).  The decision is that 20 stays the canonical
# ceiling and the clamp applies only to a value that is about to live in the
# local overlay.  A value on its way into the game is handed to
# ``livewrite``'s own clamp (``LIVE_RANGES``, 0..100) unclamped and recorded as
# whatever the game read back, so a live morale of 80 stays 80 instead of
# silently becoming 20, and an undo puts the game's 80 back instead of 20.
# ``Snapshot._attempt`` is where that split is made.

RANGES: dict[str, tuple[int, int]] = {
    "ca": (0, ABILITY_MAX),
    "pa": (0, ABILITY_MAX),
    "condition": (0, CONDITION_MAX),
    "match_sharpness": (0, CONDITION_MAX),
    "morale": (0, ATTRIBUTE_MAX),
    "jadedness": (-CONDITION_MAX, CONDITION_MAX),
    "home_reputation": (0, CONDITION_MAX),
    "current_reputation": (0, CONDITION_MAX),
    "world_reputation": (0, CONDITION_MAX),
    "height_cm": (150, 230),
}

# The range every editable attribute column shares.
ATTRIBUTE_RANGE: tuple[int, int] = (0, ATTRIBUTE_MAX)

# The range every position column shares.  A position is stored 1..20 directly
# (a position the player cannot play at all is 1, not 0 - the same rule the
# live writer's ``_bounds`` applies), so a typed 99 must not reach the game or
# the screen: it is 20.
POSITION_RANGE: tuple[int, int] = (1, ATTRIBUTE_MAX)


def range_for(column: str) -> tuple[int, int] | None:
    """The canonical (low, high) for an editable column, or None.

    ``None`` means "this column is not range-clamped here", which is why
    ``clamp`` leaves it untouched (asking price, contract fields, identity).
    """
    if column in RANGES:
        return RANGES[column]
    if column in POSITION_COLUMNS:
        return POSITION_RANGE
    if column in ATTRIBUTE_COLUMN_NAMES:
        return ATTRIBUTE_RANGE
    return None


def clamp(column: str, value: int) -> int:
    """Clamp an edited value the way the original's own code does.

    One implementation for the whole build: ``ratings.clamp`` delegates here,
    so the snapshot edit path, the screens and the rating maths can never
    disagree about what a legal value is.
    """
    bounds = range_for(column)
    if bounds is None:
        return value
    low, high = bounds
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Live-memory ranges: where the stored domain differs from the screen's
# ---------------------------------------------------------------------------
# The live writer edits the running game's own bytes, and a few of those bytes
# do not mean what the snapshot column of the same name means.  The two tables
# disagreed when they were first compared; each difference was investigated
# and this is the decision:
#
#   ca / pa      canonical 0..200, live 1..200.  The observed setter call
#                passes a minimum of 0 (``xor r9d, r9d``), so 0 is what the
#                original accepts, and the canonical table keeps it.  The live
#                writer guards with 1: a current ability of 0 is not a value
#                the game offers (the writer's table does not say why, so this
#                is inference, not observation).  That guard belongs to the
#                write path rather than to the snapshot semantics, so it stays
#                an override instead of becoming canonical.
#   morale       canonical 0..20, live 0..100.  The snapshot and the screens
#                use the 1..20 scale (Pulisic reads 14), so 20 is the correct
#                ceiling for a value the person sees and confirms *in the
#                snapshot's own domain*.  The live reader decodes the byte with
#                ``min(100, ...)`` (``live.py``), so its domain is genuinely
#                wider, and a value that came out of the game must survive the
#                round trip: the data layer clamps with this table only when the
#                value stays in the local overlay and lets the live writer's
#                table clamp on the way into the game.
#   height_cm    canonical 150..230, live 120..250.  The snapshot column is a
#                footballer's height in cm; the live field is the raw cm byte.
#                The stricter 150..230 is canonical, because it is the one the
#                screens show and a 120 cm or 250 cm player is not a value the
#                game should be given.
#
# Every other disagreement is forbidden: the contract test in
# ``tests/test_snapshot.py`` requires each column outside this table to be
# identical in ``RANGES`` and ``LIVE_RANGES``, so a future edit to one of them
# fails the suite instead of silently drifting.

LIVE_OVERRIDES: dict[str, tuple[tuple[int, int], str]] = {
    "ca": ((1, ABILITY_MAX),
           "the game writes no current ability of 0"),
    "pa": ((1, ABILITY_MAX),
           "the game writes no potential ability of 0"),
    "morale": ((0, 100),
               "the live byte is decoded 0..100, not the screen's 0..20"),
    "height_cm": ((120, 250),
                  "the live field is the raw cm byte, not a screen value"),
}

# What the live writer accepts.  ``livewrite.py`` no longer declares its own
# table: it re-exports ``RANGES = spec.LIVE_RANGES`` (the migration this comment
# used to say was still to come), so this object *is* the live write path's
# range table and a change here changes what the game is allowed to receive.
# The contract test in ``tests/test_snapshot.py`` still pins the equality, so a
# module that went back to a private copy fails the suite.
LIVE_RANGES: dict[str, tuple[int, int]] = {
    **RANGES,
    **{column: bounds for column, (bounds, _reason)
       in LIVE_OVERRIDES.items()},
    # Raw contract values the snapshot never clamps but the writer does:
    # the u32 asking-price field and the weekly wage in the registration block.
    "asking_price_raw": (0, 4_294_967_295),
    "salary_weekly_raw": (0, 4_000_000),
}


# ---------------------------------------------------------------------------
# Everything the build knows how to read or write on a player
# ---------------------------------------------------------------------------
# One list for the contract tests and for anything that has to ask "is this a
# real player column?".  It is the union of the tables above, in a stable
# order, plus the one registration column the live writer edits.

KNOWN_COLUMNS: tuple[str, ...] = tuple(dict.fromkeys(
    DIRECT_COLUMNS
    + ATTRIBUTE_COLUMN_NAMES
    + tuple(position.column for position in POSITIONS)
    + ("salary_weekly_raw",)
))

KNOWN_COLUMN_SET: frozenset[str] = frozenset(KNOWN_COLUMNS)
