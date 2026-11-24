"""Finding and describing one entity in the loaded save.

Three lookups and one list, all of them literal:

* ``search_players`` - the find-a-player tool: every filter the data layer can
  apply, then the tool's own rule that *all* the words of the query appear in
  the name.  The longest word of the query goes *into* the data layer's name
  filter, so the tool is not limited to the highest-ability slice of the save.
* ``player_detail`` - everything about one player, including his best role and
  the role's 16 / 4 attribute breakdown.
* ``find_clubs`` - clubs by name, nation or competition.
* ``fixtures`` - a club's fixtures from the game date onwards.

Every row comes from ``fmrte.db.Snapshot``'s read interface, never from its
connection, so the live-memory overlay is applied by the data layer and this
module only decides what to show.

Nothing here imports ``tkinter``, ``sqlite3`` or ``ctypes``.
"""

from __future__ import annotations

from typing import Any

from . import ratings as R
from .fmroles import (LABEL_POSITIONS, LABEL_TO_GENERAL, as_int, attr_value,
                      condition_percent, position_score, position_text,
                      role_display, role_fit, sharpness_percent, short_label)
from .fmsquad import asking_price_of, weekly_salary_of

# The rows a name search is allowed to look at before its word filter runs.
# It is a *bound on the reading*, not the way the search works: the longest word
# of the query is pushed into the data layer's own ``name`` filter, so a player
# outside the top few thousand by ability is still reachable.  Only a query with
# no word that can be pushed safely (see ``_pushdown_word``) falls back to this
# scan alone.
SEARCH_SCAN_LIMIT = 4000


def _pushdown_word(words: list[str]) -> str | None:
    """The word the data layer can filter on, or None when none is safe.

    SQLite's ``LIKE`` is case-insensitive for ASCII only, so an ASCII word
    matches exactly the rows the Python pass below would accept and no row can
    be lost.  A word carrying non-ASCII letters would be compared case
    sensitively by the data layer and could drop a row the Python pass accepts,
    so such a query keeps the bounded scan instead of narrowing wrongly.
    """
    usable = [word for word in words if word.isascii()]
    return max(usable, key=len) if usable else None


def search_players(db: Any, query: str = "", club: str = "",
                   min_age: int | None = None, max_age: int | None = None,
                   min_ca: int | None = None, max_ca: int | None = None,
                   nationality: str = "", limit: int = 15) -> dict[str, Any]:
    cap = max(1, min(40, int(limit or 15)))
    words = [word for word in (query or "").split() if word]
    pushdown = _pushdown_word(words)
    try:
        rows = list(db.query_players(
            name=pushdown, club=club or None,
            nationality=nationality or None,
            min_age=None if min_age is None else int(min_age),
            max_age=None if max_age is None else int(max_age),
            min_ca=None if min_ca is None else int(min_ca),
            max_ca=None if max_ca is None else int(max_ca),
            exclude_free_agents=True, order="ca", descending=True,
            limit=SEARCH_SCAN_LIMIT))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"the player table could not be queried: {exc}"}
    lowered = [word.lower() for word in words]
    matched = []
    for player in rows:
        name = str(player.get("name") or "").lower()
        if lowered and not all(word in name for word in lowered):
            continue
        matched.append(player)
    matched.sort(key=lambda player: (-as_int(player.get("ca"), 0),
                                     as_int(player.get("unique_id"), 0)))
    return {
        "query": query,
        "club": club or None,
        "count": min(cap, len(matched)),
        "examined": len(rows),
        "players": [
            {
                "player_unique_id": as_int(player.get("unique_id"), 0),
                "player_name": player.get("name"),
                "club": player.get("club"),
                "age": as_int(player.get("age"), 0),
                "nationality": player.get("nationality"),
                "position": position_text(player),
                "ca": as_int(player.get("ca"), 0),
                "pa": as_int(player.get("pa"), 0),
                "best_role": role_display(R.role_for(player)[1]),
                "role_rating": R.role_rating(player),
                "asking_price": asking_price_of(player),
            }
            for player in matched[:cap]
        ],
    }


def player_detail(db: Any, unique_id: int) -> dict[str, Any]:
    try:
        player = db.player(int(unique_id))
    except Exception:  # noqa: BLE001
        player = None
    if not player:
        return {"error": f"player {unique_id} is not in the snapshot"}
    general, role, duty = R.role_for(player)
    fit = role_fit(player, role, duty, short_label(
        next((label for label, position in LABEL_POSITIONS.items()
              if LABEL_TO_GENERAL.get(label) == general), "MC")))
    attributes = {
        column: attr_value(player, column) for column in R.ATTRIBUTE_COLUMNS
        if player.get(column) is not None
    }
    return {
        "player_unique_id": as_int(player.get("unique_id"), 0),
        "name": player.get("name"),
        "club": player.get("club"),
        "age": as_int(player.get("age"), 0),
        "nationality": player.get("nationality"),
        "height_cm": as_int(player.get("height_cm"), 0),
        "ca": as_int(player.get("ca"), 0),
        "pa": as_int(player.get("pa"), 0),
        "best_general_position": general,
        "best_role": role_display(role),
        "best_duty": duty,
        "role_rating": R.role_rating(player),
        "key_rating": R.key_rating(player),
        "weighted_rating": R.weighted_rating(player),
        "role_fit": fit.to_map(),
        "positions": {label: position_score(player, position)
                      for label, position in LABEL_POSITIONS.items()},
        "condition_percent": condition_percent(player),
        "sharpness_percent": sharpness_percent(player),
        "morale": as_int(player.get("morale"), 0),
        "jadedness": as_int(player.get("jadedness"), 0),
        "injured": bool(player.get("injured")),
        "injury": player.get("injury"),
        "transfer_listed": bool(player.get("transfer_listed")),
        "listed_for_loan": bool(player.get("listed_for_loan")),
        "asking_price": asking_price_of(player),
        "weekly_salary": weekly_salary_of(player) or None,
        "contract_end_date": player.get("contract_end_date"),
        "attributes": attributes,
        "guidance": (
            "role_rating blends CA and the best role's 16/4 attribute score, so "
            "the same player scores differently in another role."),
    }


def find_clubs(db: Any, name: str = "", nation: str = "",
               competition: str = "", limit: int = 12) -> dict[str, Any]:
    cap = max(1, min(40, int(limit or 12)))
    try:
        found = [dict(row) for row in db.clubs(name, 400) or []]
    except Exception as exc:  # noqa: BLE001
        return {"error": f"the club table could not be read: {exc}"}
    out = []
    for club in found:
        if nation and str(club.get("nation") or "").lower() != nation.lower():
            continue
        if competition and str(club.get("competition") or "").lower() != competition.lower():
            continue
        out.append({
            "club_id": as_int(club.get("id"), 0),
            "name": club.get("name"),
            "nation": club.get("nation"),
            "competition": club.get("competition"),
            "reputation": as_int(club.get("reputation"), 0),
            "balance": as_int(club.get("balance"), 0),
            "transfer_budget": as_int(club.get("transfer_budget"), 0),
            "training_facilities": as_int(club.get("training_facilities"), 0),
            "youth_facilities": as_int(club.get("youth_facilities"), 0),
        })
    out.sort(key=lambda row: -as_int(row.get("reputation"), 0))
    return {"count": min(cap, len(out)), "clubs": out[:cap]}


def fixtures(db: Any, club_name: str, limit: int = 8) -> dict[str, Any]:
    club = club_name
    try:
        found = db.club_by_name(club_name)
        if found:
            club = found.get("name") or club_name
    except Exception:  # noqa: BLE001
        pass
    rows = []
    try:
        for row in db.fixtures(club, max(1, min(40, int(limit)))) or []:
            rows.append(dict(row))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"the fixture list could not be read: {exc}"}
    meta = db.meta() if hasattr(db, "meta") else {}
    return {
        "club": club,
        "game_date": meta.get("game_date"),
        "count": len(rows),
        "fixtures": rows,
        "note": "Only fixtures at or after the game date are listed.",
    }
