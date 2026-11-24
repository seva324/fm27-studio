"""The FM27 transfer rules: a literal shortlist and a replacement finder.

The order is the point, and it is the same for both tools:

1. **A literal hard filter first** - position, price, wage, age, ability,
   listed status.  A candidate who fails one is gone, not penalised.
2. **Then a decision score** over what survived, which rewards the fit and the
   lack of displacement; an unknown price is a risk, never a bargain.

Every candidate row comes from ``fmrte.db.Snapshot.query_players``, so the
position floor, the age/ability bounds and the live-memory overlay are the data
layer's job rather than this module's SQL.

Nothing here imports ``tkinter``, ``sqlite3`` or ``ctypes``.
"""

from __future__ import annotations

from typing import Any

from . import spec
from .fmroles import (DEFAULT_MINIMUM_POSITION, FitCache, as_int, attr_value,
                      condition_percent, position_field, position_score,
                      role_display, round1, sharpness_percent, short_label,
                      unavailable_reason)
from .fmsquad import asking_price_of, weekly_salary_of

# The rows the hard filter is allowed to look at before the scoring pass.  The
# filter itself is pushed into the data layer, so this only bounds the reading.
SHORTLIST_SCAN_LIMIT = 20000


def _candidate_rows(db: Any, position: str, minimum: int,
                    max_age: int | None, min_ca: int | None,
                    min_pa: int | None, transfer_listed: bool | None,
                    injured: bool | None) -> list[dict[str, Any]]:
    """The candidate pool: every filter the data layer can apply, applied."""
    return list(db.query_players(
        position=position, position_min=minimum,
        max_age=None if max_age is None else int(max_age),
        min_ca=None if min_ca is None else int(min_ca),
        min_pa=None if min_pa is None else int(min_pa),
        transfer_listed=transfer_listed, injured=injured,
        exclude_free_agents=True, order="ca", descending=True,
        limit=SHORTLIST_SCAN_LIMIT))


def transfer_shortlist(
        db: Any,
        position: str,
        excluding_club: str = "",
        max_age: int | None = None,
        min_ca: int | None = None,
        min_pa: int | None = None,
        max_asking_price: int | None = None,
        max_weekly_salary: int | None = None,
        minimum_position_score: int = DEFAULT_MINIMUM_POSITION,
        transfer_listed: bool | None = None,
        injured: bool | None = None,
        minimum_attributes: dict[str, int] | None = None,
        limit: int = 8) -> dict[str, Any]:
    """Literal hard filters first, then a decision score over what survives."""
    minimum = max(1, min(20, int(minimum_position_score)))
    cap = max(1, min(30, int(limit or 8)))
    if position_field(position) is None:
        return {"error": f"{position} is not a position this build rates"}
    try:
        rows = _candidate_rows(db, position, minimum, max_age, min_ca, min_pa,
                               transfer_listed, injured)
    except Exception as exc:  # noqa: BLE001 - a bad filter must not crash a turn
        return {"error": f"the player table could not be queried: {exc}"}

    wanted_club = (excluding_club or "").strip().lower()
    candidates: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for player in rows:
        club = str(player.get("club") or "").strip().lower()
        if wanted_club and club == wanted_club:
            continue
        here = position_score(player, position)
        if here < minimum:
            dropped["position"] = dropped.get("position", 0) + 1
            continue
        if max_asking_price is not None:
            price = asking_price_of(player)
            if price is not None and price > int(max_asking_price):
                dropped["price"] = dropped.get("price", 0) + 1
                continue
        if max_weekly_salary is not None:
            wage = weekly_salary_of(player)
            if wage and wage > int(max_weekly_salary):
                dropped["wage"] = dropped.get("wage", 0) + 1
                continue
        if minimum_attributes:
            failed = [name for name, floor in minimum_attributes.items()
                      if attr_value(player, str(name).lower()) < int(floor)]
            if failed:
                dropped["attributes"] = dropped.get("attributes", 0) + 1
                continue
        reason = unavailable_reason(player)
        if reason:
            dropped["unavailable"] = dropped.get("unavailable", 0) + 1
            continue
        candidates.append(player)

    # One memo for this call: the pool is scored once, and nothing survives the
    # return, so a player edited between two calls is scored afresh.
    cache = FitCache()
    scored: list[tuple[float, dict[str, Any]]] = []
    for player in candidates:
        fit = cache.slot_fit(player, 1, position)
        value = fit.overall
        if max_asking_price is not None:
            price = asking_price_of(player)
            if price is None:
                value -= 2.0  # an unknown price is a risk, never a bargain
            elif price > 0:
                value += min(6.0, 6.0 * (1.0 - min(1.0, price / max(1, int(max_asking_price)))))
        score = round1(value)
        scored.append((score, {"player": player, "fit": fit}))
    scored.sort(key=lambda item: (-item[0], as_int(item[1]["player"].get("unique_id"), 0)))

    results = []
    for score, item in scored[:cap]:
        player = item["player"]
        fit = item["fit"]
        price = asking_price_of(player)
        wage = weekly_salary_of(player)
        results.append({
            "player_unique_id": as_int(player.get("unique_id"), 0),
            "player_name": player.get("name"),
            "club": player.get("club"),
            "age": as_int(player.get("age"), 0),
            "nationality": player.get("nationality"),
            "ca": as_int(player.get("ca"), 0),
            "pa": as_int(player.get("pa"), 0),
            "position": short_label(position),
            "position_score": fit.position_score,
            "role": role_display(fit.role) if fit.role else None,
            "role_score": fit.role_score,
            "fit_score": fit.overall,
            "decision_score": score,
            "asking_price": price,
            "asking_price_known": price is not None,
            "weekly_salary": wage or None,
            "contract_end_date": player.get("contract_end_date"),
            "injured": bool(player.get("injured")),
            "transfer_listed": bool(player.get("transfer_listed")),
            "listed_for_loan": bool(player.get("listed_for_loan")),
            "condition_percent": condition_percent(player),
            "sharpness_percent": sharpness_percent(player),
        })

    criteria: dict[str, Any] = {
        "position": short_label(position),
        "minimum_position_score": minimum,
        "excluding_club": excluding_club or None,
        "max_age": max_age,
        "min_ca": min_ca,
        "min_pa": min_pa,
        "max_asking_price": max_asking_price,
        "max_weekly_salary": max_weekly_salary,
        "transfer_listed": transfer_listed,
        "injured": injured,
        "minimum_attributes": minimum_attributes or None,
    }
    return {
        "criteria": criteria,
        "examined": len(rows),
        "eligible": len(candidates),
        "dropped_by_filter": dropped,
        "count": len(results),
        "candidates": results,
        "guidance": (
            "asking_price null means the price is not known: treat it as unknown "
            "rather than free. A high fit_score with a low decision_score usually "
            "means the price or the wage is the problem, not the player."),
    }


def find_replacements(db: Any, reference_unique_id: int,
                      position: str = "", excluding_club: str = "",
                      limit: int = 6, max_age: int | None = None,
                      max_asking_price: int | None = None) -> dict[str, Any]:
    """Candidates who would do the reference player's job, position first."""
    try:
        reference = db.player(int(reference_unique_id))
    except Exception:  # noqa: BLE001
        reference = None
    if not reference:
        return {"error": f"player {reference_unique_id} is not in the snapshot"}
    club = excluding_club or str(reference.get("club") or "")
    if not position:
        text = spec.position_text(reference)
        position = text.split("/")[0] if text and text != "—" else "MC"
    shortlist = transfer_shortlist(
        db, position, excluding_club=club, max_age=max_age,
        max_asking_price=max_asking_price,
        minimum_position_score=max(1, position_score(reference, position) - 1),
        limit=limit)
    if shortlist.get("error"):
        return shortlist
    reference_fit = FitCache().slot_fit(reference, 1, position)
    for row in shortlist.get("candidates", []):
        row["replaces"] = {
            "player_unique_id": as_int(reference.get("unique_id"), 0),
            "player_name": reference.get("name"),
            "position_score": reference_fit.position_score,
            "role": role_display(reference_fit.role) if reference_fit.role else None,
            "role_score": reference_fit.role_score,
        }
        row["position_gain"] = round1(
            float(row.get("position_score") or 0) - reference_fit.position_score)
    shortlist["reference"] = {
        "player_unique_id": as_int(reference.get("unique_id"), 0),
        "player_name": reference.get("name"),
        "club": reference.get("club"),
        "age": as_int(reference.get("age"), 0),
        "position": short_label(position),
        "position_score": reference_fit.position_score,
        "role": role_display(reference_fit.role) if reference_fit.role else None,
        "role_score": reference_fit.role_score,
    }
    return shortlist
