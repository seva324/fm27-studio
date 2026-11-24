"""Role ratings, exactly as FMRTE 27.1.0-build14 computes them.

Every constant here was read out of the original's own data files:

    Ratings/rolesAndDuties.json   KeyAttributesWeight, PreferableAttributesWeight,
                                  AvoidAttributesWeight, PositionRatingPenalties,
                                  and the position -> role -> duty -> attributes map
    Ratings/teamSelection.json    the squad-selection weights
    Ratings/wonderkidGenerator.json  the attribute roll bands
    Ratings/abilityWeights.json   per-position attribute weights (Recommended CA)

The original distinguishes FOUR figures, and this module keeps them apart:

    Best Role Rating   the role's key attributes over their maximum
    Actual Rating      CaShare of CA/200 blended with the rest from the best role
    Potential Rating   the same blend computed at the potential
    RCA                "Recommended CA": ability estimated from the attributes
                       with abilityWeights.json

The header pill "Rating cur / pot" in this build shows the **Best Role Rating**.
That is the figure verified against the original's own screen for Christian
Pulisic (uid 92074020): key attributes sum to 124 of 160 = 77.50%, and the
original shows 77.49% because it holds the ratio in a float32.

The CA blend's default is compiled into the original, not read from the JSON:
`0.5` is stored into the role-weights object at field +0x38 by the instruction
at 0x14074042C (see CA_SHARE below and MECHANICS.md section 14).

See MECHANICS.md sections 2 and 14 for the derivation and the evidence.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from . import spec
from .role_definitions import (
    AVOID_ATTRIBUTES_WEIGHT,
    KEY_ATTRIBUTES_WEIGHT,
    PREFERABLE_ATTRIBUTES_WEIGHT,
    RATINGS,
    SETTINGS_VERSION,
)

# --- constants observed in Ratings/rolesAndDuties.json ---------------------
# KEY_ATTRIBUTES_WEIGHT (16), PREFERABLE_ATTRIBUTES_WEIGHT (4),
# AVOID_ATTRIBUTES_WEIGHT (3) and SETTINGS_VERSION are imported above from
# role_definitions, which is generated verbatim from the original's own file.
#
# The same three weights are ALSO compiled into the original as IEEE-754
# doubles, which is where they are actually read from at run time. The four
# stores are at these exact addresses (each a `mov r64, imm64` then
# `mov [rbx+disp8], r64`), inside the defaults initialiser that starts at
# 0x140740380 (its dispatch tables are 0x146234688 / 0x146234694 / 0x1462346A0):
#   0x140740455 -> +0x20  16.0 (0x4030000000000000)  KeyAttributesWeight
#   0x1407403FB -> +0x28   4.0 (0x4010000000000000)  PreferableAttributesWeight
#   0x140740410 -> +0x30   3.0 (0x4008000000000000)  AvoidAttributesWeight
#   0x14074042C -> +0x38   0.5 (0x3FE0000000000000)  CaShare
# 0x14074032C, named by the earlier sheet, is NOP padding, not the entry.
#
# The exact instruction bytes were re-read this pass with native_bytes_show
# (MECHANICS.md section 17.1). They are `48 B9 <imm64>` / `48 89 4B 28` (4.0),
# `48 B9 <imm64>` / `48 89 4B 30` (3.0), `48 B9 <imm64>` / `48 89 4B 38` (0.5)
# and `48 B8 <imm64>` / `48 89 43 20` (16.0). Note 4.0/3.0/0.5 load through
# rcx (48 B9) and only 16.0 through rax (48 B8) — a pattern search for `48 B8`
# alone silently misses three of the four stores.

# --- the role-weights object the original builds at start-up ---------------
# Two code paths touch this object, and both were read this pass.
#
# (a) The constructor at 0x140740130 (82 bytes) — `mov [rcx+0x18], <frozen int[]>`,
#     `mov [rcx+0x38], 0x3FE0000000000000` (0.5) and a `new Dictionary` into +0x10.
#     So the plain constructor leaves the ladder EMPTY and sets only CaShare.
# (b) The field dispatch at 0x140740380 — it switches on a small field index
#     through three tables (0x146234688 / 0x146234694 / 0x1462346A0, all adding
#     the base 0x14074034B) and each case stores one field. This is where the
#     16.0 / 4.0 / 3.0 / 0.5 defaults and the real 20-entry ladder are installed:
#
#   offset  value  build  immediate load   store          meaning
#   +0x10   map    --                     0x140740441/445  GeneralPositionToRole...Map
#   +0x18   int20  --                     0x14074046A/46F  PositionRatingPenalties
#   +0x20   16.0   0x4030000000000000     0x140740455/45F  KeyAttributesWeight
#   +0x28    4.0   0x4010000000000000     0x1407403FB/405  PreferableAttributesWeight
#   +0x30    3.0   0x4008000000000000     0x140740410/41A  AvoidAttributesWeight
#   +0x38    0.5   0x3FE0000000000000     0x14074042C/436  CaShare
#
# 0x140740445 copies [+0x10] of the object built by 0x140741FA0 (the expanded
# role map), and 0x14074046F copies the int[20] that 0x140741F50 returns.
# The accessors confirm it (bytes read this pass, MECHANICS.md 17.1):
#   0x1407400F0  mov rax,[rcx+0x18]   ret        get_PositionRatingPenalties
#   0x140740100  lea rcx,[rcx+0x18]   call ...   set_PositionRatingPenalties
# So +0x18 is the ladder and +0x10 is the map. The earlier sheet had them the
# other way round; the validator (0x1407404C0, which reads [+0x18] and demands
# an element count of 0x14) agrees with the accessors.
#
# The accessors are doubles: 0x140740090/0A0 read and write `[rcx+0x20]` with
# `movsd xmm0, [rcx+disp8]`; likewise 0x1407400B0/0C0 (+0x28), 0x1407400D0/0E0
# (+0x30) and 0x140740110/120 (+0x38).
ROLE_WEIGHTS_OBJECT = {
    "constructor": "0x140740130",
    "builder_dispatch": "0x140740380",
    "jump_table": "0x146234688",
    "jump_table_bases": ("0x146234688", "0x146234694", "0x1462346A0"),
    "jump_base": "0x14074034B",
    "fields": {
        # Corrected in the second pass: the accessors themselves say which slot
        # is which (see ROLE_WEIGHTS_ACCESSORS). The earlier sheet had these two
        # swapped, and the validator agrees with the accessors, not the sheet:
        # 0x1407404C0 reads [+0x18] and requires its element count to be 0x14.
        "GeneralPositionToRoleAndDutiesDefinitionsMap": 0x10,
        "PositionRatingPenalties": 0x18,
        "KeyAttributesWeight": 0x20,
        "PreferableAttributesWeight": 0x28,
        "AvoidAttributesWeight": 0x30,
        "CaShare": 0x38,
    },
    "stores": {
        "KeyAttributesWeight": ("0x140740455", 16.0),
        "PreferableAttributesWeight": ("0x1407403FB", 4.0),
        "AvoidAttributesWeight": ("0x140740410", 3.0),
        "CaShare": ("0x14074042C", 0.5),
    },
    "accessors": {
        "KeyAttributesWeight": ("0x140740090", "0x1407400A0"),
        "PreferableAttributesWeight": ("0x1407400B0", "0x1407400C0"),
        "AvoidAttributesWeight": ("0x1407400D0", "0x1407400E0"),
        "CaShare": ("0x140740110", "0x140740120"),
        "PositionRatingPenalties": ("0x1407400F0", "0x140740100"),
    },
}

# The getters and setters as the instruction that proves the slot, byte for
# byte (native_bytes_show 0x140740090, 160 bytes; MECHANICS.md 17.1):
ROLE_WEIGHTS_ACCESSORS = {
    "KeyAttributesWeight": {
        "getter": "0x140740090", "getter_op": "movsd xmm0,[rcx+0x20]",
        "setter": "0x1407400A0", "setter_op": "movsd [rcx+0x20],xmm1",
    },
    "PreferableAttributesWeight": {
        "getter": "0x1407400B0", "getter_op": "movsd xmm0,[rcx+0x28]",
        "setter": "0x1407400C0", "setter_op": "movsd [rcx+0x28],xmm1",
    },
    "AvoidAttributesWeight": {
        "getter": "0x1407400D0", "getter_op": "movsd xmm0,[rcx+0x30]",
        "setter": "0x1407400E0", "setter_op": "movsd [rcx+0x30],xmm1",
    },
    "PositionRatingPenalties": {
        "getter": "0x1407400F0", "getter_op": "mov rax,[rcx+0x18]",
        "setter": "0x140740100", "setter_op": "lea rcx,[rcx+0x18]",
    },
    "CaShare": {
        "getter": "0x140740110", "getter_op": "movsd xmm0,[rcx+0x38]",
        "setter": "0x140740120", "setter_op": "movsd [rcx+0x38],xmm1",
    },
}

# The object the original ends up with when nothing overrides it. These are the
# defaults the initialiser stores, and they equal Ratings/rolesAndDuties.json.
DEFAULT_ROLE_WEIGHTS = {
    "KeyAttributesWeight": 16.0,
    "PreferableAttributesWeight": 4.0,
    "AvoidAttributesWeight": 3.0,
    "CaShare": 0.5,
}

# The settings rules 0x1407404C0 and 0x140741DE0 actually enforce. Read out of
# their code, not guessed (MECHANICS.md 17.2 / 17.3):
#
#   0x1407404C0 (279 bytes) rejects the object unless
#     * +0x10 is not null                       (the expanded role map)
#     * sub_1407406C0(obj) != 0                 (a companion check)
#     * +0x18 is not null and its length == 20  (the penalty ladder)
#     * the ladder satisfies a comparer check   (call 0x1431FEF00, result != 0)
#     * +0x20 (key weight)   >= 0.0
#     * +0x28 (preferable)   >= 0.0
#     * +0x30 (avoid)        >= 0.0
#     * +0x20 + +0x28 + +0x30 > 0.0     <-- the sum rule
#   CaShare (+0x38) is NOT validated by it.
#
#   0x140741DE0 (310 bytes) converts three collections to int and requires each
#   total to be exactly 0x64 = 100: the three role-weight groups must each add
#   up to 100 %.
ROLE_WEIGHTS_RULES = {
    "validator": "0x1407404C0",
    # 0x1407404C0 calls 0x1407406C0 before it looks at the weights. That second
    # function walks the position enum and rejects the object unless every
    # position has a role entry and every duty's attribute resolves; it is the
    # completeness rule implemented by validate_role_definitions() below.
    "roles_completeness_validator": "0x1407406C0",
    "group_validator": "0x140741DE0",
    "ladder_length": 20,
    "weights_must_be_non_negative": True,
    "weights_sum_must_be_positive": True,
    "group_total": 100,
    "ca_share_validated": False,
}

# The original's own type names for this feature, read out of its reflection
# metadata in .rdata. They are recorded so a later reader can go from a name to
# the address without searching the 128 MB image again; they are names, not
# behaviour. Two of them explain the shape this module models:
#   PositionRoleAndDutiesRate has BOTH a PlayerPositionRate and a
#   GeneralPositionRate, and BestFormationsCalculator is a separate calculator,
#   so "best position" is computed outside the rating object.
SCOUT_TYPE_MAP = {
    "PlayerRating": "0x143775823",
    "IPlayerRating": "0x14377574d",
    "PositionRoleAndDutiesRate": "0x14377588b",
    "PositionRoleRates": "0x1437758f2",
    "WeightedAttribute": "0x143775616",
    "DutyAttribute": "0x14377551f",
    "DutiesDictionary": "0x1437754a1",
    "PositionRolesAndDutiesSettings": "0x143775aa8",
    "DefaultPositionRolesAndDutiesSettings": "0x143775952",
    "BestFormationsCalculator": "0x1437756ea",
    "GetPlayerPositionRate": "0x143877b51",
}

_ROLE_WEIGHTS_CACHE: dict[str, float] | None = None

# Filled by role_weights() the first time the weights are resolved: the problems
# 0x1407406C0 would report about the role table in use. Empty when the table is
# complete, which is what the shipped one is.
ROLE_DEFINITION_PROBLEMS: tuple[str, ...] = ()


def validate_role_weights(weights: dict[str, Any]) -> list[str]:
    """The original's own checks on a role-weights object.

    Returns a list of problems; an empty list means the object is valid. The
    conditions are the ones 0x1407404C0 tests, in its order, plus the 20-entry
    ladder rule (which it tests on the array, here on the number of entries).
    CaShare is deliberately not checked, because the original does not check it.
    """
    problems: list[str] = []
    keys = ("KeyAttributesWeight", "PreferableAttributesWeight", "AvoidAttributesWeight")
    values: dict[str, float] = {}
    for key in keys:
        try:
            values[key] = float(weights[key])
        except (KeyError, TypeError, ValueError):
            problems.append(f"{key} is missing or not a number")
    for key in keys:
        if key in values and values[key] < 0.0:
            problems.append(f"{key} is negative ({values[key]:g})")
    ladder = weights.get("PositionRatingPenalties")
    if ladder is None:
        problems.append("PositionRatingPenalties is missing")
    else:
        try:
            length = len(ladder)
        except TypeError:
            problems.append("PositionRatingPenalties is not a list")
        else:
            if length != ROLE_WEIGHTS_RULES["ladder_length"]:
                problems.append(
                    f"PositionRatingPenalties has {length} entries, not "
                    f"{ROLE_WEIGHTS_RULES['ladder_length']}")
    if len(values) == len(keys) and sum(values.values()) <= 0.0:
        problems.append(
            "KeyAttributesWeight + PreferableAttributesWeight + "
            "AvoidAttributesWeight must be greater than 0")
    return problems


def validate_role_definitions(ratings: dict[str, Any] | None = None,
                              positions: tuple[str, ...] | None = None,
                              known_columns: set[str] | None = None) -> list[str]:
    """The completeness rule of the original's ``0x1407406C0``.

    ``0x1407404C0`` calls this before it looks at any weight. In the original it
    walks the position enum and, for every position, looks the value up in the
    object's ``+0x10`` dictionary; it returns 0 (reject) when a position has no
    entry, when an entry's duty list is empty, or when a duty names an attribute
    that its attribute dictionary does not resolve::

        plVar8 = ..._get_Item(*(undefined8 *)(param_1 + 0x10), uVar13);
        if (plVar8 == (longlong *)0x0) return 0;
        ...
        lVar7 = ..._get_Item(_DAT_1472d20f0 + 0x10, lVar7);
        if (lVar7 == 0) return 0;

    Returns the problems found; an empty list means the table would pass. The
    build's own ``RATINGS`` is generated from the original's file, so it passes;
    a person who edits ``rolesAndDuties.json`` and points ``FMRTE_ROLE_WEIGHTS``
    at it gets the same rejection the original would give.
    """
    table = RATINGS if ratings is None else ratings
    wanted = GENERAL_ORDER if positions is None else positions
    if known_columns is None:
        # The 62-column attribute table in spec.py: a superset of the 52 the
        # screens show, so every duty the shipped table names still resolves,
        # and a duty naming a real hidden attribute is not falsely rejected.
        # (This used to read db.ATTRIBUTE_COLUMNS through a deferred import,
        # which was one half of the old db <-> ratings cycle.)
        known_columns = set(spec.ATTRIBUTE_COLUMN_NAMES)

    problems: list[str] = []
    if not table:
        return ["the roles table is empty"]
    for position in wanted:
        roles = table.get(position)
        if not roles:
            problems.append(f"{position} has no role entry")
            continue
        for role, duties in roles.items():
            if not duties:
                problems.append(f"{position}.{role} has no duty")
                continue
            for duty, definition in duties.items():
                try:
                    key, preferable = definition
                except (TypeError, ValueError):
                    problems.append(f"{position}.{role}.{duty} is not a (key, preferable) pair")
                    continue
                if not key:
                    problems.append(f"{position}.{role}.{duty} has no key attribute")
                for column in tuple(key) + tuple(preferable):
                    if known_columns and column not in known_columns:
                        problems.append(
                            f"{position}.{role}.{duty} names an unknown attribute {column!r}")
    return problems


def role_definitions_ok() -> bool:
    """``True`` when the shipped role table passes ``0x1407406C0``'s rule."""
    return not validate_role_definitions()


def load_role_weights(path: str | None) -> dict[str, Any]:
    """Read the original's Ratings/rolesAndDuties.json into a weights object.

    The original ships the same numbers in that file and in the compiled
    defaults; reading the file means a person who changed them in the original
    gets the same rating here. A path of None (or a file that is missing or
    invalid) falls back to DEFAULT_ROLE_WEIGHTS, exactly like the original, whose
    compiled defaults are what it uses when there is no override.

    Raises ValueError when the file exists but breaks the rules above, so a
    caller can tell "no file" apart from "bad file".
    """
    import json
    import os

    if not path or not os.path.isfile(path):
        return dict(DEFAULT_ROLE_WEIGHTS) | {
            "PositionRatingPenalties": list(POSITION_RATING_PENALTIES)}
    with open(path, encoding="utf-8-sig") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("rolesAndDuties.json is not an object")
    weights: dict[str, Any] = {
        key: raw[key] for key in DEFAULT_ROLE_WEIGHTS if key in raw
    }
    weights.setdefault("CaShare", DEFAULT_ROLE_WEIGHTS["CaShare"])
    weights["PositionRatingPenalties"] = list(
        raw.get("PositionRatingPenalties") or POSITION_RATING_PENALTIES)
    problems = validate_role_weights(weights)
    if problems:
        raise ValueError("; ".join(problems))
    return weights


def role_weights() -> dict[str, Any]:
    """The weights in force: the original's file if pointed at, else defaults."""
    global _ROLE_WEIGHTS_CACHE, ROLE_DEFINITION_PROBLEMS
    if _ROLE_WEIGHTS_CACHE is None:
        import os

        path = os.environ.get("FMRTE_ROLE_WEIGHTS")
        try:
            _ROLE_WEIGHTS_CACHE = loadsafe(path)
        except ValueError:
            _ROLE_WEIGHTS_CACHE = load_role_weights(None)
        # The original builds the roles object before it reads any weight
        # (0x140740340 calls 0x140741FA0 first) and 0x1407404C0 rejects it unless
        # 0x1407406C0 passes. Run the same completeness rule over the table the
        # rating is about to use, and keep the answer so a page or a test can
        # report it instead of the failure being silent.
        ROLE_DEFINITION_PROBLEMS = tuple(validate_role_definitions())
    return _ROLE_WEIGHTS_CACHE


def loadsafe(path: str | None) -> dict[str, Any]:
    """load_role_weights, but a malformed file falls back instead of raising."""
    try:
        return load_role_weights(path)
    except (OSError, ValueError):
        return load_role_weights(None)


def set_role_weights(weights: dict[str, Any] | None) -> None:
    """Pin the weights used from now on (pass None to go back to the file)."""
    global _ROLE_WEIGHTS_CACHE
    if weights is None:
        _ROLE_WEIGHTS_CACHE = None
        return
    problems = validate_role_weights(weights)
    if problems:
        raise ValueError("; ".join(problems))
    _ROLE_WEIGHTS_CACHE = dict(weights)


def key_weight() -> float:
    return float(role_weights()["KeyAttributesWeight"])


def preferable_weight() -> float:
    return float(role_weights()["PreferableAttributesWeight"])


def avoid_weight() -> float:
    return float(role_weights()["AvoidAttributesWeight"])


def ca_share() -> float:
    return float(role_weights()["CaShare"])

# `CA in ratings` — how much of Actual Rating and Potential Rating comes from
# the current ability; the rest comes from the player's best role. The original's
# own UI text (Assets/Languages/english.json) states the default:
#
#   Settings.Ratings.RoleWeights.CaShare      = "CA in ratings"
#   Settings.Ratings.RoleWeights.CaShare.Help = "How much CA counts in Actual
#       Rating and Potential Rating. The rest comes from the player's best role.
#       Default: 50%."
#
# The compiled 0.5 at 0x14074042C agrees with that text. Two independent
# sources, so this is observed, not guessed.
CA_SHARE = 0.5

# --- the value ranges the original's own code enforces --------------------
# Read out of the original's machine code, not its settings files. These are the
# maximums the original's own property setters are called with, each found as
# `mov r8d, <max>` immediately followed by `xor r9d, r9d` (minimum 0) and a call:
#
#   0x1411782B8  mov r8d, 0x14    -> 20      attribute
#   0x1411D0A48  mov r8d, 0xC8    -> 200     current / potential ability
#   0x14102D4E0  mov r8d, 0x2710  -> 10000   condition / sharpness / reputation
#
# Honest correction (MECHANICS.md section 13.5): sub_1404446B0 (0x1404446B0) is
# a virtual-dispatch wrapper, not a clamp. Its body just forwards four arguments
# through a function pointer. The ranges below are still the ones the original's
# setters are called with, but they are observed from those call sites, not
# proven by a clamp helper.
#
# The numbers themselves are declared once, in spec.py, together with every
# edit range and the clamp that uses them; these names are re-exported because
# the tool layer and the tests import them from here.
ATTRIBUTE_MAX = spec.ATTRIBUTE_MAX
ABILITY_MAX = spec.ABILITY_MAX
CONDITION_MAX = spec.CONDITION_MAX

# The original stores a rating as a single-precision ratio and then truncates
# it to two decimals for display. That is why Pulisic's 124/160 shows as
# 77.49 % and not 77.50 %: 124/160 as a float32 is 0.7749999761581421, and
# 0.7749999761581421 * 100 = 77.49999761581421, truncated to 77.49.
RATING_DECIMALS = 2

# Out-of-position penalty ladder, in order (observed, 20 entries).
#
# The data lives at 0x14517BB38 in .rdata, 80 bytes = 20 x int32, and
# sub_140741F50 at 0x140741F50 builds it into an int[20] at start-up. Its
# disassembly is 14 instructions:
#
#   lea rcx, [0x14619ACD0]      ; the int[] type
#   mov edx, 0x14               ; 20 elements
#   call 0x143441F00            ; allocate
#   lea rcx, [rbx+0x10]
#   lea rdx, [0x14517BB38]      ; the ladder
#   mov r8d, 0x50               ; 80 bytes
#   call 0x14215CBC0            ; copy
#
# native_bytes_show on 0x14517BB38 reads the 20 values back exactly as below.
PENALTY_LADDER_DATA = "0x14517BB38"
PENALTY_LADDER_BUILDER = "0x140741F50"
# 0x1407404C0 reads the settings object at +0x18 and rejects it unless its
# element count is 0x14; 0x140741F50 allocates exactly 0x14 ints and copies
# 0x50 bytes. Code and data agree, so the ladder's length is fixed at 20.
PENALTY_LADDER_LENGTH = 20
POSITION_RATING_PENALTIES = (
    76, 72, 68, 64, 60, 56, 52, 48, 44, 40, 36, 20, 17, 15, 5, 4, 3, 2, 1, 0,
)

# --- constants observed in Ratings/teamSelection.json ---------------------
# These are ALSO compiled into the original as int32 immediates: sub_1407417F0
# at 0x1407417F0 packs them into one object, eight instructions in total. The
# disassembly (native_function_disassemble) reads:
#
#   mov rax, 0x5000000096 ; mov [rcx+0x10], rax    -> +0x10 = 150, +0x14 = 80
#   mov rax, 0x5A00000078 ; mov [rcx+0x18], rax    -> +0x18 = 120, +0x1C = 90
#   mov rax, 0x2300000050 ; mov [rcx+0x20], rax    -> +0x20 = 80,  +0x24 = 35
#   mov dword [rcx+0x28], 0xF                      -> +0x28 = 15
#
# so the JSON order, the field order and the compiled values all agree.
TEAM_SELECTION_WEIGHTS_BUILDER = "0x1407417F0"
TEAM_SELECTION_WEIGHTS_FIELDS = {
    "PositionAbilityWeight": 0x10,
    "RoleWeight": 0x14,
    "CurrentAbilityWeight": 0x18,
    "PotentialAbilityWeight": 0x1C,
    "FitnessWeight": 0x20,
    "ReputationWeight": 0x24,
    "MoraleWeight": 0x28,
}

TEAM_SELECTION_WEIGHTS = {
    "PositionAbilityWeight": 150,
    "RoleWeight": 80,
    "CurrentAbilityWeight": 120,
    "PotentialAbilityWeight": 90,
    "FitnessWeight": 80,
    "ReputationWeight": 35,
    "MoraleWeight": 15,
}

# The general position each displayed position maps to. The role itself is no
# longer guessed from this: the original rates every role and shows the best
# one (see best_role below).  Derived from spec.POSITIONS, so the labels, the
# columns and the role-table groups cannot drift apart.
POSITION_TO_GENERAL = spec.LABEL_TO_GENERAL

# Which snapshot columns hold a player's competence in each general position.
# The original picks the role from the player's *best* position; the snapshot
# stores one 1..20 competence per position (players table), so the general
# position with the highest competence is the one the original shows.
# Verified: Pulisic's competences are AMR 20, AMC 18, ST 17, AML 15 ... and the
# original's page reads "Wide Forward (IP)" — i.e. AMR -> Winger -> WideForward.
# Grouped from the one position table in spec, so a position cannot be missing.
GENERAL_TO_COMPETENCE: dict[str, tuple[str, ...]] = spec.GENERAL_TO_COMPETENCE

# The order the original's own position list uses (config.json ColumnsSettings
# "Positions.*"), used only to break ties between equally good positions.
GENERAL_ORDER = spec.GENERAL_ORDER

# The original's config.json sets HighlightBestRoleDutyAutomatically: true, so
# it does not use a fixed role per position. It scores EVERY role the player's
# best position offers and shows the highest one. That rule is what reproduces
# the original's own header: for Pulisic the eight Winger roles score 76.87,
# 71.66, 74.28, 73.18, 67.85, 77.49, 71.66 and 73.57, and the best is
# WideForward / InPossession at 77.49 - exactly the "Wide Forward (IP) 77.49 %"
# the original displays. A fixed role per position cannot do that.
#
# The role names below are the original's own (generated into
# role_definitions.py); the old build invented names like "CentralDefender"
# and "AdvancedForward" that do not exist in the original's file.
DEFAULT_ROLE = {
    "Goalkeeper": "Goalkeeper",
    "DefenderCentral": "CentreBack",
    "FullBack": "FullBack",
    "WingBack": "WingBack",
    "DefensiveMidfielder": "DefensiveMidfielder",
    "MidfielderCentral": "CentralMidfielder",
    "AttackingMidfielderCentral": "AttackingMidfielder",
    "Winger": "WideForward",
    "Striker": "CentreForward",
}

def _humanise(role: str) -> str:
    """Turn a role key from the original's file into its display name.

    The original's keys are PascalCase ("WideForward", "BallPlayingCentreBack")
    and its UI shows them spaced ("Wide Forward"). This splits on the case
    boundaries, which reproduces every label the original shows.
    """
    out: list[str] = []
    for index, char in enumerate(role):
        if index and char.isupper() and not role[index - 1].isupper():
            out.append(" ")
        out.append(char)
    return "".join(out)


# Display names for the roles the original's file defines. Anything not listed
# falls back to _humanise(), so a new role still reads sensibly.
ROLE_LABEL = {
    "WideForward": "Wide Forward",
    "Winger": "Winger",
    "InsideForward": "Inside Forward",
    "InsideWinger": "Inside Winger",
    "InsideOutletWinger": "Inside Outlet Winger",
    "PlaymakingWinger": "Playmaking Winger",
    "TrackingWinger": "Tracking Winger",
    "WideOutletWinger": "Wide Outlet Winger",
    "AttackingMidfielder": "Attacking Midfielder",
    "CentralMidfielder": "Central Midfielder",
    "DefensiveMidfielder": "Defensive Midfielder",
    "CentreBack": "Centre Back",
    "FullBack": "Full Back",
    "WingBack": "Wing Back",
    "Goalkeeper": "Goalkeeper",
    "CentreForward": "Centre Forward",
}


def _value(player: dict[str, Any], column: str) -> int:
    """Read one attribute, clamped to the 0..20 the original enforces."""
    try:
        raw = player.get(column)
        if raw is None:
            return 0
        return max(0, min(ATTRIBUTE_MAX, int(raw)))
    except (TypeError, ValueError):
        return 0


def _f32(value: float) -> float:
    """Round a number to single precision, the way the original stores it."""
    return struct.unpack("f", struct.pack("f", value))[0]


def as_displayed(ratio: float) -> float:
    """Turn a 0..1 ratio into the percentage the original's screen shows.

    The original keeps the ratio in a float32 and truncates the percentage to
    two decimals. Both steps matter: Pulisic's blended ratio is exactly 0.775
    in double precision, but as a float32 it is 0.7749999761581421, so the
    screen reads 77.49 % rather than 77.50 %. Verified against the original's
    own header for Pulisic ("Rating cur / pot 77.49 % / 77.49 %").
    """
    percent = _f32(ratio) * 100.0
    scale = 10 ** RATING_DECIMALS
    # Truncate, do not round: 77.4999976 must become 77.49, not 77.50.
    return math.floor(percent * scale) / scale


def _competence(player: dict[str, Any], general: str) -> int:
    """The player's best competence (1..20) among a general position's columns."""
    best = 0
    for column in GENERAL_TO_COMPETENCE.get(general, ()):
        try:
            value = int(player.get(column) or 0)
        except (TypeError, ValueError):
            value = 0
        best = max(best, value)
    return best


def best_general_position(player: dict[str, Any]) -> str:
    """The general position the original rates the player in.

    The original shows the player's *best* role ("HighlightBestRoleDutyAutomatically"
    is true in its config.json), so this is the general position with the highest
    stored competence. Ties fall back to the original's own position order.
    """
    scored = [(_competence(player, g), -GENERAL_ORDER.index(g), g)
              for g in GENERAL_ORDER]
    scored.sort(reverse=True)
    best_score, _rank, best = scored[0]
    if best_score <= 0:
        # No competence data at all: fall back to the displayed position text.
        # spec owns that helper, so this module never imports db (that import
        # was one half of the old db <-> ratings cycle).
        text = spec.position_text(player)
        primary = text.split("/")[0] if text and text != "—" else ""
        return POSITION_TO_GENERAL.get(primary, "MidfielderCentral")
    return best


def _role_score(player: dict[str, Any], key: tuple[str, ...]) -> float:
    """The raw 0..1 key-attribute ratio for one role/duty, before display."""
    if not key:
        return 0.0
    total = sum(_value(player, column) for column in key)
    return total / (len(key) * ATTRIBUTE_MAX)


def _weighted_definition_score(player: dict[str, Any],
                               key: tuple[str, ...],
                               preferable: tuple[str, ...]) -> float:
    """One role/duty scored at the original's 16 / 4 key/preferable weights.

    The weights come from role_weights(), so the original's own
    Ratings/rolesAndDuties.json (when FMRTE_ROLE_WEIGHTS points at it) changes
    the figure exactly as it changes the original's. With no file they are the
    compiled defaults 16 and 4.
    """
    if not key and not preferable:
        return 0.0
    kw = key_weight()
    pw = preferable_weight()
    key_sum = sum(_value(player, column) for column in key)
    pref_sum = sum(_value(player, column) for column in preferable)
    maximum = (len(key) * kw + len(preferable) * pw)
    if not maximum:
        return 0.0
    earned = (key_sum / ATTRIBUTE_MAX * kw + pref_sum / ATTRIBUTE_MAX * pw)
    return earned / maximum


def best_role(player: dict[str, Any]) -> tuple[str, str, str]:
    """The (general position, role, duty) the original highlights.

    The original sets HighlightBestRoleDutyAutomatically: true, so it scores
    every role and duty its best position offers and shows the highest. The
    role is chosen on its key-attribute share, which is the figure the role
    panel labels "Key"; the header then blends that role's 16/4 weighted score
    with CA (see role_rating). Choosing the role on the weighted score instead
    would change 14,244 of the save's 42,344 best roles without any evidence
    beyond Pulisic, whose role is the same either way, so the key rule stands.

    Verified against the original's own screen: Pulisic's Winger roles score
    highest at WideForward / InPossession (77.50 % key, 76.00 % weighted), the
    "Wide Forward (IP)" the original displays. Ties fall back to the original's
    own position order, then to the role order in its file.
    """
    general = best_general_position(player)
    roles = RATINGS.get(general) or {}
    best: tuple[float, str, str] | None = None
    for role, duties in roles.items():
        for duty, (key, _preferable) in duties.items():
            score = _role_score(player, key)
            if best is None or score > best[0]:
                best = (score, role, duty)
    if best is None:
        return general, DEFAULT_ROLE.get(general, "CentralMidfielder"), "InPossession"
    return general, best[1], best[2]


def role_for(player: dict[str, Any]) -> tuple[str, str, str]:
    """Return (general position, role, duty) for a player."""
    return best_role(player)


def role_score(player: dict[str, Any]) -> float:
    """The best role's own score, before any CA is mixed in.

    The original's own settings help says the rating is CA plus "the rest ...
    from the player's best role". The role part is the weighted 16/4 score over
    the role's key and preferable attributes (see weighted_rating), returned as
    the raw 0..1 ratio, not the float32 display value.
    """
    general, role, duty = best_role(player)
    definition = RATINGS.get(general, {}).get(role, {}).get(duty)
    if not definition:
        return 0.0
    return _weighted_definition_score(player, definition[0], definition[1])


def role_rating(player: dict[str, Any]) -> float:
    """The original's "Rating cur / pot", as the original displays it.

    The original's own settings help (Assets/Languages/english.json) says:

        "How much CA counts in Actual Rating and Potential Rating. The rest
         comes from the player's best role. Default: 50%."

    so this is the CA share blended with the best role's weighted score, not a
    plain key-attribute average:

        ratio = CA_SHARE * (CA / 200) + (1 - CA_SHARE) * role_score

    For Pulisic both readings happen to meet: CA 158/200 = 0.79 and the
    16/4-weighted role score is 0.76, so the blend is exactly 0.775 and the
    float32/truncate step shows 77.49 % exactly as the original's header does.
    The old key-only reading also gave 0.775 for him, which is why the
    coincidence hid this term; other players separate the two.
    """
    try:
        ca = int(player.get("ca") or 0)
    except (TypeError, ValueError):
        ca = 0
    ca_ratio = max(0.0, min(1.0, ca / ABILITY_MAX))
    share = ca_share()
    ratio = share * ca_ratio + (1.0 - share) * role_score(player)
    return as_displayed(ratio)


def key_rating(player: dict[str, Any]) -> float:
    """The role's key attributes alone, which the role panel labels "Key"."""
    general, role, duty = role_for(player)
    definition = RATINGS.get(general, {}).get(role, {}).get(duty)
    if not definition or not definition[0]:
        return 0.0
    key = definition[0]
    total = sum(_value(player, column) for column in key)
    return as_displayed(total / (len(key) * ATTRIBUTE_MAX))


def preferable_rating(player: dict[str, Any]) -> float:
    """The preferable-attribute share, shown as the role panel's second figure."""
    general, role, duty = role_for(player)
    definition = RATINGS.get(general, {}).get(role, {}).get(duty)
    if not definition:
        return 0.0
    _key, preferable = definition
    if not preferable:
        return 0.0
    total = sum(_value(player, column) for column in preferable)
    return as_displayed(total / (len(preferable) * ATTRIBUTE_MAX))


def weighted_rating(player: dict[str, Any]) -> float:
    """Key and preferable attributes combined at the original's 16 / 4 weights.

    This is the role-only half of the header rating (Pulisic: 76.00 % before
    the float32 step, shown as 75.99 %). The header itself blends this with CA
    at CA_SHARE, so this figure is kept for the role panel and for checking.
    """
    return as_displayed(role_score(player))


def position_penalty(player: dict[str, Any], position: str) -> int:
    """The out-of-position penalty the original applies, in percent.

    The ladder is real code data: the 20 int32 values live at 0x14517BB38 in
    .rdata and sub_140741F50 (0x140741F50) builds them into an int[20] at
    startup (allocator called with count 0x14, then 0x50 bytes copied).

    The ladder is indexed by how far the player's competence for `position`
    falls below his best competence. It is a *penalty*, so it reads from the
    far end: a player in his best position takes the last entry (0) and a
    player with no competence there takes the first (76). The index is clamped
    to the ladder's 20 entries.
    """
    if len(POSITION_RATING_PENALTIES) != PENALTY_LADDER_LENGTH:
        return 0
    best = max((_competence(player, g) for g in GENERAL_ORDER), default=0)
    here = _competence(player, position)
    if best <= 0:
        return POSITION_RATING_PENALTIES[0]
    drop = max(0, best - here)
    last = PENALTY_LADDER_LENGTH - 1
    index = last - min(drop, last)
    return POSITION_RATING_PENALTIES[index]


def role_label(player: dict[str, Any]) -> str:
    _general, role, _duty = role_for(player)
    return ROLE_LABEL.get(role) or _humanise(role)


# --- the ranges the original's own code enforces on every edit ------------
# Declared once in spec.py (RANGES for the snapshot/screen domain, LIVE_RANGES
# for the live-memory domain, and the one clamp both use).  This name is kept
# because the player page reads it for the four bottom-strip fields and the
# tests import it.
EDIT_RANGES: dict[str, tuple[int, int]] = spec.RANGES

# Every player attribute column the original clamps to 0..20.  The wider
# 62-column table lives in spec.py; it is a superset of the 52 the screens
# show, because the preferred-foot and personality columns are editable too.
ATTRIBUTE_COLUMNS = spec.ATTRIBUTE_COLUMN_NAMES


def clamp(column: str, value: int) -> int:
    """Clamp an edited value the way the original's own code does.

    The original routes every editable player value through one helper that
    takes (value, minimum, maximum, ...) and clamps to that range. The
    maximums it is called with are 20 for attributes, 200 for ability and
    10000 for condition, sharpness and reputation (see the constants above).

    The one implementation is spec.clamp; this wrapper exists because callers
    and tests import ``ratings.clamp``.
    """
    return spec.clamp(column, value)


def key_attribute_columns(player: dict[str, Any]) -> tuple[str, ...]:
    general, role, duty = role_for(player)
    definition = RATINGS.get(general, {}).get(role, {}).get(duty)
    return definition[0] if definition else ()


def preferable_attribute_columns(player: dict[str, Any]) -> tuple[str, ...]:
    general, role, duty = role_for(player)
    definition = RATINGS.get(general, {}).get(role, {}).get(duty)
    return definition[1] if definition else ()
