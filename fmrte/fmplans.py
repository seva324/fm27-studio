"""The FM27 plans: a tactic from the squad, and a week of training.

This is the *one* home of the plan rules.  Before this module existed the same
four tables and the same two generators were declared twice - once in
``assistant`` and once in ``fmtools`` - and the panel had to try one and fall
back to the other.  Now:

* ``TRAINING_ACTIVITIES`` (with ``ACTIVITY_BY_ID`` and ``DAYS``) is the single
  activity table.
* ``TEAM_INSTRUCTIONS`` is the single instruction table; ``TEAM_INSTRUCTION_KEYS``
  is its key order and ``TEAM_INSTRUCTION_VALUES`` *is* the same mapping under
  the older name, so a new setting joins all three automatically.
* ``build_tactic_from_squad`` is the single tactic generator: roles and
  instructions are derived from the eleven who will actually play, never copied
  from a template.
* ``training_week`` is the single training generator, and ``training_sheet`` is
  the one renderer for it.

The plan vocabulary (duties and the role catalogue) lives here too, because a
plan is the only thing that reads it.

Nothing here writes into the game: every plan says ``writes_game: False``.  No
``tkinter``, ``sqlite3`` or ``ctypes`` import.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from . import tacticfiles
from .fmlineup import Constraints, optimize_lineup
from .fmroles import (DEFAULT_MINIMUM_POSITION, OUT_ROLE_PREFERENCE, PHASE_IN,
                      PHASE_OUT, POSITION_LABELS, ROLE_PREFERENCE,
                      attr_value, as_int, best_role_for, position_score,
                      role_display, short_label)
from .fmsquad import squad_rows

# ---------------------------------------------------------------------------
# The rule tables
# ---------------------------------------------------------------------------

# The identifiers the assistant measured from FM27, grouped by trainer tab.
# This is the activity table: the tool output, the plan and the assistant all
# read it here.
TRAINING_ACTIVITIES: tuple[tuple[int, str, str, str], ...] = (
    (0, "Passing scenario", "传球场景", "Tactical understanding"),
    (1, "Build-up play", "进攻组织", "Tactical understanding"),
    (2, "Chance creation", "创造机会", "Tactical understanding"),
    (3, "In-possession shape", "有球队形", "Tactical understanding"),
    (4, "Engagement line", "遭遇线", "Tactical understanding"),
    (5, "Pressing intensity", "压迫强度", "Tactical understanding"),
    (6, "Pressing traps", "压迫陷阱", "Tactical understanding"),
    (7, "Out-of-possession shape", "无球队形", "Tactical understanding"),
    (101, "Progressive passing", "传球推进", "Match preparation"),
    (102, "Final third", "进攻三区", "Match preparation"),
    (200, "Individual development", "个人培养", "Ball skills"),
    (201, "Attacking ball feel", "进攻球感训练", "Ball skills"),
    (202, "Defensive ball feel", "防守球感训练", "Ball skills"),
    (203, "Ball control", "控球训练", "Ball skills"),
    (300, "Rest", "休息", "Physical and recovery"),
    (301, "Recovery", "恢复", "Physical and recovery"),
    (400, "Team bonding", "团队配合", "Extra activities"),
    (500, "Match", "比赛", "Match day"),
)

ACTIVITY_BY_ID: dict[int, tuple[int, str, str, str]] = {
    row[0]: row for row in TRAINING_ACTIVITIES}

DAYS: tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday",
                         "Friday", "Saturday", "Sunday")

# A week has three sessions a day in FM27's schedule grid.
SESSIONS_PER_DAY = 3

# The team instructions the tactic file stores, with the values the decoder
# accepts.  A plan names a value for each; the sheet prints them so the person
# can set the same ones in the game.  ``TEAM_INSTRUCTION_VALUES`` is this very
# mapping under the name the tool layer has always exported, and the key order
# is derived from it, so there is one table and no copy to drift.
TEAM_INSTRUCTIONS: dict[str, tuple[str, ...]] = {
    "mentality": ("Very Defensive", "Defensive", "Cautious", "Balanced",
                  "Positive", "Attacking", "Very Attacking"),
    "tempo": ("Much Lower", "Lower", "Standard", "Higher", "Much Higher"),
    "passingDirectness": ("Much Shorter", "Shorter", "Standard", "Longer",
                          "Much Longer"),
    "lineOfEngagement": ("Much Lower", "Lower", "Standard", "Higher",
                         "Much Higher"),
    "defensiveLine": ("Much Lower", "Lower", "Standard", "Higher",
                      "Much Higher"),
    "triggerPress": ("Much Lower", "Lower", "Standard", "Higher", "Much Higher"),
    "attackingWidth": ("Much Narrower", "Narrower", "Standard", "Wider",
                       "Much Wider"),
    "crossingStyle": ("Work Ball Into Box", "Mixed", "Whipped Crosses",
                      "Floated Crosses"),
    "tackling": ("Stay On Feet", "Standard", "Get Stuck In"),
    "timeWasting": ("Never", "Sometimes", "Frequently", "Always"),
}

TEAM_INSTRUCTION_VALUES: dict[str, tuple[str, ...]] = TEAM_INSTRUCTIONS

TEAM_INSTRUCTION_KEYS: tuple[str, ...] = tuple(TEAM_INSTRUCTIONS)

# The duty vocabulary a plan's slots use.
DUTIES: tuple[str, ...] = ("Defend", "Support", "Attack")

# A small, honest role catalogue: the roles the game's own role list uses for
# each position group, with the duties each role is normally given.  The full
# catalogue lives in the game's Ratings/rolesAndDuties.json; this is the subset
# a template-only plan needs, and every entry is a role the game shows.
ROLE_CATALOGUE: dict[str, list[tuple[str, str]]] = {
    "GK": [("Goalkeeper", "Defend"), ("Sweeper Keeper", "Support")],
    "DL": [("Full-Back", "Defend"), ("Wing-Back", "Support"),
           ("Inverted Full-Back", "Defend")],
    "DR": [("Full-Back", "Defend"), ("Wing-Back", "Support"),
           ("Inverted Full-Back", "Defend")],
    "DC": [("Centre-Back", "Defend"), ("Ball-Playing Centre-Back", "Defend"),
           ("No-Nonsense Centre-Back", "Defend")],
    "WBL": [("Wing-Back", "Support"), ("Complete Wing-Back", "Attack")],
    "WBR": [("Wing-Back", "Support"), ("Complete Wing-Back", "Attack")],
    "DM": [("Defensive Midfielder", "Defend"), ("Anchor", "Defend"),
           ("Deep-Lying Playmaker", "Support"), ("Ball-Winning Midfielder", "Support")],
    "ML": [("Winger", "Attack"), ("Wide Midfielder", "Support"),
           ("Inverted Winger", "Attack")],
    "MR": [("Winger", "Attack"), ("Wide Midfielder", "Support"),
           ("Inverted Winger", "Attack")],
    "MC": [("Central Midfielder", "Support"), ("Box-to-Box Midfielder", "Support"),
           ("Advanced Playmaker", "Attack"), ("Mezzala", "Support")],
    "AML": [("Winger", "Attack"), ("Inside Forward", "Attack"),
            ("Inverted Winger", "Attack")],
    "AMR": [("Winger", "Attack"), ("Inside Forward", "Attack"),
            ("Inverted Winger", "Attack")],
    "AMC": [("Attacking Midfielder", "Attack"), ("Advanced Playmaker", "Attack"),
            ("Shadow Striker", "Attack"), ("Enganche", "Support")],
    "ST": [("Advanced Forward", "Attack"), ("Poacher", "Attack"),
           ("Complete Forward", "Attack"), ("Deep-Lying Forward", "Support"),
           ("Pressing Forward", "Defend"), ("Target Forward", "Attack")],
}


class NoTemplateError(LookupError):
    """No tactic template was available to build a plan from."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def formation_label(positions: Iterable[str]) -> str:
    """Count the outfield lines the way the game names a shape."""
    lines = [0, 0, 0, 0, 0]
    for position in positions:
        if position == "Goalkeeper":
            continue
        if position.startswith("Defender") or position.startswith("WingBack"):
            lines[0] += 1
        elif position.startswith("DefensiveMidfielder"):
            lines[1] += 1
        elif position.startswith("Midfielder"):
            lines[2] += 1
        elif position.startswith("AttackingMidfielder"):
            lines[3] += 1
        elif position.startswith("Striker"):
            lines[4] += 1
    return "-".join(str(count) for count in lines if count)


def pick_template(templates: Sequence[dict[str, Any]], template_name: str
                  ) -> dict[str, Any]:
    """The named template, else the first one; a missing list is an error."""
    for candidate in templates:
        if candidate.get("name") == template_name or candidate.get("file") == template_name:
            return candidate
    if not templates:
        raise NoTemplateError("no tactic template was found")
    return templates[0]


def _average(players: Sequence[dict[str, Any]], column: str) -> float:
    """The squad's mean for one attribute, as a float.

    It stays fractional on purpose: the thresholds below are written at .5, so
    rounding the mean first would both lose information and make a value like
    14.5 round to 14 and miss the very threshold it was written for.
    """
    values = [attr_value(player, column) for player in players]
    if not values:
        return 0.0
    return sum(values) / len(values)


def derive_instructions(players: Sequence[dict[str, Any]]) -> dict[str, str]:
    """The ten team instructions, each derived from the squad's own numbers.

    Nothing is copied from a template: a setting is only chosen from the values
    the decoder accepts, which is the rule the planner exists to enforce.
    """
    squad = [player for player in players if position_score(player, "Goalkeeper") < 15
             ] or list(players)
    passing = _average(squad, "passing")
    technique = _average(squad, "technique")
    pace = _average(squad, "pace")
    acceleration = _average(squad, "acceleration")
    stamina = _average(squad, "stamina")
    work_rate = _average(squad, "work_rate")
    aggression = _average(squad, "aggression")
    anticipation = _average(squad, "anticipation")
    first_touch = _average(squad, "first_touch")
    decisions = _average(squad, "decisions")
    composure = _average(squad, "composure")

    technical = (passing + technique + first_touch) / 3
    athletic = (pace + acceleration + stamina) / 3
    pressing = (work_rate + stamina + aggression) / 3

    wanted: dict[str, str] = {}
    if technical >= 14.5 and athletic >= 14.5:
        wanted["mentality"] = "Attacking"
    elif athletic >= 14.0 or technical >= 14.5:
        wanted["mentality"] = "Positive"
    elif technical <= 11.5 and athletic <= 12.5:
        wanted["mentality"] = "Cautious"
    else:
        wanted["mentality"] = "Balanced"

    if technical >= 14.5 and decisions >= 13.5:
        wanted["tempo"] = "Higher"
    elif technical <= 11.5:
        wanted["tempo"] = "Lower"
    else:
        wanted["tempo"] = "Standard"

    if technical >= 14.5 and (composure + first_touch) / 2 >= 14.0:
        wanted["passingDirectness"] = "Shorter"
    elif technical <= 11.5:
        wanted["passingDirectness"] = "Longer"
    else:
        wanted["passingDirectness"] = "Standard"

    if pressing >= 14.5:
        wanted["lineOfEngagement"] = "Higher"
    elif pressing <= 11.5:
        wanted["lineOfEngagement"] = "Lower"
    else:
        wanted["lineOfEngagement"] = "Standard"

    if pace >= 14.0 and anticipation >= 13.5:
        wanted["defensiveLine"] = "Higher"
    elif pace <= 11.5:
        wanted["defensiveLine"] = "Lower"
    else:
        wanted["defensiveLine"] = "Standard"

    if work_rate >= 14.5 and stamina >= 14.0:
        wanted["triggerPress"] = "Higher"
    elif work_rate <= 11.5:
        wanted["triggerPress"] = "Lower"
    else:
        wanted["triggerPress"] = "Standard"

    if athletic >= 14.5:
        wanted["attackingWidth"] = "Wider"
    elif technical >= 14.5:
        wanted["attackingWidth"] = "Narrower"
    else:
        wanted["attackingWidth"] = "Standard"

    if _average(squad, "crossing") >= 14 and _average(squad, "jumping_reach") >= 14:
        wanted["crossingStyle"] = "Whipped Crosses"
    elif _average(squad, "crossing") <= 11:
        wanted["crossingStyle"] = "Work Ball Into Box"
    else:
        wanted["crossingStyle"] = "Mixed"

    if aggression >= 14.5:
        wanted["tackling"] = "Get Stuck In"
    elif aggression <= 10.5:
        wanted["tackling"] = "Stay On Feet"
    else:
        wanted["tackling"] = "Standard"

    wanted["timeWasting"] = "Sometimes"

    # Only labels the decoder accepts may be written, so an unexpected value is
    # replaced by the setting's own standard rather than passed on.
    out: dict[str, str] = {}
    for key in TEAM_INSTRUCTION_KEYS:
        allowed = TEAM_INSTRUCTION_VALUES.get(key, ())
        value = wanted.get(key)
        out[key] = value if value in allowed else (
            "Standard" if "Standard" in allowed else (allowed[0] if allowed else ""))
    return out


# ---------------------------------------------------------------------------
# The tactic: derived from the eleven who will play
# ---------------------------------------------------------------------------

def build_tactic_from_squad(db: Any, club: str, template_name: str = "",
                            style: str = "Balanced",
                            minimum_position_score: int = DEFAULT_MINIMUM_POSITION,
                            name: str = "") -> dict[str, Any]:
    """A full tactic for the squad that is actually at the club.

    This is the one tactic generator: the panel's "Build the plan" button and
    the ``fm27_build_tactic_from_squad`` tool both call it, so they cannot
    disagree.  Roles come from each player's own attributes, and the ten team
    instructions from the eleven who will actually play - never from the whole
    registration list, whose youth players would drag every average down.
    """
    squad = squad_rows(db, club, 300)
    if not squad:
        return {"error": f"no players are stored for {club}"}
    templates: dict[str, dict[str, Any]] = {}
    try:
        for template in tacticfiles.load_templates():
            templates[template["name"]] = template
            templates[template["file"]] = template
    except Exception:  # noqa: BLE001 - no readable folder means no template
        templates = {}
    chosen = templates.get(template_name) if template_name else None
    if chosen is None and templates:
        # A stable default: the first template in file order, so two runs of the
        # same question produce the same shape.
        chosen = sorted({id(value): value for value in templates.values()}.values(),
                        key=lambda row: row["file"])[0]
    if chosen is None:
        return {"error": "no tactic template was found; set FMRTE_GAME_DIR to the "
                         "folder that holds Tactics/*.json"}
    positions = list(chosen["positions"])
    lineup = optimize_lineup(squad, positions, Constraints(
        minimum_position_score=minimum_position_score))
    by_id = {as_int(player.get("unique_id"), 0): player for player in squad}
    eleven = [by_id[row["player_unique_id"]]
              for row in lineup.get("assignments", [])
              if row["player_unique_id"] in by_id]
    if not eleven:
        return {"error": "the squad cannot fill this shape at the minimum standard"}
    instructions = derive_instructions(eleven)
    if style:
        lowered = style.lower()
        allowed = TEAM_INSTRUCTION_VALUES["mentality"]
        if "attack" in lowered:
            instructions["mentality"] = "Attacking"
        elif "defens" in lowered or "counter" in lowered:
            instructions["mentality"] = "Cautious"
        elif "press" in lowered:
            instructions["mentality"] = "Positive"
            instructions["triggerPress"] = "Higher"
            instructions["lineOfEngagement"] = "Higher"
        if instructions["mentality"] not in allowed:
            instructions["mentality"] = "Balanced"

    roles: list[dict[str, Any]] = []
    for row in lineup.get("assignments", []):
        slot = int(row["slot"])
        position = positions[slot - 1]
        # The same index that picked the eleven above; the second walk over
        # ``squad`` it replaces could only ever return the same player.
        player = by_id.get(row["player_unique_id"])
        if player is None:
            continue
        in_role, in_fit = best_role_for(
            player, position, PHASE_IN, ROLE_PREFERENCE.get(short_label(position), ()))
        out_role, out_fit = best_role_for(
            player, position, PHASE_OUT,
            OUT_ROLE_PREFERENCE.get(short_label(position), ()))
        roles.append({
            "slot": slot,
            "position": short_label(position),
            "player_unique_id": row["player_unique_id"],
            "player_name": row.get("player_name"),
            "in_possession": {
                "role": role_display(in_role) if in_role else None,
                "score": in_fit.score if in_fit.available else None,
            },
            "out_of_possession": {
                "role": role_display(out_role) if out_role else None,
                "score": out_fit.score if out_fit.available else None,
            },
        })

    plan_name = name.strip() or f"{chosen['name']} — {club}"
    lines = [plan_name,
             f"Shape: {chosen['name']}   ·   Club: {club}",
             f"Style: {style or 'Balanced'}",
             "",
             "Eleven roles:"]
    for row in roles:
        lines.append(
            f"{row['slot']:>2}. {row['position']:<4} {row['player_name']} — "
            f"{row['in_possession']['role']}"
            f" (with the ball, {row['in_possession']['score']}) / "
            f"{row['out_of_possession']['role']} (without, "
            f"{row['out_of_possession']['score']})")
    lines.append("")
    lines.append("Team instructions:")
    for key in TEAM_INSTRUCTION_KEYS:
        lines.append(f"  {key}: {instructions[key]}")
    if lineup.get("unfilled_slots"):
        lines.append("")
        lines.append("Slots the squad cannot fill at the minimum standard: "
                     + ", ".join(lineup["unfilled_slots"]))

    return {
        "plan": plan_name,
        "club": club,
        "template": chosen["name"],
        "shape": lineup.get("shape"),
        "style": style or "Balanced",
        "roles": roles,
        "instructions": instructions,
        "unfilled_slots": lineup.get("unfilled_slots"),
        "team_score": lineup.get("team_score"),
        "sheet": "\n".join(lines),
        "writes_game": False,
        "guidance": (
            "This is a sheet to set up in the game, not a tactic file. Every "
            "value is one the tactic editor offers, so nothing has to be "
            "guessed when typing it in."),
    }


# ---------------------------------------------------------------------------
# The template-only plan (no squad available)
# ---------------------------------------------------------------------------

def _pick_role(position: str, style: str, index: int) -> tuple[str, str]:
    """Choose a role for one slot from the catalogue, biased by the style."""
    label = POSITION_LABELS.get(position, "MC")
    options = ROLE_CATALOGUE.get(label, ROLE_CATALOGUE["MC"])
    lowered = style.lower()
    if "defensive" in lowered or "counter" in lowered:
        preferred = ("Defend", "Support")
    elif "attacking" in lowered:
        preferred = ("Attack", "Support")
    else:
        preferred = ("Support", "Attack", "Defend")
    for duty in preferred:
        for role, role_duty in options:
            if role_duty == duty:
                return role, role_duty
    role, role_duty = options[index % len(options)]
    return role, role_duty


def style_instructions(style: str) -> dict[str, str]:
    """The style's own instruction preset, for a plan with no squad behind it."""
    lowered = style.lower()
    if "defensive" in lowered or "counter" in lowered:
        return {
            "mentality": "Cautious", "tempo": "Lower",
            "passingDirectness": "Standard", "lineOfEngagement": "Lower",
            "defensiveLine": "Lower", "triggerPress": "Lower",
            "attackingWidth": "Standard", "crossingStyle": "Mixed",
            "tackling": "Stay On Feet", "timeWasting": "Frequently",
        }
    if "attacking" in lowered or "press" in lowered:
        return {
            "mentality": "Positive", "tempo": "Higher",
            "passingDirectness": "Longer", "lineOfEngagement": "Higher",
            "defensiveLine": "Higher", "triggerPress": "Much Higher",
            "attackingWidth": "Wider", "crossingStyle": "Whipped Crosses",
            "tackling": "Get Stuck In", "timeWasting": "Never",
        }
    return {
        "mentality": "Balanced", "tempo": "Standard",
        "passingDirectness": "Standard", "lineOfEngagement": "Standard",
        "defensiveLine": "Standard", "triggerPress": "Standard",
        "attackingWidth": "Standard", "crossingStyle": "Mixed",
        "tackling": "Standard", "timeWasting": "Sometimes",
    }


def build_template_plan(templates: Sequence[dict[str, Any]], template_name: str,
                        style: str, name: str) -> dict[str, Any]:
    """A plan drawn from a template and a style, with no squad to judge.

    This is not the squad generator and it is not what the panel builds; it is
    the degenerate form used when only a template is known, and it shares the
    template picker, the position labels and the instruction table with the
    real one.
    """
    template = pick_template(templates, template_name)
    slots = []
    for index, position in enumerate(template["positions"]):
        role, duty = _pick_role(position, style, index)
        slots.append({
            "number": index + 1,
            "position": POSITION_LABELS.get(position, "MC"),
            "role": role,
            "duty": duty,
        })
    return {
        "name": name or f"{template['name']} — {style}",
        "template": template["file"],
        "formation": formation_label(template["positions"]),
        "style": style,
        "slots": slots,
        "instructions": style_instructions(style),
    }


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def activity_label(activity: int) -> str:
    row = ACTIVITY_BY_ID.get(activity)
    return row[1] if row else f"Unknown ({activity})"


def activity_localised(activity: int) -> str:
    row = ACTIVITY_BY_ID.get(activity)
    return row[2] if row else ""


def training_sheet(name: str, focus: str, days: Sequence[Sequence[int]],
                   notes: Sequence[str] = ()) -> str:
    """One week grid: the plan's sheet, printed identically everywhere."""
    lines = [name, f"Focus: {focus}", ""]
    for index, day in enumerate(days):
        labels = []
        for activity in day:
            row = ACTIVITY_BY_ID.get(activity)
            labels.append(row[1] if row else "Rest")
        lines.append(f"{DAYS[index]:<10} " + "  ·  ".join(labels))
    if notes:
        lines.append("")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


def training_activities() -> dict[str, Any]:
    return {
        "count": len(TRAINING_ACTIVITIES),
        "activities": [
            {"id": identifier, "name": name, "localised": localised, "group": group}
            for identifier, name, localised, group in TRAINING_ACTIVITIES
        ],
        "days": list(DAYS),
        "sessions_per_day": SESSIONS_PER_DAY,
        "guidance": (
            "Identifiers are the ones measured from FM27. 300 is rest and 301 "
            "recovery; 0-7 are tactical understanding, 101-102 match preparation, "
            "200-203 ball skills, 400 extra activities, 500 match day."),
    }


def training_week(focus: str, name: str = "",
                  match_day: str = "") -> dict[str, Any]:
    """A seven-day, three-session week that matches the focus."""
    lowered = (focus or "").lower()
    if "recover" in lowered or "rest" in lowered or "tired" in lowered:
        week = [[301, 300, 300], [301, 300, 300], [301, 300, 300],
                [301, 300, 300], [301, 300, 300], [300, 300, 300],
                [300, 300, 300]]
        notes = ["Recovery days use 301; rest days use 300.",
                 "No tactical work, so sharpness falls slowly rather than fast."]
    elif "press" in lowered or "defens" in lowered:
        week = [[5, 6, 7], [4, 7, 301], [5, 6, 7], [4, 5, 301],
                [6, 7, 102], [101, 102, 301], [300, 300, 300]]
        notes = ["Pressing intensity (5) and pressing traps (6) are the two "
                 "sessions that raise the press.",
                 "Out-of-possession shape (7) is what makes the press hold.",
                 "The measured evidence says a non-standard pressing trap costs "
                 "points, so use 6 to train the shape rather than to change it."]
    elif "attack" in lowered or "chance" in lowered or "final third" in lowered:
        week = [[2, 102, 201], [1, 2, 301], [2, 102, 203], [1, 2, 301],
                [102, 201, 203], [101, 102, 301], [300, 300, 300]]
        notes = ["Chance creation (2) and final third (102) are the attacking "
                 "sessions.",
                 "Ball feel (201/203) keeps the technical work in the week."]
    elif "ball" in lowered or "technical" in lowered or "control" in lowered:
        week = [[203, 201, 202], [203, 201, 301], [203, 202, 201],
                [203, 201, 301], [201, 202, 203], [203, 201, 301],
                [300, 300, 300]]
        notes = ["Ball control (203) is the base session; ball feel (201/202) "
                 "sits on top of it."]
    elif "bond" in lowered or "morale" in lowered or "team" in lowered:
        week = [[400, 3, 301], [400, 7, 301], [3, 4, 301], [400, 3, 301],
                [7, 4, 301], [400, 301, 300], [300, 300, 300]]
        notes = ["Team bonding (400) is the extra-activities session.",
                 "Shape sessions (3/7) keep the tactical work while morale "
                 "recovers."]
    else:
        week = [[3, 4, 301], [1, 2, 301], [5, 7, 301], [3, 4, 301],
                [101, 102, 301], [200, 203, 301], [300, 300, 300]]
        notes = ["A balanced week: shape, build-up, pressing, then match "
                 "preparation.",
                 "The last day is rest (300) before the match."]

    if match_day:
        wanted = match_day.strip().lower()
        for index, day in enumerate(DAYS):
            if day.lower().startswith(wanted[:3]):
                week[index] = [500, 500, 500]
                notes.append(f"{day} is the match day (500).")
                break

    labels = []
    for index, day in enumerate(week):
        labels.append({
            "day": DAYS[index],
            "sessions": [
                {"id": activity,
                 "name": ACTIVITY_BY_ID.get(activity, (0, "Rest"))[1]}
                for activity in day
            ],
        })
    plan_name = name.strip() or f"{focus or 'Balanced'} week"
    plan_focus = focus or "Balanced"
    return {
        "name": plan_name,
        "focus": plan_focus,
        "days": labels,
        "notes": notes,
        "sheet": training_sheet(plan_name, plan_focus, week, notes),
        "writes_game": False,
        "guidance": (
            "Every identifier is one FM27 measures. Tactical familiarity is the "
            "lever the evidence supports: tactical sessions build it, rest days "
            "protect sharpness."),
    }
