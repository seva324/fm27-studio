"""The FM27 tool catalogue the model sees, and the dispatcher that runs it.

This module is now only the *surface* of the FM27 tools:

* :func:`tool_specs` - the JSON-schema catalogue (what the model may call).
* :func:`execute_tool` - one name, one argument dict, one readable result.
* the Match Lab knowledge wrappers, which project ``fmrte.knowledge``'s pack
  into the tool result shape.
* re-exports of every rule and reader the tool layer used to declare here, so
  the tests and the assistant keep importing the names they always did.

The rules themselves live in the modules this one is built from:

======================================  =====================================
``fmroles.py``                          position, role fit, slot fit, fitness
``fmlineup.py``                         constraints and the XI optimizer
``fmsquad.py``                          squad depth, club context, the money
``fmtransfers.py``                      shortlists and replacements
``fmlookup.py``                         name searches, player/club detail
``fmplans.py``                          the plan tables, tactic and training
``tacticfiles.py``                      the game's own ``Tactics/*.json``
======================================  =====================================

Every tool is read-only and operates on the editor's own data layer
(``fmrte.db.Snapshot``), through its read interface, so a tool can never write
into the game and never touches its SQLite connection.

Nothing in this module imports Tkinter or touches a provider.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import knowledge
# --- the plan tables and generators, re-exported ---------------------------
from .fmplans import (ACTIVITY_BY_ID, DAYS, TEAM_INSTRUCTION_KEYS,
                      TEAM_INSTRUCTION_VALUES, TRAINING_ACTIVITIES,
                      build_tactic_from_squad, derive_instructions,
                      training_activities, training_week)
# --- the rules and readers, re-exported ------------------------------------
from .fmlineup import Assignment, Constraints, optimize_lineup
from .fmlookup import (find_clubs, fixtures, player_detail, search_players,
                       search_staff)
from .fmroles import (ABILITY_SHARE, DEFAULT_MINIMUM_POSITION, LABEL_POSITIONS,
                      LABEL_TO_GENERAL, OUT_ROLE_PREFERENCE, PHASE_IN, PHASE_OUT,
                      POSITION_FIELDS, POSITION_LABELS, POSITION_SHARE,
                      ROLE_PREFERENCE, ROLE_SHARE, Fit, SlotFit, as_int,
                      best_role_for, canonical_role, condition_percent,
                      fitness_score, general_for, position_field, position_score,
                      role_attributes, role_display, role_fit, role_keys,
                      short_label, sharpness_percent, slot_fit,
                      unavailable_reason)
from .fmsquad import (CONTRACT_HORIZON_MONTHS, analyze_squad, club_context,
                      position_depth, squad_rows)
from .fmtransfers import find_replacements, transfer_shortlist


# ---------------------------------------------------------------------------
# Knowledge: the Match Lab pack, projected for the model
# ---------------------------------------------------------------------------

def search_knowledge(query: str = "", tag: str = "", limit: int = 5
                     ) -> dict[str, Any]:
    pack = knowledge.load()
    matches = knowledge.search(query, tag, limit)
    return {
        "query": query or None,
        "tag": tag or None,
        "count": len(matches),
        "findings": [finding.to_map() for finding in matches],
        "engine": pack.engine,
        "version": pack.version,
        "method": pack.method,
        "caveats": list(pack.caveats),
        "source": pack.source,
        "guidance": knowledge.guidance(),
    }


def knowledge_study(study: str) -> dict[str, Any]:
    pack = knowledge.load()
    matches = knowledge.study(study)
    out: dict[str, Any] = {"study": study, "count": len(matches)}
    if not matches:
        out["available_studies"] = pack.study_ids()
        out["guidance"] = ("No study matched that id; use one of the available "
                           "studies or fm27_search_knowledge with a plain question.")
        return out
    out["findings"] = [finding.to_map() for finding in matches]
    out["method"] = pack.method
    out["caveats"] = list(pack.caveats)
    out["source"] = pack.source
    return out


def management_priorities() -> dict[str, Any]:
    pack = knowledge.load()
    return {
        "count": len(knowledge.priorities()),
        "priorities": knowledge.priorities(),
        "not_in_snapshot": list(knowledge.NOT_IN_SNAPSHOT),
        "guidance": (
            "Raise these in this order before recommending tactics or transfers. "
            "Sharpness and condition are match inputs this app cannot read from "
            "the snapshot: ask the user, then reason about them with the "
            "magnitudes above. Current and potential ability are deliberately "
            "absent because the match engine does not read them."),
        "method": pack.method,
        "caveats": list(pack.caveats),
        "source": pack.source,
    }


# ---------------------------------------------------------------------------
# The catalogue the model sees
# ---------------------------------------------------------------------------

def _schema(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def tool_specs() -> list[dict[str, Any]]:
    """The tools, in the OpenAI function-calling shape.

    The catalogue and :func:`execute_tool` are one contract: every argument the
    dispatcher reads is declared here, because ``additionalProperties`` is
    false and a model can only pass what a schema allows - and nothing is
    declared that the dispatcher then ignores.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "fm27_find_players",
                "description": (
                    "Search FM27 players in the loaded save by name, club, age, "
                    "ability or nationality. Returns the FM unique id, which is "
                    "the authoritative identity."),
                "parameters": _schema({
                    "name": _string("Player name contains this text"),
                    "club": _string("Exact club name"),
                    "nationality": _string("Exact nationality"),
                    "min_age": _integer("Minimum age"),
                    "max_age": _integer("Maximum age"),
                    "min_ca": _integer("Minimum current ability"),
                    "max_ca": _integer("Maximum current ability"),
                    "limit": _integer("Maximum players to return, 1-40, default 15"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_player_detail",
                "description": (
                    "Everything about one player: attributes, position ratings, "
                    "best role, the CA-blended role rating, fitness and contract."),
                "parameters": _schema({
                    "player_unique_id": _integer("FM unique id of the player"),
                }, ("player_unique_id",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_club_context",
                "description": (
                    "A club's profile, finances, facility ratings, squad and next "
                    "fixture. Facility ratings use FM's 0-20 scale; money is raw "
                    "pounds."),
                "parameters": _schema({
                    "club": _string("Club name, for example Milan"),
                    "squad_limit": _integer("Maximum squad players, default 25"),
                }, ("club",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_find_clubs",
                "description": "Find clubs by name, nation or competition.",
                "parameters": _schema({
                    "name": _string("Club name contains this text"),
                    "nation": _string("Exact nation"),
                    "competition": _string("Exact competition"),
                    "limit": _integer("Maximum clubs, default 12"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_find_staff",
                "description": (
                    "Search a club's COMPLETE backroom staff roster (all 90+ staff, "
                    "not capped at 12) or search globally for coaches, scouts, "
                    "physios, HoYD, DoF, sports scientists, and analysts, ranked by "
                    "official FM coaching/role star ratings (0.5-5.0★) using live "
                    "attributes when attached to FM27."),
                "parameters": _schema({
                    "club": _string("Exact club name to inspect all staff at that club (omit to search all 23k+ staff)"),
                    "name": _string("Part of staff name or club"),
                    "job": _string("Exact job title, e.g. 'Coach', 'Fitness Coach', 'Goalkeeping Coach', 'Scout', 'Physiotherapist', 'Head of Youth Development'"),
                    "role_category": _string(
                        "FM star rating category to score & sort by: attacking_tactical, "
                        "attacking_technical, defending_tactical, defending_technical, "
                        "possession_tactical, possession_technical, fitness, "
                        "gk_shot_stopping, gk_handling, set_pieces, scout, physio, "
                        "sports_scientist, hoyd, dof, analyst"),
                    "min_ca": _integer("Minimum Current Ability"),
                    "max_ca": _integer("Maximum Current Ability"),
                    "min_pa": _integer("Minimum Potential Ability"),
                    "max_pa": _integer("Maximum Potential Ability"),
                    "min_stars": {"type": "number", "description": "Minimum FM star rating (0.5 to 5.0)"},
                    "limit": _integer("Maximum staff to return, 1-150, default 60 (pass 120 to inspect a full club staff)"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_analyze_squad",
                "description": (
                    "Squad depth by position, tactic fit for a shape, contract "
                    "risk, injuries and the recruitment needs worth fixing first. "
                    "Use this before any squad or transfer advice."),
                "parameters": _schema({
                    "club": _string("Club name; defaults to the managed club"),
                    "minimum_position_score": _integer(
                        "Minimum positional ability, 1-20, default 15"),
                    "contract_horizon_months": _integer(
                        "Contract-risk horizon in months, default 12"),
                    "shape": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional eleven tactic positions, for example "
                            "['Goalkeeper','DefenderLeft','DefenderCentral',"
                            "'DefenderCentral','DefenderRight',"
                            "'DefensiveMidfielder','DefensiveMidfielder',"
                            "'AttackingMidfielderCentral',"
                            "'AttackingMidfielderLeft',"
                            "'AttackingMidfielderRight','Striker']"),
                    },
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_optimize_lineup",
                "description": (
                    "Build the best unique XI for a shape: one player per slot, "
                    "no duplicates, injured and on-duty players excluded unless "
                    "asked for. Returns the XI, alternatives and unfilled slots."),
                "parameters": _schema({
                    "club": _string("Club name; defaults to the managed club"),
                    "shape": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tactic positions in slot order",
                    },
                    "minimum_position_score": _integer(
                        "Minimum positional ability, 1-20, default 15"),
                    "include_injured": _boolean(
                        "Include injured players in automatic selection"),
                    "include_on_duty": _boolean(
                        "Include players away on international duty"),
                    "locked": {
                        "type": "array",
                        "description": "Assignments that must be kept",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tactic_slot": {"type": "integer"},
                                "player_unique_id": {"type": "integer"},
                            },
                            "required": ["tactic_slot", "player_unique_id"],
                        },
                    },
                    "alternative_limit": _integer(
                        "Alternatives per slot, 1-10, default 3"),
                }, ("shape",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_transfer_shortlist",
                "description": (
                    "Transfer candidates for one literal position with hard "
                    "filters (price, wage, age, ability, listed status) and a "
                    "decision score. An unknown price stays null, never free."),
                "parameters": _schema({
                    "position": _string(
                        "Template position or short label, for example Striker "
                        "or ST"),
                    "club": _string("Club to exclude; defaults to the managed club"),
                    "max_age": _integer("Maximum age"),
                    "min_ca": _integer("Minimum current ability"),
                    "min_pa": _integer("Minimum potential ability"),
                    "max_asking_price": _integer("Literal maximum asking price in pounds"),
                    "max_weekly_salary": _integer("Maximum weekly wage in pounds"),
                    "minimum_position_score": _integer(
                        "Minimum positional ability, 1-20, default 15"),
                    "minimum_attributes": {
                        "type": "object",
                        "description": (
                            "Hard attribute floors, keyed by attribute name, for "
                            "example {'pace': 15, 'passing': 14}. A candidate "
                            "below any floor is dropped, not penalised."),
                        "additionalProperties": {"type": "integer"},
                    },
                    "transfer_listed": _boolean(
                        "true = only transfer-listed, false = exclude them"),
                    "injured": _boolean("true = only injured, false = fit only"),
                    "limit": _integer("Maximum candidates, 1-30, default 8"),
                }, ("position",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_find_replacements",
                "description": (
                    "Players who would do a named player's job in his deployed "
                    "position, with the gain or loss against him."),
                "parameters": _schema({
                    "player_unique_id": _integer("The player to replace"),
                    "position": _string(
                        "Deployed position or short label; omit to use his best"),
                    "club": _string(
                        "Club to exclude; defaults to the player's own club"),
                    "max_age": _integer("Maximum candidate age"),
                    "max_asking_price": _integer("Literal maximum asking price in pounds"),
                    "limit": _integer("Maximum candidates, default 6"),
                }, ("player_unique_id",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_build_tactic_from_squad",
                "description": (
                    "Derive a complete tactic from the squad that is at the club: "
                    "a role with and without the ball for every slot, and the ten "
                    "team instructions, each chosen from values the game accepts."),
                "parameters": _schema({
                    "club": _string("Club name; defaults to the managed club"),
                    "template": _string(
                        "The game's own template name, for example 4-2-3-1"),
                    "style": _string(
                        "Attacking, Balanced, Defensive, Counter or Pressing"),
                    "minimum_position_score": _integer(
                        "Minimum positional ability, 1-20, default 15"),
                    "name": _string("Name for the plan"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_role_attributes",
                "description": (
                    "The attributes a role asks for, with the original's 16/4 "
                    "key and preferable weights. Use it to justify a role choice."),
                "parameters": _schema({
                    "position": _string("Template position or short label"),
                    "role": _string("Role name; omit to list every role there"),
                    "phase": _string("InPossession or OutOfPossession"),
                }, ("position",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_create_training_schedule",
                "description": (
                    "Build a validated seven-day, three-session FM27 training week "
                    "(from a focus preset OR a custom 7x3 `days` matrix of FM27 activity "
                    "IDs/names) AND export it as a genuine loadable `.fmf` training "
                    "schedule file directly into the user's FM27/schedules folder so they "
                    "can immediately load it via Training > Schedules > Load."),
                "parameters": _schema({
                    "focus": _string("Focus preset (Balanced, Tactical, Pressing, Attacking, Ball control, Physical, Recovery, Team bonding) or label when using custom `days`"),
                    "name": _string("Name for the schedule (also used as the exported .fmf filename)"),
                    "match_day": _string(
                        "Day the match falls on, for example Saturday"),
                    "days": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "description": (
                            "Optional custom 7-day x 3-session matrix (Monday..Sunday) of "
                            "measured FM27 activity IDs (e.g. [[301,300,300], ..., [500,300,300], [300,300,300]]). "
                            "Pass this whenever tailoring a custom week instead of asking the user to tweak a preset."
                        ),
                    },
                    "description": _string("Optional description embedded inside the .tsc training schedule"),
                    "export_fmf": _boolean("Export a real loadable .fmf file into FM27/schedules (default true when name or days is provided)"),
                }, ("focus",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_read_training_schedule",
                "description": (
                    "Read and decode an existing local FM27 .fmf training schedule "
                    "from the user's FM27/schedules folder to inspect its 7-day "
                    "sessions and description."),
                "parameters": _schema({
                    "filename": _string("Schedule filename or path inside FM27/schedules, e.g. 'FM AI Week 1.fmf'"),
                }, ("filename",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_training_activities",
                "description": "Every measured FM27 training activity id and group.",
                "parameters": _schema({}),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_search_knowledge",
                "description": (
                    "Search the measured FM27 Match Lab findings before advising "
                    "on attributes, ability, tactics, familiarity or goalkeepers. "
                    "Cite the finding id when you use one."),
                "parameters": _schema({
                    "query": _string("Words to match, for example 'pace'"),
                    "tag": _string(
                        "Exact tag: attribute, tactics, familiarity, context, "
                        "goalkeeper, position, model, method, robustness, "
                        "negative-finding"),
                    "limit": _integer("Maximum findings, 1-20, default 5"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_get_knowledge_study",
                "description": "Read every finding of one Match Lab study.",
                "parameters": _schema({
                    "study": _string(
                        "Study id or prefix, for example 01-attribute-values"),
                }, ("study",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_get_management_priorities",
                "description": (
                    "What to fix first, in measured order, before squad or "
                    "tactical advice."),
                "parameters": _schema({}),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_fixtures",
                "description": "A club's fixtures from the game date onwards.",
                "parameters": _schema({
                    "club": _string("Club name; defaults to the managed club"),
                    "limit": _integer("Maximum fixtures, default 8"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_search_online_tactics",
                "description": (
                    "Search ranked FM27 community tactics from FM-Arena (Patch 27.1.1, "
                    "direct .fmf download supported) or Sortitoutsi."),
                "parameters": _schema({
                    "source": _string("'fm-arena' (default) or 'sortitoutsi'"),
                    "query": _string("Keyword filter on tactic name or author"),
                    "formation": _string("Formation filter, e.g. '4-2-3-1' or '3-4-2-1'"),
                    "limit": _integer("Maximum tactics to return, default 10"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_download_tactic",
                "description": (
                    "Download a public .fmf tactic file from an FM-Arena thread URL "
                    "directly into the user's Football Manager 27 tactics folder."),
                "parameters": _schema({
                    "url": _string("FM-Arena thread URL returned by fm27_search_online_tactics"),
                }, ("url",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_status",
                "description": (
                    "Check whether the native FM27 Tactics Lab match engine is installed "
                    "and ready to simulate, plus currently loaded save and local saves/tactics. "
                    "If installed=false, ask the user if they want to download it from "
                    "https://github.com/seva324/fm-tactics-lab."),
                "parameters": _schema({}),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_list_local",
                "description": (
                    "List the user's local FM27 .fm save files (games folder), local "
                    ".fmf tactic files (tactics folder), and local .fmf training schedule "
                    "files (schedules folder) without guessing filenames."),
                "parameters": _schema({}),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_download_engine",
                "description": (
                    "Download FM27 Tactics Lab from https://github.com/seva324/fm-tactics-lab "
                    "into local app storage. ONLY call with confirm=true AFTER the user "
                    "explicitly agrees to download it."),
                "parameters": _schema({
                    "confirm": _boolean("Must be true only after the user confirms download"),
                }, ("confirm",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_inspect_fmf",
                "description": (
                    "Parse a local .fmf tactic file using the native FM27 game parser "
                    "to inspect its exact name, mentality, and dual-phase setup."),
                "parameters": _schema({
                    "path": _string("Absolute path or filename inside FM27 tactics folder"),
                }, ("path",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_load_save",
                "description": (
                    "Import an FM27 .fm save into the native Tactics Lab engine, load the "
                    "managed club onto the tactics board, return its real current squad "
                    "(already excluding loaned-out players), and discover real league opponents. "
                    "Pass exclude_player_uids to exclude injured or on-holiday players."),
                "parameters": _schema({
                    "save_path": _string("Path to .fm save file (omit to load the newest save)"),
                    "club": _string("Club name hint; defaults to the managed club"),
                    "tactic_index": _integer("Saved tactic slot index, default 0"),
                    "exclude_player_uids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional player unique_ids to exclude from lineup/bench (e.g. injured or on holiday)",
                    },
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_create_tactic",
                "description": (
                    "Create a real, native FM27 engine-validated custom tactic tailored to the "
                    "club's squad (formation, mentality, per-position roles, and team instructions), "
                    "validate it through Tactics Lab, and export it as a genuine .fmf file into "
                    "the user's FM27 tactics folder so it can be immediately A/B tested with "
                    "fm27_lab_run_ab_test and loaded in-game."),
                "parameters": _schema({
                    "name": _string("Name for the new tactic (used as .fmf filename)"),
                    "formation": _string("Formation such as '4-2-3-1', '4-3-3', '3-4-2-1', '4-4-2', '3-5-2', '4-2-4', '5-2-3', '4-1-2-1-2'"),
                    "mentality": _string("Mentality: 'Very Defensive', 'Defensive', 'Cautious', 'Balanced', 'Positive', 'Attacking', 'Very Attacking'"),
                    "roles": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Optional role overrides by position id, e.g. {'GK':'BallPlayingGoalkeeper','DML':'DeepLyingPlaymaker','AMC':'AttackingMidfielder'}",
                    },
                    "instructions": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Optional team instructions, e.g. {'PassingDirectness':'Shorter','Tempo':'Higher','TriggerPress':'More Often','DefensiveTransition':'Counter-Press','PressingTrap':'Standard'}",
                    },
                    "save_to_tactics_folder": _boolean("Export .fmf directly into FM27/tactics folder (default true)"),
                }, ("name", "formation")),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_run_ab_test",
                "description": (
                    "Run a native FM27 match-engine test or paired A/B comparison "
                    "(Tactic A vs Tactic B) on fixed seeds against real league opponents "
                    "(automatically imports the newest .fm save if mode='league' and none "
                    "is loaded yet) or neutral quick fixtures. Uses parallel native workers "
                    "('auto') by default. For a 300-match A/B test, e.g. 15 opponents x "
                    "10 matches_per_opponent = 150 per side (300 total matches). Always "
                    "report verified_matches and validation_scope honestly."),
                "parameters": _schema({
                    "tactic_b_kind": _string("'local_fmf', 'fm_arena', or 'save'"),
                    "tactic_b_ref": _string("Filename/path for local_fmf, URL for fm_arena, or slot index for save"),
                    "tactic_a_kind": _string("Optional baseline Tactic A kind ('save', 'local_fmf', 'fm_arena') for A/B test"),
                    "tactic_a_ref": _string("Optional baseline Tactic A filename/path, URL, or slot index"),
                    "club": _string("Club name hint when auto-loading the local .fm save"),
                    "mode": _string("'league' (uses club's .fm save & league opponents) or 'quick'"),
                    "opponent_count": _integer("Number of league opponents, default 4 (1-20)"),
                    "matches_per_opponent": _integer("Even number of home+away matches per opponent, default 2 (e.g. 10 with 15 opponents = 300 total A/B matches)"),
                    "workers": _string("Parallel native workers: 'auto' (recommended, default) or integer like '4'"),
                    "exclude_player_uids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional player unique_ids to exclude from lineup/bench (injured / on holiday)",
                    },
                    "wait": _boolean("Wait for the test to finish (default true) and return verified results"),
                    "timeout_seconds": _integer("Max seconds to wait when wait=true (default 240, max 900)"),
                }, ("tactic_b_kind",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_compare_tactics",
                "description": (
                    "Run a multi-tactic native match-engine benchmark comparing 2 to 5 "
                    "candidate tactics (from 'save', 'local_fmf', or 'fm_arena') against the "
                    "same fixed seeds and league opponents, then rank them by verified PPG, "
                    "GD, and xGD to select the strongest tactic for the club."),
                "parameters": _schema({
                    "tactics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": _string("'save', 'local_fmf', or 'fm_arena'"),
                                "ref": _string("Slot index for save, filename/path for local_fmf, or FM-Arena URL"),
                            },
                            "required": ["kind"],
                        },
                        "description": "2 to 5 candidate tactics; index 0 is the shared paired baseline",
                    },
                    "club": _string("Club name hint when auto-loading the local .fm save"),
                    "mode": _string("'league' (default) or 'quick'"),
                    "opponent_count": _integer("Number of league opponents, default 4"),
                    "matches_per_opponent": _integer("Even home+away matches per opponent, default 2"),
                    "timeout_seconds": _integer("Max seconds per pairing, default 180"),
                }, ("tactics",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_wait_job",
                "description": (
                    "Wait for an in-progress FM27 Tactics Lab simulation job_id to finish "
                    "and return its verified results."),
                "parameters": _schema({
                    "job_id": _string("Job ID returned by fm27_lab_run_ab_test or fm27_lab_jobs"),
                    "timeout_seconds": _integer("Max seconds to wait (default 180, max 300)"),
                }, ("job_id",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_cancel_job",
                "description": "Cancel the currently running FM27 Tactics Lab simulation job.",
                "parameters": _schema({
                    "confirm": _boolean("Set true to cancel the active simulation job"),
                }, ("confirm",)),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_jobs",
                "description": "Get progress and verified match results of FM27 Tactics Lab simulation jobs.",
                "parameters": _schema({}),
            },
        },
    ]


def tool_definitions() -> list[dict[str, Any]]:
    return tool_specs()


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------

def _shape_from(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            label = POSITION_LABELS.get(item.strip())
            out.append(label or item.strip().upper())
    return out


def _attribute_floors(value: Any) -> dict[str, int] | None:
    """``{"pace": 15}`` as the hard filter wants it, or None.

    The schema declares an object of integers; a model that sends a list, a
    string or a non-numeric floor gets no attribute filter rather than a failed
    tool call, and every name is normalised the way the filter compares it.
    """
    if not isinstance(value, dict):
        return None
    floors: dict[str, int] = {}
    for name, floor in value.items():
        try:
            floors[str(name).strip().lower()] = int(floor)
        except (TypeError, ValueError):
            continue
    return floors or None


def execute_tool(db: Any, name: str, arguments: dict[str, Any],
                 default_club: str = "",
                 on_progress: Any = None) -> dict[str, Any]:
    """Run one tool by name. Every failure is a readable result, never a raise."""
    args = arguments if isinstance(arguments, dict) else {}
    tool = (name or "").strip()

    def club() -> str:
        # ``club`` is the only name: every schema forbids extra properties with
        # ``additionalProperties: false``, so a second alias could never be
        # passed by the model and would be dead surface.
        return str(args.get("club") or default_club or "").strip()

    try:
        if tool == "fm27_find_players":
            return search_players(
                db, str(args.get("name") or ""), str(args.get("club") or ""),
                args.get("min_age"), args.get("max_age"),
                args.get("min_ca"), args.get("max_ca"),
                str(args.get("nationality") or ""), as_int(args.get("limit"), 15))
        if tool == "fm27_player_detail":
            return player_detail(db, as_int(args.get("player_unique_id"), 0))
        if tool == "fm27_club_context":
            return club_context(db, str(args.get("club") or default_club),
                                as_int(args.get("squad_limit"), 25))
        if tool == "fm27_find_clubs":
            return find_clubs(
                db, str(args.get("name") or ""), str(args.get("nation") or ""),
                str(args.get("competition") or ""), as_int(args.get("limit"), 12))
        if tool == "fm27_find_staff":
            raw_stars = args.get("min_stars")
            try:
                min_stars_val = float(raw_stars) if raw_stars is not None else None
            except (TypeError, ValueError):
                min_stars_val = None
            return search_staff(
                db,
                query=str(args.get("name") or ""),
                club=str(args.get("club") or ""),
                job=str(args.get("job") or ""),
                role_category=str(args.get("role_category") or ""),
                min_ca=args.get("min_ca"),
                max_ca=args.get("max_ca"),
                min_pa=args.get("min_pa"),
                max_pa=args.get("max_pa"),
                min_stars=min_stars_val,
                limit=as_int(args.get("limit"), 60),
            )
        if tool == "fm27_analyze_squad":
            return analyze_squad(
                db, club(), as_int(args.get("minimum_position_score"),
                                   DEFAULT_MINIMUM_POSITION),
                as_int(args.get("contract_horizon_months"), CONTRACT_HORIZON_MONTHS),
                _shape_from(args.get("shape")))
        if tool == "fm27_optimize_lineup":
            shape = _shape_from(args.get("shape"))
            if not shape:
                return {"error": "shape must name at least one tactic position"}
            squad = squad_rows(db, club(), 300)
            if not squad:
                return {"error": f"no players are stored for {club()}"}
            constraints = Constraints(
                minimum_position_score=as_int(args.get("minimum_position_score"),
                                              DEFAULT_MINIMUM_POSITION),
                include_injured=bool(args.get("include_injured")),
                include_on_duty=bool(args.get("include_on_duty")),
                alternative_limit=as_int(args.get("alternative_limit"), 3))
            locks = args.get("locked")
            if isinstance(locks, list):
                for row in locks:
                    if not isinstance(row, dict):
                        continue
                    slot = as_int(row.get("tactic_slot"), 0)
                    unique_id = as_int(row.get("player_unique_id"), 0)
                    if slot and unique_id:
                        constraints.locked[slot] = unique_id
            return optimize_lineup(squad, shape, constraints)
        if tool == "fm27_transfer_shortlist":
            position = str(args.get("position") or "")
            if not position:
                return {"error": "position is required"}
            return transfer_shortlist(
                db, position, str(args.get("club") or default_club),
                args.get("max_age"), args.get("min_ca"), args.get("min_pa"),
                args.get("max_asking_price"), args.get("max_weekly_salary"),
                as_int(args.get("minimum_position_score"), DEFAULT_MINIMUM_POSITION),
                args.get("transfer_listed"), args.get("injured"),
                _attribute_floors(args.get("minimum_attributes")),
                as_int(args.get("limit"), 8))
        if tool == "fm27_find_replacements":
            return find_replacements(
                db, as_int(args.get("player_unique_id"), 0),
                str(args.get("position") or ""), str(args.get("club") or ""),
                as_int(args.get("limit"), 6), args.get("max_age"),
                args.get("max_asking_price"))
        if tool == "fm27_build_tactic_from_squad":
            return build_tactic_from_squad(
                db, club(), str(args.get("template") or ""),
                str(args.get("style") or "Balanced"),
                as_int(args.get("minimum_position_score"),
                       DEFAULT_MINIMUM_POSITION),
                str(args.get("name") or ""))
        if tool == "fm27_role_attributes":
            return role_attributes(
                str(args.get("position") or ""), str(args.get("role") or ""),
                str(args.get("phase") or PHASE_IN))
        if tool == "fm27_create_training_schedule":
            from . import schedulefiles, tacticweb
            plan_name = str(args.get("name") or "")
            raw_days = args.get("days")
            desc = str(args.get("description") or "")
            raw_export = args.get("export_fmf")
            result = training_week(
                str(args.get("focus") or "Balanced"),
                plan_name,
                str(args.get("match_day") or ""),
                days=raw_days,
            )
            if "error" not in result:
                result["schedules_dir"] = tacticweb.detect_fm27_schedules_dir()
                should_export = (
                    bool(raw_export)
                    if raw_export is not None
                    else bool(plan_name.strip() or raw_days is not None)
                )
                if should_export:
                    week_ids = [
                        [int(s["id"]) for s in day["sessions"]]
                        for day in result["days"]
                    ]
                    desc_text = desc.strip() or (
                        " ".join(result.get("notes") or [])[:240]
                        or f"Generated by FM27 Studio ({result['focus']})"
                    )
                    exported = schedulefiles.export_schedule_fmf(
                        result["name"], week_ids, description=desc_text
                    )
                    result["exports_fmf"] = True
                    result["exported"] = exported
                    result["exported_path"] = exported["path"]
                    result["filename"] = exported["filename"]
                    result["size"] = exported["size"]
                    result["import_step"] = exported["import_step"]
                    result["ok"] = True
            return result
        if tool == "fm27_read_training_schedule":
            from . import schedulefiles
            fname = str(args.get("filename") or "").strip()
            if not fname:
                return {"error": "filename is required"}
            return schedulefiles.read_schedule_fmf(fname)
        if tool == "fm27_training_activities":
            return training_activities()
        if tool == "fm27_search_knowledge":
            return search_knowledge(str(args.get("query") or ""),
                                    str(args.get("tag") or ""),
                                    as_int(args.get("limit"), 5))
        if tool == "fm27_get_knowledge_study":
            return knowledge_study(str(args.get("study") or ""))
        if tool == "fm27_get_management_priorities":
            return management_priorities()
        if tool == "fm27_fixtures":
            return fixtures(db, club() or default_club, as_int(args.get("limit"), 8))
        if tool == "fm27_search_online_tactics":
            from . import tacticweb
            return tacticweb.search_online_tactics(
                source=str(args.get("source") or "fm-arena"),
                query=str(args.get("query") or ""),
                formation=str(args.get("formation") or ""),
                limit=as_int(args.get("limit"), 10),
            )
        if tool == "fm27_download_tactic":
            from . import tacticweb
            return tacticweb.download_fm_arena_tactic(str(args.get("url") or ""))
        if tool == "fm27_lab_status":
            return _get_lab_client().status()
        if tool == "fm27_lab_list_local":
            from . import tacticslab, tacticweb
            return {
                "ok": True,
                "saves": tacticslab.list_local_saves(),
                "tactics": tacticweb.list_local_fmf("tactics"),
                "schedules": tacticweb.list_local_fmf("schedules"),
            }
        if tool == "fm27_lab_download_engine":
            if not bool(args.get("confirm")):
                from . import tacticslab
                return {
                    "ok": False,
                    "error": (
                        "Confirmation required: ask the user first, then call "
                        "fm27_lab_download_engine(confirm=true) to download "
                        f"{tacticslab.LAB_REPO_URL}."
                    ),
                }
            return _lab_download_engine()
        if tool == "fm27_lab_inspect_fmf":
            return _lab_inspect_fmf(str(args.get("path") or ""))
        if tool == "fm27_lab_load_save":
            sp = str(args.get("save_path") or "").strip()
            ch = club() or default_club
            if not sp and not ch:
                return {"error": "Provide save_path or club to load a save into Tactics Lab."}
            excl = _int_list(args.get("exclude_player_uids"))
            return _lab_load_save(sp, ch, as_int(args.get("tactic_index"), 0),
                                  exclude_player_uids=excl, on_progress=on_progress)
        if tool == "fm27_lab_create_tactic":
            tname = str(args.get("name") or "").strip()
            tform = str(args.get("formation") or "").strip()
            if not tname and not tform:
                return {"error": "Provide name and formation (e.g. '4-2-3-1') to create a native .fmf tactic."}
            return _lab_create_tactic(
                name=tname or f"Custom {tform}",
                formation=tform or "4-2-3-1",
                mentality=str(args.get("mentality") or "Balanced"),
                roles=args.get("roles") if isinstance(args.get("roles"), dict) else None,
                instructions=args.get("instructions") if isinstance(args.get("instructions"), dict) else None,
                save_to_tactics_folder=bool(args.get("save_to_tactics_folder", True)),
            )
        if tool == "fm27_lab_run_ab_test":
            tb_kind = str(args.get("tactic_b_kind") or "").strip()
            if not tb_kind:
                return {"error": "tactic_b_kind is required ('save', 'local_fmf', or 'fm_arena')."}
            return _lab_run_ab_test(
                tactic_b_kind=tb_kind,
                tactic_b_ref=str(args.get("tactic_b_ref") or ""),
                tactic_a_kind=str(args.get("tactic_a_kind") or ""),
                tactic_a_ref=str(args.get("tactic_a_ref") or ""),
                club_hint=club() or default_club,
                mode=str(args.get("mode") or "league"),
                opponent_count=as_int(args.get("opponent_count"), 4),
                matches_per_opponent=as_int(args.get("matches_per_opponent"), 2),
                workers=str(args.get("workers") or "auto"),
                exclude_player_uids=_int_list(args.get("exclude_player_uids")),
                wait=bool(args.get("wait", True)),
                timeout_seconds=as_int(args.get("timeout_seconds"), 240),
                on_progress=on_progress,
            )
        if tool == "fm27_lab_compare_tactics":
            raw_tactics = args.get("tactics")
            if not isinstance(raw_tactics, list) or len(raw_tactics) < 2:
                return {"error": "tactics must be a list of 2 to 5 tactic objects ({kind, ref})."}
            return _lab_compare_tactics(
                tactics=raw_tactics,
                club_hint=club() or default_club,
                mode=str(args.get("mode") or "league"),
                opponent_count=as_int(args.get("opponent_count"), 4),
                matches_per_opponent=as_int(args.get("matches_per_opponent"), 2),
                timeout_seconds=as_int(args.get("timeout_seconds"), 180),
                on_progress=on_progress,
            )
        if tool == "fm27_lab_wait_job":
            jid = str(args.get("job_id") or "").strip()
            if not jid:
                return {"error": "job_id is required."}
            return _lab_wait_job(
                jid,
                timeout_seconds=as_int(args.get("timeout_seconds"), 180),
                on_progress=on_progress,
            )
        if tool == "fm27_lab_cancel_job":
            if not bool(args.get("confirm")):
                return {"error": "Pass confirm=true to cancel the active Lab simulation job."}
            client = _get_lab_client()
            if not client.installed:
                return _lab_missing_response()
            return {"ok": True, "result": client.cancel_job()}
        if tool == "fm27_lab_jobs":
            client = _get_lab_client()
            if not client.installed:
                return _lab_missing_response()
            return {"ok": True, "jobs": client.list_jobs()}
    except Exception as exc:  # noqa: BLE001 - a tool must never break a turn
        return {"error": f"{tool} failed: {type(exc).__name__}: {exc}"}
    return {"error": f"{tool} is not a tool this build provides"}


_SHARED_LAB_CLIENT: Any = None


def set_lab_client(client: Any) -> None:
    """Register Studio's shared TacticsLabClient so AI tools reuse one engine process."""
    global _SHARED_LAB_CLIENT
    _SHARED_LAB_CLIENT = client


def _get_lab_client():
    global _SHARED_LAB_CLIENT
    if _SHARED_LAB_CLIENT is None:
        from . import tacticslab
        _SHARED_LAB_CLIENT = tacticslab.TacticsLabClient()
    return _SHARED_LAB_CLIENT


def _tactic_spec(kind: str, ref: str) -> dict[str, Any]:
    k = (kind or "save").strip().lower()
    if k == "fm_arena":
        return {"kind": "fm_arena", "url": ref}
    if k == "local_fmf":
        return {"kind": "local_fmf", "path": ref}
    return {"kind": "save", "tactic_index": as_int(ref, 0)}


def _lab_missing_response() -> dict[str, Any]:
    from . import tacticslab
    return {
        "ok": False,
        "installed": False,
        "repo_url": tacticslab.LAB_REPO_URL,
        "error": (
            "FM27 Tactics Lab engine is not installed locally. "
            f"Ask the user if they want to download it from {tacticslab.LAB_REPO_URL} "
            "(or click 'Download Engine' in the Lab tab)."
        ),
    }


def _lab_inspect_fmf(raw_path: str) -> dict[str, Any]:
    import os
    from . import tacticweb
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    raw = raw_path.strip()
    if not raw:
        return {"error": "path is required"}
    path = raw if os.path.isabs(raw) else os.path.join(tacticweb.detect_fm27_tactics_dir(), raw)
    res = client.inspect_fmf_file(path)
    res.pop("scenario", None)
    return res


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            n = int(item)
            if n > 0:
                out.append(n)
        except (TypeError, ValueError):
            continue
    return out


def _lab_load_save(save_path: str, club_hint: str, tactic_index: int,
                   exclude_player_uids: list[int] | None = None,
                   on_progress: Any = None) -> dict[str, Any]:
    from . import tacticslab
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    sp = save_path.strip()
    if not sp:
        saves = tacticslab.list_local_saves().get("saves") or []
        if not saves:
            return {"error": "No .fm save files found in the FM27 games folder."}
        sp = saves[0]["path"]
    return client.import_and_load_save(
        sp,
        club_name_hint=club_hint or "",
        tactic_index=tactic_index,
        exclude_player_uids=exclude_player_uids,
        on_progress=on_progress,
    )


def _lab_create_tactic(*, name: str, formation: str, mentality: str,
                       roles: dict[str, str] | None,
                       instructions: dict[str, str] | None,
                       save_to_tactics_folder: bool) -> dict[str, Any]:
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    return client.create_custom_tactic(
        name=name,
        formation=formation,
        mentality=mentality,
        roles=roles,
        instructions=instructions,
        save_to_tactics_folder=save_to_tactics_folder,
    )


def _lab_download_engine() -> dict[str, Any]:
    from . import tacticslab
    res = tacticslab.download_lab_from_github()
    client = _get_lab_client()
    client.refresh_install()
    res["status"] = client.status()
    return res


def _lab_wait_job(job_id: str, *, timeout_seconds: int = 240,
                  on_progress: Any = None) -> dict[str, Any]:
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    capped_timeout = max(10, min(900, int(timeout_seconds or 240)))
    waited = client.wait_for_job(
        job_id,
        timeout_seconds=float(capped_timeout),
        on_progress=on_progress,
    )
    return {"ok": True, "job_id": job_id, **waited}


def _lab_compare_tactics(*, tactics: list[Any], club_hint: str,
                         mode: str, opponent_count: int,
                         matches_per_opponent: int, timeout_seconds: int,
                         on_progress: Any = None) -> dict[str, Any]:
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    specs: list[dict[str, Any]] = []
    for item in tactics[:5]:
        if isinstance(item, dict):
            specs.append(_tactic_spec(str(item.get("kind") or "save"), str(item.get("ref") or "")))
    if len(specs) < 2:
        return {"error": "At least 2 valid tactic specs are required."}
    capped_timeout = max(15, min(900, int(timeout_seconds or 240)))
    return client.compare_tactics(
        specs,
        mode=mode,
        club_name_hint=club_hint,
        opponent_count=opponent_count,
        matches_per_opponent=matches_per_opponent,
        timeout_per_job=float(capped_timeout),
        on_progress=on_progress,
    )


def _lab_run_ab_test(*, tactic_b_kind: str, tactic_b_ref: str,
                     tactic_a_kind: str, tactic_a_ref: str,
                     club_hint: str = "",
                     mode: str, opponent_count: int,
                     matches_per_opponent: int,
                     workers: str = "auto",
                     exclude_player_uids: list[int] | None = None,
                     wait: bool,
                     timeout_seconds: int = 240,
                     on_progress: Any = None) -> dict[str, Any]:
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    auto_loaded = None
    if mode == "league":
        auto_loaded = client.ensure_save_loaded(
            club_name_hint=club_hint,
            exclude_player_uids=exclude_player_uids,
            on_progress=on_progress,
        )
    tb = _tactic_spec(tactic_b_kind, tactic_b_ref)
    ta = _tactic_spec(tactic_a_kind, tactic_a_ref) if tactic_a_kind.strip() else None
    if callable(on_progress):
        on_progress("Starting native FM27 match-engine simulation…")
    started = client.start_ab_test(
        tactic_b=tb,
        tactic_a=ta,
        mode=mode,
        opponent_count=opponent_count,
        matches_per_opponent=matches_per_opponent,
        workers=workers,
        exclude_player_uids=exclude_player_uids,
        event_details=False,
    )
    if auto_loaded:
        started["auto_loaded_save"] = {
            "club_name": auto_loaded.get("club_name"),
            "save_path": auto_loaded.get("save_path"),
            "opponents_count": auto_loaded.get("opponents_count"),
        }
    if wait and started.get("job_id"):
        capped_timeout = max(10, min(900, int(timeout_seconds or 240)))
        waited = client.wait_for_job(
            str(started["job_id"]),
            timeout_seconds=float(capped_timeout),
            on_progress=on_progress,
        )
        started["result"] = waited.get("job")
        started["timed_out"] = bool(waited.get("timed_out"))
        if waited.get("timed_out"):
            started["guidance"] = (
                "The simulation is still running in Tactics Lab; call "
                f"fm27_lab_wait_job(job_id='{started['job_id']}', timeout_seconds=600) to finish collecting results."
            )
    return started
