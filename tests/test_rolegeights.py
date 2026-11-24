"""The role-weights object, read out of FMRTE.exe instruction by instruction.

Everything here is checked against the original's own machine code, whose
addresses are asserted so that a future edit cannot quietly re-label a field:

    0x140740090  movsd xmm0,[rcx+0x20]   getter of KeyAttributesWeight
    0x1407400A0  movsd [rcx+0x20],xmm1   setter
    0x1407400B0/0C0                      PreferableAttributesWeight (+0x28)
    0x1407400D0/0E0                      AvoidAttributesWeight     (+0x30)
    0x1407400F0  mov rax,[rcx+0x18]      getter of PositionRatingPenalties
    0x140740100  lea rcx,[rcx+0x18]      setter of the same
    0x140740110/120                      CaShare                    (+0x38)

The two collection slots were the one thing the earlier sheet had wrong: it
called +0x10 the penalty ladder and +0x18 the role map. The getter at
0x1407400F0 reads +0x18 for the ladder, and the validator at 0x1407404C0
requires the array at +0x18 to hold 0x14 (20) entries, so +0x18 is the ladder.

Run from app/ with:  .venv\\Scripts\\python.exe -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

from fmrte import ratings  # noqa: E402
from fmrte.db import Snapshot  # noqa: E402

PULISIC_UID = 92074020

# Where the original's own Ratings/rolesAndDuties.json may be. The build never
# needs it (the same numbers are compiled in as defaults), so a machine without
# FMRTE just skips the file tests. Set FMRTE_ORIGINAL_DIR to point at the
# folder that holds FMRTE.exe.
ORIGINAL_HINTS = (
    os.environ.get("FMRTE_ORIGINAL_DIR"),
    r"C:\Program Files\FMRTE",
)


def original_role_file() -> str | None:
    for folder in ORIGINAL_HINTS:
        if not folder:
            continue
        for candidate in (os.path.join(folder, "Ratings", "rolesAndDuties.json"),
                          os.path.join(folder, "current", "Ratings",
                                       "rolesAndDuties.json")):
            if os.path.exists(candidate):
                return candidate
    return None


class RoleWeightsLayoutTests(unittest.TestCase):
    """The field map, as the accessors prove it."""

    def test_field_offsets(self):
        fields = ratings.ROLE_WEIGHTS_OBJECT["fields"]
        self.assertEqual(
            fields["GeneralPositionToRoleAndDutiesDefinitionsMap"], 0x10)
        self.assertEqual(fields["PositionRatingPenalties"], 0x18)
        self.assertEqual(fields["KeyAttributesWeight"], 0x20)
        self.assertEqual(fields["PreferableAttributesWeight"], 0x28)
        self.assertEqual(fields["AvoidAttributesWeight"], 0x30)
        self.assertEqual(fields["CaShare"], 0x38)

    def test_every_accessor_is_recorded_with_its_instruction(self):
        for name, row in ratings.ROLE_WEIGHTS_ACCESSORS.items():
            self.assertTrue(row["getter"].startswith("0x14074"), name)
            self.assertTrue(row["setter"].startswith("0x14074"), name)
            self.assertIn("rcx", row["getter_op"], name)

    def test_the_ladder_getter_reads_0x18(self):
        self.assertEqual(
            ratings.ROLE_WEIGHTS_ACCESSORS["PositionRatingPenalties"]["getter_op"],
            "mov rax,[rcx+0x18]")

    def test_compiled_defaults(self):
        self.assertEqual(ratings.DEFAULT_ROLE_WEIGHTS, {
            "KeyAttributesWeight": 16.0,
            "PreferableAttributesWeight": 4.0,
            "AvoidAttributesWeight": 3.0,
            "CaShare": 0.5,
        })

    def test_the_two_code_paths_are_recorded(self):
        obj = ratings.ROLE_WEIGHTS_OBJECT
        self.assertEqual(obj["constructor"], "0x140740130")
        self.assertEqual(obj["builder_dispatch"], "0x140740380")
        self.assertEqual(len(obj["jump_table_bases"]), 3)
        self.assertEqual(obj["jump_base"], "0x14074034B")

    def test_validation_rules_are_documented_with_their_addresses(self):
        rules = ratings.ROLE_WEIGHTS_RULES
        self.assertEqual(rules["validator"], "0x1407404C0")
        self.assertEqual(rules["roles_completeness_validator"], "0x1407406C0")
        self.assertEqual(rules["group_validator"], "0x140741DE0")
        self.assertEqual(rules["ladder_length"], 20)
        self.assertEqual(rules["group_total"], 100)
        self.assertFalse(rules["ca_share_validated"])

    def test_the_scout_type_map_is_recorded(self):
        # The original's own names, read out of .rdata. They are what says the
        # rating has a player-position rate AND a general-position rate.
        names = {
            "PlayerRating": "0x143775823",
            "PositionRoleAndDutiesRate": "0x14377588b",
            "PositionRoleRates": "0x1437758f2",
            "BestFormationsCalculator": "0x1437756ea",
            "GetPlayerPositionRate": "0x143877b51",
        }
        for name, address in names.items():
            self.assertEqual(ratings.SCOUT_TYPE_MAP[name], address, name)


class RoleDefinitionsCompletenessTests(unittest.TestCase):
    """The rule 0x1407404C0 calls at 0x1407406C0 before it reads any weight.

    0x1407406C0 walks the position enum, looks each value up in the object's
    +0x10 dictionary, and returns 0 when a position has no entry or when a duty
    names an attribute its attribute dictionary does not resolve.
    """

    def test_the_shipped_role_table_is_complete(self):
        self.assertEqual(ratings.validate_role_definitions(), [])

    def test_the_runtime_check_passed(self):
        ratings.role_weights()
        self.assertEqual(ratings.ROLE_DEFINITION_PROBLEMS, ())
        self.assertTrue(ratings.role_definitions_ok())

    def test_a_missing_position_is_rejected(self):
        table = dict(ratings.RATINGS)
        table.pop("Striker", None)
        problems = ratings.validate_role_definitions(table)
        self.assertTrue(any("Striker has no role entry" in p for p in problems),
                        problems)

    def test_a_duty_with_an_unknown_attribute_is_rejected(self):
        table = {
            "Striker": {
                "Poacher": {
                    "InPossession": (("not_a_real_column",), ()),
                },
            },
        }
        problems = ratings.validate_role_definitions(
            table, positions=("Striker",),
            known_columns={"finishing", "composure"})
        self.assertTrue(any("unknown attribute" in p for p in problems), problems)

    def test_a_duty_with_no_key_attribute_is_rejected(self):
        table = {"Striker": {"Poacher": {"InPossession": ((), ("finishing",))}}}
        problems = ratings.validate_role_definitions(
            table, positions=("Striker",), known_columns={"finishing"})
        self.assertTrue(any("no key attribute" in p for p in problems), problems)


class RoleWeightsValidationTests(unittest.TestCase):
    """The six conditions 0x1407404C0 tests."""

    def defaults(self, **overrides):
        obj = dict(ratings.DEFAULT_ROLE_WEIGHTS)
        obj["PositionRatingPenalties"] = list(ratings.POSITION_RATING_PENALTIES)
        obj.update(overrides)
        return obj

    def test_the_defaults_are_valid(self):
        self.assertEqual(ratings.validate_role_weights(self.defaults()), [])

    def test_a_short_ladder_is_rejected(self):
        obj = self.defaults()
        obj["PositionRatingPenalties"] = obj["PositionRatingPenalties"][:19]
        problems = ratings.validate_role_weights(obj)
        self.assertTrue(any("19 entries" in p for p in problems), problems)

    def test_a_negative_weight_is_rejected(self):
        problems = ratings.validate_role_weights(
            self.defaults(AvoidAttributesWeight=-3.0))
        self.assertTrue(any("AvoidAttributesWeight is negative" in p
                            for p in problems), problems)

    def test_three_zero_weights_are_rejected(self):
        problems = ratings.validate_role_weights(self.defaults(
            KeyAttributesWeight=0.0,
            PreferableAttributesWeight=0.0,
            AvoidAttributesWeight=0.0))
        self.assertTrue(any("greater than 0" in p for p in problems), problems)

    def test_a_single_zero_weight_is_allowed(self):
        obj = self.defaults(AvoidAttributesWeight=0.0)
        self.assertEqual(ratings.validate_role_weights(obj), [])

    def test_ca_share_is_not_validated(self):
        # The original's validator never looks at +0x38, so an out-of-range
        # CA share is "valid" there and must be accepted here too.
        self.assertEqual(
            ratings.validate_role_weights(self.defaults(CaShare=2.5)), [])

    def test_a_missing_ladder_is_rejected(self):
        obj = dict(ratings.DEFAULT_ROLE_WEIGHTS)
        problems = ratings.validate_role_weights(obj)
        self.assertTrue(any("PositionRatingPenalties is missing" in p
                            for p in problems), problems)


class RoleWeightsFileTests(unittest.TestCase):
    """Reading the original's own Ratings/rolesAndDuties.json."""

    def tearDown(self):
        ratings.set_role_weights(None)

    def test_no_path_falls_back_to_the_defaults(self):
        w = ratings.load_role_weights(None)
        self.assertEqual(w["KeyAttributesWeight"], 16.0)
        self.assertEqual(len(w["PositionRatingPenalties"]), 20)

    def test_a_missing_file_falls_back(self):
        w = ratings.load_role_weights(os.path.join(APP, "no-such-file.json"))
        self.assertEqual(w["CaShare"], 0.5)

    def test_the_originals_file_loads(self):
        path = original_role_file()
        if not path:
            self.skipTest("original FMRTE installation not present")
        w = ratings.load_role_weights(path)
        self.assertEqual(w["KeyAttributesWeight"], 16)
        self.assertEqual(w["PreferableAttributesWeight"], 4)
        self.assertEqual(w["AvoidAttributesWeight"], 3)
        self.assertEqual(w["CaShare"], 0.5)
        self.assertEqual(len(w["PositionRatingPenalties"]), 20)

    def test_a_bad_file_raises(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "rolesAndDuties.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"KeyAttributesWeight": -1}, handle)
            with self.assertRaises(ValueError):
                ratings.load_role_weights(path)

    def test_set_role_weights_changes_the_formula(self):
        db = Snapshot()
        pulisic = db.player(PULISIC_UID)
        baseline = ratings.role_rating(pulisic)
        self.assertAlmostEqual(baseline, 77.49, places=2)

        # Key-only weights: the role half becomes the key share (0.775 for
        # Pulisic), so with CA still half the blend rises slightly.
        obj = dict(ratings.DEFAULT_ROLE_WEIGHTS)
        obj["PositionRatingPenalties"] = list(ratings.POSITION_RATING_PENALTIES)
        obj["PreferableAttributesWeight"] = 0.0
        ratings.set_role_weights(obj)
        changed = ratings.role_rating(pulisic)
        self.assertNotEqual(round(changed, 2), round(baseline, 2))

        ratings.set_role_weights(None)
        self.assertAlmostEqual(ratings.role_rating(pulisic), baseline, places=6)

    def test_set_role_weights_rejects_a_bad_object(self):
        obj = dict(ratings.DEFAULT_ROLE_WEIGHTS)
        obj["PositionRatingPenalties"] = []
        with self.assertRaises(ValueError):
            ratings.set_role_weights(obj)


class AvoidWeightIsNotInTheDisplayedRatingTests(unittest.TestCase):
    """Why AVOID_ATTRIBUTES_WEIGHT is documented but not multiplied in.

    The original's own role file gives every duty two attribute groups only —
    "KeyAttribute": true and false. There is no avoid group, so the weight of 3
    has nothing to act on for the header rating. The arithmetic agrees: Pulisic's
    header is exactly 0.5 * 158/200 + 0.5 * 0.76 = 0.775, and any positive
    avoid term with a non-zero attribute average would move it off 77.49.
    """

    def test_the_role_file_has_two_groups_per_duty(self):
        from fmrte.role_definitions import RATINGS

        for general, roles in RATINGS.items():
            for role, duties in roles.items():
                for duty, definition in duties.items():
                    self.assertIsInstance(definition, tuple, (general, role, duty))
                    self.assertEqual(len(definition), 2, (general, role, duty))

    def test_pulisic_header_is_the_key_pref_blend(self):
        db = Snapshot()
        pulisic = db.player(PULISIC_UID)
        ca_ratio = pulisic["ca"] / ratings.ABILITY_MAX
        blend = 0.5 * ca_ratio + 0.5 * ratings.role_score(pulisic)
        self.assertAlmostEqual(blend, 0.775, places=9)
        self.assertEqual(ratings.role_rating(pulisic), 77.49)


if __name__ == "__main__":
    unittest.main()
