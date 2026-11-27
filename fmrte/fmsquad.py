"""Reading a club out of the loaded save: its squad, its depth, its money.

This is the read layer of the FM27 tools.  Every row comes from
``fmrte.db.Snapshot``'s own interface - ``players_for_club`` for a whole squad
(with the live-memory overlay already applied), ``club_by_name``,
``staff``, ``next_fixture`` - so no module here touches the SQLite connection.
The analysis on top of those rows answers three questions: how deep the squad
is by position, what shape it can field, and which contracts are running out.

The money fields live here too, because a player's price and wage are part of
reading him: the snapshot stores a raw number and a display number, and the
squad listing, the search results, the player page and the shortlist must all
prefer the same one.

Nothing here imports ``tkinter``, ``sqlite3`` or ``ctypes``.
"""

from __future__ import annotations

from typing import Any, Sequence

from .fmlineup import Constraints, optimize_lineup
from .fmroles import (DEFAULT_MINIMUM_POSITION, LABEL_POSITIONS, POSITION_LABELS,
                      as_int, condition_percent, position_score,
                      position_text, sharpness_percent)

CONTRACT_HORIZON_MONTHS = 12


def squad_rows(db: Any, club: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every column for a club's players, with the overlays applied.

    The screen's own ``Snapshot.squad`` selects the twenty or so columns a list
    needs; a role fit needs all fifty-four attributes, so the rows are read in
    full through ``players_for_club`` and the same player object is then reused
    by every slot.
    """
    name = (club or "").strip()
    if not name:
        return []
    try:
        rows = db.players_for_club(name, max(1, int(limit)))
    except Exception:  # noqa: BLE001 - a partial data layer must still answer
        return []
    out: list[dict[str, Any]] = []
    name_low = name.lower()
    for row in rows:
        item = dict(row)
        playing = str(item.get("playing_club") or item.get("loan_club") or "").strip().lower()
        own_club = str(item.get("club") or "").strip().lower()
        if playing == name_low and own_club != name_low:
            item["_for_club"] = name
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def asking_price_of(player: dict[str, Any]) -> int | None:
    """What the club wants for the player, or None when that is not known.

    The raw column is the number the game stores; the display column is the
    rounded one the screens paint.  A positive raw value wins, and a zero or
    missing price stays explicitly unknown rather than becoming "free".
    """
    raw = player.get("asking_price_raw")
    if raw is not None:
        value = as_int(raw, 0)
        if value > 0:
            return value
    value = as_int(player.get("asking_price"), 0)
    return value if value > 0 else None


def weekly_salary_of(player: dict[str, Any]) -> int:
    return as_int(player.get("salary_weekly_raw"), 0)


# ---------------------------------------------------------------------------
# Squad analysis
# ---------------------------------------------------------------------------

def position_depth(players: Sequence[dict[str, Any]],
                   minimum: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, position in sorted(LABEL_POSITIONS.items()):
        viable = [player for player in players
                  if position_score(player, position) >= minimum]
        if not viable:
            rows.append({"position": label, "viable": 0, "best": None,
                         "depth_note": "no viable option"})
            continue
        viable.sort(key=lambda player: (-position_score(player, position),
                                        -as_int(player.get("ca"), 0)))
        best = viable[0]
        rows.append({
            "position": label,
            "viable": len(viable),
            "best": best.get("name"),
            "best_score": position_score(best, position),
            "depth_note": ("thin" if len(viable) == 1
                           else "ok" if len(viable) <= 3 else "deep"),
        })
    return rows


def analyze_squad(db: Any, club: str,
                  minimum_position_score: int = DEFAULT_MINIMUM_POSITION,
                  contract_horizon_months: int = CONTRACT_HORIZON_MONTHS,
                  shape: Sequence[str] | None = None) -> dict[str, Any]:
    """Depth, tactic fit, contract risk and the weaknesses worth fixing first."""
    minimum = max(1, min(20, int(minimum_position_score)))
    squad = squad_rows(db, club, 300)
    if not squad:
        return {"error": f"no players are stored for {club}"}
    shape = list(shape or [])
    meta = db.meta() if hasattr(db, "meta") else {}
    game_date = str(meta.get("game_date") or "")
    horizon = f"{game_date[:4]}-{game_date[5:7]}-{game_date[8:10]}" if game_date else ""
    try:
        import datetime
        start = datetime.date.fromisoformat(horizon) if horizon else datetime.date.today()
        cutoff = start + datetime.timedelta(days=30 * int(contract_horizon_months))
        cutoff_text = cutoff.isoformat()
    except Exception:  # noqa: BLE001 - a bad game date must not stop an analysis
        cutoff_text = ""

    depth = position_depth(squad, minimum)
    thin = [row for row in depth if row["viable"] == 0]
    shallow = [row for row in depth if row["viable"] == 1]

    injured = [player.get("name") for player in squad if player.get("injured")]
    expiring = []
    if cutoff_text:
        for player in squad:
            end = str(player.get("contract_end_date") or "")
            if end and end != "—" and end <= cutoff_text:
                expiring.append({
                    "player_unique_id": as_int(player.get("unique_id"), 0),
                    "player_name": player.get("name"),
                    "contract_end_date": end,
                    "age": as_int(player.get("age"), 0),
                })
        expiring.sort(key=lambda row: str(row["contract_end_date"]))

    out: dict[str, Any] = {
        "club": club,
        "squad_size": len(squad),
        "minimum_position_score": minimum,
        "position_depth": depth,
        "no_viable_option": [row["position"] for row in thin],
        "single_option": [row["position"] for row in shallow],
        "injured": injured,
        "contracts_expiring": expiring,
        "contract_horizon_months": int(contract_horizon_months),
    }
    if shape:
        shape_slots = [POSITION_LABELS.get(position, position) for position in shape]
        lineup = optimize_lineup(squad, shape, Constraints(
            minimum_position_score=minimum, include_injured=False,
            include_on_duty=False))
        out["tactic_shape"] = lineup.get("shape")
        out["tactic_fit"] = lineup.get("assignments")
        out["unfilled_slots"] = lineup.get("unfilled_slots")
        out["team_score"] = lineup.get("team_score")
        out["shape_positions"] = shape_slots

    needs: list[str] = []
    for row in thin:
        needs.append(f"{row['position']} has no player rated {minimum} or better")
    for row in shallow:
        needs.append(f"{row['position']} has only {row['best']}")
    if injured:
        needs.append(f"{len(injured)} injured player(s) in the squad")
    out["recruitment_needs"] = needs
    out["guidance"] = (
        "Judge players by the attributes their role uses, not by CA: the match "
        "engine does not read Current Ability. Sharpness and condition outrank "
        "every tactical change, so ask about tired or rusty players before "
        "recommending a system.")
    return out


# ---------------------------------------------------------------------------
# Club context
# ---------------------------------------------------------------------------

def club_context(db: Any, club_name: str, squad_limit: int = 25) -> dict[str, Any]:
    try:
        club = db.club_by_name(club_name)
    except Exception:  # noqa: BLE001
        club = None
    if not club:
        return {"error": f"{club_name} is not in the snapshot"}
    name = club.get("name") or club_name
    squad = squad_rows(db, name, max(1, min(60, int(squad_limit))))
    staff = []
    staff_by_job: dict[str, int] = {}
    try:
        for row in db.staff(name, 300) or []:
            d = dict(row)
            job_lbl = str(d.get("job") or "Staff")
            staff_by_job[job_lbl] = staff_by_job.get(job_lbl, 0) + 1
            staff.append({
                "unique_id": as_int(d.get("unique_id") or d.get("id"), 0),
                "name": d.get("name"),
                "job": job_lbl,
                "age": as_int(d.get("age"), 0),
                "ca": as_int(d.get("ca"), 0),
                "pa": as_int(d.get("pa"), 0),
            })
    except Exception:  # noqa: BLE001
        staff = []
        staff_by_job = {}
    fixture = None
    try:
        fixture = db.next_fixture(name)
    except Exception:  # noqa: BLE001
        fixture = None
    return {
        "club": name,
        "nation": club.get("nation"),
        "competition": club.get("competition"),
        "reputation": as_int(club.get("reputation"), 0),
        "balance": as_int(club.get("balance"), 0),
        "transfer_budget": as_int(club.get("transfer_budget"), 0),
        "payroll_budget": as_int(club.get("payroll_budget"), 0),
        "training_facilities": as_int(club.get("training_facilities"), 0),
        "youth_facilities": as_int(club.get("youth_facilities"), 0),
        "youth_coaching": as_int(club.get("youth_coaching"), 0),
        "youth_recruitment": as_int(club.get("youth_recruitment"), 0),
        "squad": [
            {
                "player_unique_id": as_int(player.get("unique_id"), 0),
                "player_name": player.get("name"),
                "age": as_int(player.get("age"), 0),
                "position": position_text(player),
                "ca": as_int(player.get("ca"), 0),
                "pa": as_int(player.get("pa"), 0),
                "asking_price": asking_price_of(player),
                "weekly_salary": weekly_salary_of(player) or None,
                "contract_end_date": player.get("contract_end_date"),
                "injured": bool(player.get("injured")),
                "condition_percent": condition_percent(player),
                "sharpness_percent": sharpness_percent(player),
            }
            for player in squad
        ],
        "staff_count": len(staff),
        "staff_by_job": staff_by_job,
        "staff": staff[:30],
        "staff_note": (
            f"Showing top {min(30, len(staff))} of {len(staff)} staff; "
            "call fm27_find_staff(club=..., limit=150) to inspect ALL staff members, "
            "live coaching/scouting attributes, and FM star ratings."
        ),
        "next_fixture": dict(fixture) if fixture else None,
        "facility_scale": "FM's own 0-20 scale",
        "money_scale": "raw pounds",
    }
