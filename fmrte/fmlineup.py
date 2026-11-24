"""The FM27 lineup optimizer: constraints, the XI and its alternatives.

One player can fill only one slot, locks are honoured, injured and on-duty
players are excluded unless asked for, and the result is improved by local
search so it is not just a greedy first pass.

The rule this module hides is *how an eleven is chosen*: it reads no database,
holds no state, and every role/fitness rule it uses comes from ``fmroles``.
Everything it needs about the current call arrives in a :class:`FitCache`, so
two calls can never share one.

Nothing here imports ``tkinter``, ``sqlite3``, ``ctypes`` or the data layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .fmroles import (DEFAULT_MINIMUM_POSITION, FitCache, SlotFit, as_int,
                      condition_percent, fitness_score, position_score,
                      round1, sharpness_percent, short_label, truthy,
                      unavailable_reason)


@dataclass
class Constraints:
    minimum_position_score: int = DEFAULT_MINIMUM_POSITION
    include_injured: bool = False
    include_on_duty: bool = False
    unavailable_ids: frozenset[int] = field(default_factory=frozenset)
    locked: dict[int, int] = field(default_factory=dict)
    alternative_limit: int = 3
    fitness_weight: int = 1

    def normalised(self) -> "Constraints":
        return Constraints(
            minimum_position_score=max(1, min(20, int(self.minimum_position_score))),
            include_injured=bool(self.include_injured),
            include_on_duty=bool(self.include_on_duty),
            unavailable_ids=frozenset(self.unavailable_ids),
            locked=dict(self.locked),
            alternative_limit=max(1, min(10, int(self.alternative_limit))),
            fitness_weight=max(0, min(5, int(self.fitness_weight))),
        )


@dataclass
class Assignment:
    slot: int
    position: str
    player: dict[str, Any]
    fit: SlotFit
    locked: bool = False

    def to_map(self) -> dict[str, Any]:
        out = self.fit.to_map()
        out["player_unique_id"] = as_int(self.player.get("unique_id"), 0)
        out["player_name"] = self.player.get("name")
        out["age"] = as_int(self.player.get("age"), 0)
        out["ca"] = as_int(self.player.get("ca"), 0)
        out["condition_percent"] = condition_percent(self.player)
        out["sharpness_percent"] = sharpness_percent(self.player)
        out["locked"] = self.locked
        return out


def slot_score(player: dict[str, Any], slot: int, position: str,
               constraints: Constraints, cache: FitCache) -> float:
    """The ranking value the optimizer maximises for one player and slot."""
    fit = cache.slot_fit(player, slot, position)
    score = fit.position_score * 100.0
    if fit.role_score is not None:
        score += fit.role_score * 6.0
    else:
        score += fit.position_score * 6.0
    score += fitness_score(player) * constraints.fitness_weight
    if player.get("injured"):
        days = as_int(player.get("injury_max_days_remaining"), 0)
        # An injury costs more than any fitness difference, but a squad can be
        # short enough that the only natural option is injured, so it does not
        # make the player unselectable.
        score -= min(600.0, 200.0 + days * 10.0)
    return score


def eligible(players: Iterable[dict[str, Any]],
             constraints: Constraints) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for player in players:
        unique_id = as_int(player.get("unique_id"), 0)
        if not unique_id or unique_id in constraints.unavailable_ids:
            continue
        if not constraints.include_injured and player.get("injured"):
            continue
        if not constraints.include_on_duty and truthy(player.get("on_duty")):
            continue
        if unavailable_reason(player):
            continue
        out.append(player)
    out.sort(key=lambda row: as_int(row.get("unique_id"), 0))
    return out


def assign_greedy(players: list[dict[str, Any]],
                  slots: Sequence[tuple[int, str]],
                  constraints: Constraints,
                  cache: FitCache) -> dict[int, Assignment]:
    """Best marginal gain first, which is a strong baseline for eleven slots."""
    pairs: list[tuple[float, int, int, int]] = []
    for player in players:
        unique_id = as_int(player.get("unique_id"), 0)
        for slot, position in slots:
            here = position_score(player, position)
            if here < constraints.minimum_position_score:
                continue
            pairs.append((slot_score(player, slot, position, constraints, cache),
                          -unique_id, slot, unique_id))
    pairs.sort(reverse=True)
    by_id = {as_int(player.get("unique_id"), 0): player for player in players}
    positions = dict(slots)
    taken_players: set[int] = set()
    filled: set[int] = set()
    out: dict[int, Assignment] = {}
    for _score, _neg, slot, unique_id in pairs:
        if slot in filled or unique_id in taken_players:
            continue
        player = by_id.get(unique_id)
        if player is None:
            continue
        filled.add(slot)
        taken_players.add(unique_id)
        out[slot] = Assignment(slot, positions[slot], player,
                               cache.slot_fit(player, slot, positions[slot]))
    return out


def improve(players: list[dict[str, Any]],
            all_slots: Sequence[tuple[int, str]],
            movable: Sequence[tuple[int, str]],
            constraints: Constraints,
            current: dict[int, Assignment],
            cache: FitCache,
            rounds: int = 40) -> dict[int, Assignment]:
    """Swap and reassign until no single change improves the total."""
    positions = dict(all_slots)
    movable_slots = [slot for slot, _position in movable]
    by_id = {as_int(player.get("unique_id"), 0): player for player in players}
    best = dict(current)

    def total(assignment: dict[int, Assignment]) -> float:
        return sum(slot_score(item.player, item.slot, item.position,
                              constraints, cache)
                   for item in assignment.values())

    best_total = total(best)
    for _round in range(rounds):
        improved = False
        filled = [slot for slot in movable_slots if slot in best]
        # 1. Reassign one slot to an unused player.
        used = {as_int(item.player.get("unique_id"), 0) for item in best.values()}
        for slot in filled:
            for unique_id, player in by_id.items():
                if unique_id in used:
                    continue
                if position_score(player, positions[slot]) < constraints.minimum_position_score:
                    continue
                trial = dict(best)
                trial[slot] = Assignment(slot, positions[slot], player,
                                         cache.slot_fit(player, slot, positions[slot]))
                value = total(trial)
                if value > best_total + 1e-9:
                    best, best_total, improved = trial, value, True
                    break
            if improved:
                break
        if improved:
            continue
        # 2. Swap the players of two filled slots.
        for index, left in enumerate(filled):
            for right in filled[index + 1:]:
                left_player = best[left].player
                right_player = best[right].player
                if (position_score(right_player, positions[left])
                        < constraints.minimum_position_score
                        or position_score(left_player, positions[right])
                        < constraints.minimum_position_score):
                    continue
                trial = dict(best)
                trial[left] = Assignment(left, positions[left], right_player,
                                         cache.slot_fit(right_player, left, positions[left]))
                trial[right] = Assignment(right, positions[right], left_player,
                                          cache.slot_fit(left_player, right, positions[right]))
                value = total(trial)
                if value > best_total + 1e-9:
                    best, best_total, improved = trial, value, True
                    break
            if improved:
                break
        if not improved:
            break
    return best


def optimize_lineup(players: Sequence[dict[str, Any]],
                    positions_in_order: Sequence[str],
                    constraints: Constraints | None = None
                    ) -> dict[str, Any]:
    """The best unique XI for a tactic shape, plus alternatives per slot."""
    safe = (constraints or Constraints()).normalised()
    # One memo for this call only: it is dropped when the function returns, so
    # a player the caller mutated after the previous call is scored afresh.
    cache = FitCache()
    shape = list(positions_in_order) or []
    if not shape:
        return {"error": "no tactic shape was given"}
    if len(shape) > 20:
        return {"error": "the optimizer supports at most 20 slots"}
    slots = [(index + 1, position) for index, position in enumerate(shape)]
    warnings: list[str] = []

    locked: dict[int, Assignment] = {}
    by_id = {as_int(player.get("unique_id"), 0): player for player in players}
    for slot, unique_id in safe.locked.items():
        if slot < 1 or slot > len(slots):
            warnings.append(f"lock on slot {slot} ignored: the shape has {len(slots)} slots")
            continue
        player = by_id.get(as_int(unique_id, 0))
        if player is None:
            warnings.append(f"lock on slot {slot} ignored: player {unique_id} is not in the pool")
            continue
        here = position_score(player, slots[slot - 1][1])
        if here < safe.minimum_position_score:
            warnings.append(
                f"lock on slot {slot} kept even though {player.get('name')} rates "
                f"{here} there, below the {safe.minimum_position_score} minimum")
        if player.get("injured"):
            warnings.append(f"lock on slot {slot}: {player.get('name')} is injured")
        if unavailable_reason(player):
            warnings.append(
                f"lock on slot {slot}: {player.get('name')} is {unavailable_reason(player)}")
        locked[slot] = Assignment(slot, slots[slot - 1][1], player,
                                  cache.slot_fit(player, slot, slots[slot - 1][1]),
                                  True)

    locked_ids = {as_int(item.player.get("unique_id"), 0) for item in locked.values()}
    pool = [player for player in eligible(players, safe)
            if as_int(player.get("unique_id"), 0) not in locked_ids]
    open_slots = [(slot, position) for slot, position in slots
                  if slot not in locked]

    selected = dict(locked)
    if open_slots and pool:
        selected.update(assign_greedy(pool, open_slots, safe, cache))
        selected = improve(pool, slots, open_slots, safe, selected, cache)

    assignments = [selected[slot] for slot, _position in slots if slot in selected]
    unfilled = [short_label(position) for slot, position in slots
                if slot not in selected]
    total = sum(slot_score(item.player, item.slot, item.position, safe, cache)
                for item in assignments)

    used = {as_int(item.player.get("unique_id"), 0) for item in assignments}
    alternatives: dict[str, list[dict[str, Any]]] = {}
    for slot, position in open_slots:
        rows: list[tuple[float, dict[str, Any]]] = []
        for player in pool:
            unique_id = as_int(player.get("unique_id"), 0)
            if unique_id in used:
                continue
            if position_score(player, position) < safe.minimum_position_score:
                continue
            rows.append((slot_score(player, slot, position, safe, cache), player))
        rows.sort(key=lambda item: (-item[0], as_int(item[1].get("unique_id"), 0)))
        alternatives[str(slot)] = [
            {
                "player_unique_id": as_int(player.get("unique_id"), 0),
                "player_name": player.get("name"),
                "position_score": position_score(player, position),
                "ca": as_int(player.get("ca"), 0),
                "condition_percent": condition_percent(player),
                "sharpness_percent": sharpness_percent(player),
            }
            for _score, player in rows[:safe.alternative_limit]
        ]

    return {
        "shape": "".join(short_label(position) + " " for position in shape).strip(),
        "slots": len(shape),
        "filled": len(assignments),
        "unfilled_slots": unfilled,
        "assignments": [item.to_map() for item in assignments],
        "alternatives": alternatives,
        "team_score": round1(total),
        "average_slot_score": round1(total / len(assignments)) if assignments else 0.0,
        "warnings": warnings,
        "minimum_position_score": safe.minimum_position_score,
        "excluded": {
            "injured": not safe.include_injured,
            "on_duty": not safe.include_on_duty,
        },
        "guidance": (
            "One player fills only one slot. A slot below the position minimum is "
            "left empty rather than filled by an unsuitable player. Run a second "
            "shape to compare systems before committing."),
    }
