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
from .fmlookup import find_clubs, fixtures, player_detail, search_players
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
                    "Build a seven-day, three-session training week from a focus "
                    "such as Pressing, Attacking, Ball control, Recovery, Team "
                    "bonding or Balanced, using FM27's measured activity ids."),
                "parameters": _schema({
                    "focus": _string("What the week should work on"),
                    "name": _string("Name for the schedule"),
                    "match_day": _string(
                        "Day the match falls on, for example Saturday"),
                }, ("focus",)),
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
                    "and ready to simulate. If installed=false, ask the user if they want "
                    "to download it from https://github.com/seva324/fm-tactics-lab."),
                "parameters": _schema({}),
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
                    "managed club onto the tactics board, and discover league opponents."),
                "parameters": _schema({
                    "save_path": _string("Path to .fm save file (omit to load the newest save)"),
                    "club": _string("Club name hint; defaults to the managed club"),
                    "tactic_index": _integer("Saved tactic slot index, default 0"),
                }),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fm27_lab_run_ab_test",
                "description": (
                    "Run a native FM27 match-engine test or A/B comparison (Tactic A vs Tactic B) "
                    "against league opponents or neutral fixtures. Tactics can come from 'save', "
                    "'local_fmf' (filename/path), or 'fm_arena' (thread URL). Always report "
                    "verified count and validation_scope honestly."),
                "parameters": _schema({
                    "tactic_b_kind": _string("'local_fmf', 'fm_arena', or 'save'"),
                    "tactic_b_ref": _string("Filename/path for local_fmf, URL for fm_arena, or slot index for save"),
                    "tactic_a_kind": _string("Optional baseline Tactic A kind ('save', 'local_fmf', 'fm_arena') for A/B test"),
                    "tactic_a_ref": _string("Optional baseline Tactic A filename/path, URL, or slot index"),
                    "mode": _string("'league' (requires loaded save) or 'quick'"),
                    "opponent_count": _integer("Number of league opponents, default 4"),
                    "matches_per_opponent": _integer("Even number of home+away matches per opponent, default 2"),
                    "wait": _boolean("Wait for short tests to finish (up to 90s) and return verified results"),
                }, ("tactic_b_kind",)),
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
                 default_club: str = "") -> dict[str, Any]:
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
            return training_week(str(args.get("focus") or "Balanced"),
                                 str(args.get("name") or ""),
                                 str(args.get("match_day") or ""))
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
        if tool == "fm27_lab_inspect_fmf":
            return _lab_inspect_fmf(str(args.get("path") or ""))
        if tool == "fm27_lab_load_save":
            return _lab_load_save(
                str(args.get("save_path") or ""),
                club() or default_club,
                as_int(args.get("tactic_index"), 0),
            )
        if tool == "fm27_lab_run_ab_test":
            return _lab_run_ab_test(
                tactic_b_kind=str(args.get("tactic_b_kind") or "save"),
                tactic_b_ref=str(args.get("tactic_b_ref") or ""),
                tactic_a_kind=str(args.get("tactic_a_kind") or ""),
                tactic_a_ref=str(args.get("tactic_a_ref") or ""),
                mode=str(args.get("mode") or "league"),
                opponent_count=as_int(args.get("opponent_count"), 4),
                matches_per_opponent=as_int(args.get("matches_per_opponent"), 2),
                wait=bool(args.get("wait", True)),
            )
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


def _lab_load_save(save_path: str, club_hint: str, tactic_index: int) -> dict[str, Any]:
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
    )


def _lab_run_ab_test(*, tactic_b_kind: str, tactic_b_ref: str,
                     tactic_a_kind: str, tactic_a_ref: str,
                     mode: str, opponent_count: int,
                     matches_per_opponent: int, wait: bool) -> dict[str, Any]:
    import time
    client = _get_lab_client()
    if not client.installed:
        return _lab_missing_response()
    tb = _tactic_spec(tactic_b_kind, tactic_b_ref)
    ta = _tactic_spec(tactic_a_kind, tactic_a_ref) if tactic_a_kind.strip() else None
    started = client.start_ab_test(
        tactic_b=tb,
        tactic_a=ta,
        mode=mode,
        opponent_count=opponent_count,
        matches_per_opponent=matches_per_opponent,
        event_details=False,
    )
    if wait and started.get("job_id"):
        jid = started["job_id"]
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            jobs = client.list_jobs()
            cur = next((j for j in jobs if j.get("id") == jid), None)
            if cur and cur.get("status") not in ("queued", "running"):
                started["result"] = cur
                return started
            time.sleep(1.5)
    return started
