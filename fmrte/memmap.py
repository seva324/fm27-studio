"""The memory map the live editor uses, and where each number was read.

Everything here was observed in the person's own working reader
(``fm-ai-assistant-2.1.0-SNAPSHOT-desktop.jar``), which reads this exact save
from the running game:

* ``com.github.fmaiassistant.memory.PlayerRecordLayout#current()`` gives the
  player layout.
* ``com.github.fmaiassistant.player.AttributeDefinitions`` gives the attribute
  block and every attribute's byte offset.
* ``com.github.fmaiassistant.linux.FmOffsets`` gives the offset table slots.

``MECHANICS.md`` records the class and method each group came from. The short
version:

    person pointer P (from the People vector)
      P - 36   current ability   (i16)
      P - 34   potential ability (i16)
      P - 44   condition         (u16)
      P - 48   match sharpness   (u16)
      P - 46   jadedness         (i16)
      P - 28   morale            (u8)
      P - 42 / -40 / -38  home / current / world reputation (u16)
      P - 84   asking price raw  (u32)
      P - 90   height in cm      (u8)
      P + 12   unique id         (u32)
      P + 168  registration pointer (u64)
      P - 314  start of the attribute block
    attributes are indexed from that block at (field offset - 40)

The attribute block is the important one for the person's complaint: it is
where the Technical / Mental / Physical numbers live, so they can be written
back into the game.
"""

from __future__ import annotations

from . import spec

# -- identity ---------------------------------------------------------------
PERSON_INDEX_REL = 8
PERSON_UNIQUE_ID_REL = 12
PERSON_PREFIX_BYTES = 16

SUBTYPE_STAFF = 256
SUBTYPE_PLAYER = 648
SUBTYPE_PLAYER_STAFF = 896
SUBTYPE_HUMAN_MANAGER = 1088

# Player/staff records sit this far before a plain player record.
PLAYER_STAFF_SHIFT = 248

# -- direct player fields ---------------------------------------------------
# column -> (offset from the person pointer, struct format)
DIRECT_FIELDS: dict[str, tuple[int, str]] = {
    "ca": (-36, "<h"),
    "pa": (-34, "<h"),
    "condition": (-44, "<H"),
    "match_sharpness": (-48, "<H"),
    "jadedness": (-46, "<h"),
    "morale": (-28, "<B"),
    "home_reputation": (-42, "<H"),
    "current_reputation": (-40, "<H"),
    "world_reputation": (-38, "<H"),
    "asking_price_raw": (-84, "<I"),
    "height_cm": (-90, "<B"),
    "unique_id": (12, "<I"),
}

# Fields read at a fixed positive offset from the person pointer (no block).
DIRECT_POSITIVE_FIELDS: dict[str, tuple[int, str]] = {
    "adaptability": (112, "<B"),
    "ambition": (113, "<B"),
    "loyalty": (114, "<B"),
    "pressure": (115, "<B"),
    "professionalism": (116, "<B"),
    "sportsmanship": (117, "<B"),
    "temperament": (118, "<B"),
    "controversy": (119, "<B"),
}

# -- attribute block --------------------------------------------------------
ATTRIBUTE_BLOCK_REL = -314
ATTRIBUTE_INDEX_BASE = 40

# The original stores visible attributes as 0..100 and shows them as 1..20:
#   norm20(v) = clamp(round(v * 20 / 100), 1, 20)
# Positions and the hidden personality attributes are stored 1..20 directly.
# The value itself is the single authority's (spec.py); this name is kept so a
# caller that already imports it from memmap keeps working.
ATTRIBUTE_STORAGE_SCALE = spec.ATTRIBUTE_STORAGE_SCALE

# column -> FieldDef offset (AttributeDefinitions)
ATTRIBUTE_OFFSETS: dict[str, int] = {
    # positions (stored 1..20)
    "goalkeeper": 42,
    "defender_left": 44,
    "defender_central": 45,
    "defender_right": 46,
    "defensive_midfielder": 47,
    "midfielder_left": 48,
    "midfielder_central": 49,
    "midfielder_right": 50,
    "attacking_midfielder_left": 51,
    "attacking_midfielder_central": 52,
    "attacking_midfielder_right": 53,
    "striker": 54,
    "wing_back_left": 55,
    "wing_back_right": 56,
    # visible attributes (stored 0..100)
    "crossing": 57,
    "dribbling": 58,
    "finishing": 59,
    "heading": 60,
    "long_shots": 61,
    "marking": 62,
    "off_the_ball": 63,
    "passing": 64,
    "penalties": 65,
    "tackling": 66,
    "vision": 67,
    "handling": 68,
    "aerial_ability": 69,
    "command_of_area": 70,
    "communication": 71,
    "kicking": 72,
    "throwing": 73,
    "anticipation": 74,
    "decisions": 75,
    "one_on_ones": 76,
    "positioning": 77,
    "reflexes": 78,
    "first_touch": 79,
    "technique": 80,
    "left_foot": 81,
    "right_foot": 82,
    "flair": 83,
    "corners": 84,
    "teamwork": 85,
    "work_rate": 86,
    "long_throws": 87,
    "eccentricity": 88,
    "rushing_out": 89,
    "tendency_to_punch": 90,
    "acceleration": 91,
    "free_kicks": 92,
    "strength": 93,
    "stamina": 94,
    "pace": 95,
    "jumping_reach": 96,
    "leadership": 97,
    "dirtiness": 98,
    "balance": 99,
    "bravery": 100,
    "consistency": 101,
    "aggression": 102,
    "agility": 103,
    "important_matches": 104,
    "injury_proneness": 105,
    "versatility": 106,
    "natural_fitness": 107,
    "determination": 108,
    "composure": 109,
    "concentration": 110,
}

# The 14 position columns, from the single authority in spec.py.  A set: the
# live writer only ever asks "is this column a position" (positions are stored
# 1..20 and are not scaled, see ATTRIBUTE_STORAGE_SCALE above).
POSITION_COLUMNS = spec.POSITION_COLUMNS

# -- injury -----------------------------------------------------------------
# The player record holds a pointer to an injury container; the container's
# first qword points at the injury item. Every offset below is from
# ``PlayerRecordLayout`` (FM AI Assistant 2.1.0), which reads this exact save:
#
#   Direct.injuryReferenceRel   -0x190   (the pointer, relative to the person)
#   Injury.typeRel              +0x08    (pointer to the injury type object)
#   Injury.typeNameRel          +0x20    (the type object's name string)
#   Injury.dayRel               +0x20    (u16 day of year, masked with 511)
#   Injury.yearRel              +0x22    (u16 year)
#   Injury.fullTrainingRel      +0x28    (u16 total days until full training)
#   Injury.lightTrainingRel     +0x2A    (u16 total days until light training)
#
# ``PlayerExporter.injuryStatus`` decides "injured" by the pointer alone: a
# non-zero reference whose first qword is readable. That is why writing the
# condition/sharpness/jadedness numbers never healed anybody - the injury
# record itself is what the game and the editor both read.
INJURY_REFERENCE_REL = -0x190
INJURY_TYPE_REL = 0x08
INJURY_TYPE_NAME_REL = 0x20
INJURY_DAY_REL = 0x20
INJURY_YEAR_REL = 0x22
INJURY_FULL_TRAINING_REL = 0x28
INJURY_LIGHT_TRAINING_REL = 0x2A

# -- registration (contract) ------------------------------------------------
REGISTRATION_POINTER_REL = 168
SALARY_WEEKLY_REL = 32
CONTRACT_DATE_REL = 72
JOINED_DATE_REL = 76
TRANSFER_STATUS_REL = 87

# Dates are two u16: the day of the year (1..366, masked with 511) and the year.
DATE_DAY_MASK = 511

# -- offset table (FmOffsets) ----------------------------------------------
# The single authority for the eleven offset-table slots.  ``fmmemory.SLOTS``
# is a re-export of this object, not a second copy: the scanner, the people
# vector and the live writer must not be able to disagree about where a slot
# is, and two literals would drift the first time one of them was updated.
SLOTS = {
    "CityOffset": 240,
    "ClubOffset": 248,
    "CompetitionOffset": 256,
    "ContinentOffset": 264,
    "NationOffset": 272,
    "CurrencyOffset": 280,
    "PeopleOffset": 336,
    "RegionOffset": 352,
    "StadiumOffset": 360,
    "TeamOffset": 384,
    "AgreementOffset": 416,
}
