"""The FM27 role rules: positions, role fit, slot fit and fitness.

These are the rules the FM AI Assistant project uses, in one place and with no
data layer under them:

* **Position vocabulary** - ``spec`` owns the tables; the three names the tool
  layer has always exported (``POSITION_FIELDS``, ``POSITION_LABELS``,
  ``LABEL_TO_GENERAL``) are re-exported here, never copied.
* **Role fit** - every role and duty the player's general position offers is
  scored at the original's 16 / 4 key / preferable attribute weights from
  ``role_definitions.RATINGS``.
* **Slot fit** - a slot's fit blends position competence (35 %), role quality
  (45 %) and Current Ability (20 %), exactly the way
  ``RoleFitService.slotFit`` does.
* **Fitness and availability** - condition, sharpness, and the reasons a
  registered player will not play for the club.

The fit memo lives for exactly one call: :class:`FitCache` is created by the
caller, passed down, and dropped when the call returns.  There is no module
level cache to forget to clear, so a stale fit is unrepresentable.

Nothing here imports ``tkinter``, ``sqlite3``, ``ctypes`` or the data layer.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Sequence

from . import ratings as R
from . import spec

# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

# Template name -> snapshot column, template name -> short label, and short
# label -> general position in the original's role table.  Declared once in
# ``spec``; the names are kept here because the tool layer, the assistant and
# the tests import them from this module.
POSITION_FIELDS: dict[str, str] = spec.POSITION_FIELDS
POSITION_LABELS: dict[str, str] = spec.POSITION_LABELS
LABEL_TO_GENERAL: dict[str, str] = spec.LABEL_TO_GENERAL

# Short label -> template name (the reverse of POSITION_LABELS).
LABEL_POSITIONS: dict[str, str] = {v: k for k, v in POSITION_LABELS.items()}

# The roles each position is normally asked to do, most preferred first.  Only
# roles the original's own file defines for that general position are used, and
# a preference the table does not offer falls through to the best-scoring one.
ROLE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "GK": ("Goalkeeper", "SweeperKeeper"),
    "DR": ("FullBack", "WingBack", "InvertedFullBack"),
    "DL": ("FullBack", "WingBack", "InvertedFullBack"),
    "WBR": ("WingBack", "FullBack", "CompleteWingBack"),
    "WBL": ("WingBack", "FullBack", "CompleteWingBack"),
    "DC": ("CentreBack", "BallPlayingCentreBack", "NoNonsenseCentreBack"),
    "DM": ("DefensiveMidfielder", "Anchor", "DeepLyingPlaymaker"),
    "MC": ("CentralMidfielder", "BoxToBoxMidfielder", "AdvancedPlaymaker"),
    "MR": ("Winger", "InvertedWinger", "WideMidfielder"),
    "ML": ("Winger", "InvertedWinger", "WideMidfielder"),
    "AMR": ("Winger", "InsideForward", "InvertedWinger"),
    "AML": ("Winger", "InsideForward", "InvertedWinger"),
    "AMC": ("AttackingMidfielder", "AdvancedPlaymaker", "ShadowStriker"),
    "ST": ("AdvancedForward", "CompleteForward", "Poacher", "DeepLyingForward"),
}

OUT_ROLE_PREFERENCE: dict[str, tuple[str, ...]] = {
    "GK": ("Goalkeeper", "SweeperKeeper"),
    "DR": ("FullBack", "WingBack", "InvertedFullBack"),
    "DL": ("FullBack", "WingBack", "InvertedFullBack"),
    "WBR": ("WingBack", "FullBack"),
    "WBL": ("WingBack", "FullBack"),
    "DC": ("CentreBack", "BallPlayingCentreBack", "WideCentreBack"),
    "DM": ("DefensiveMidfielder", "Anchor", "DeepLyingPlaymaker"),
    "MC": ("CentralMidfielder", "BoxToBoxMidfielder"),
    "MR": ("Winger", "WideMidfielder", "TrackingWinger"),
    "ML": ("Winger", "WideMidfielder", "TrackingWinger"),
    "AMR": ("Winger", "WideMidfielder", "TrackingWinger"),
    "AML": ("Winger", "WideMidfielder", "TrackingWinger"),
    "AMC": ("AttackingMidfielder", "CentralMidfielder"),
    "ST": ("AdvancedForward", "PressingForward", "DeepLyingForward"),
}

PHASE_IN = "InPossession"
PHASE_OUT = "OutOfPossession"

# A slot fit blends the three things the original's own role panel shows.
POSITION_SHARE = 35.0
ROLE_SHARE = 45.0
ABILITY_SHARE = 20.0

DEFAULT_MINIMUM_POSITION = 15


def position_field(position: str) -> str | None:
    """The snapshot column for a template position name or a short label."""
    name = (position or "").strip()
    if name in POSITION_FIELDS:
        return POSITION_FIELDS[name]
    label = name.upper()
    if label in LABEL_POSITIONS:
        return POSITION_FIELDS[LABEL_POSITIONS[label]]
    lowered = name.lower()
    for key, column in POSITION_FIELDS.items():
        if key.lower() == lowered or column == lowered:
            return column
    return None


def short_label(position: str) -> str:
    name = (position or "").strip()
    if name in POSITION_LABELS:
        return POSITION_LABELS[name]
    return name.upper() if name else "?"


def general_for(position: str) -> str | None:
    return LABEL_TO_GENERAL.get(short_label(position))


def as_int(value: Any, fallback: int = 0) -> int:
    try:
        if value is None:
            return fallback
        return int(value)
    except (TypeError, ValueError):
        return fallback


def attr_value(player: dict[str, Any], column: str) -> int:
    return max(0, min(R.ATTRIBUTE_MAX, as_int(player.get(column), 0)))


def round1(value: float) -> float:
    return round(value * 10.0) / 10.0


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def position_score(player: dict[str, Any], position: str) -> int:
    """The player's stored competence (1..20) in one position."""
    column = position_field(position)
    if not column:
        return 0
    return max(0, min(R.ATTRIBUTE_MAX, as_int(player.get(column), 0)))


def position_text(player: dict[str, Any], minimum: int = 15) -> str:
    """The best three positions a player is competent in, e.g. ``DL/DC/DR``.

    The tool layer's own reading: every position is ranked by the player's
    stored competence and the best three at or above ``minimum`` are printed.
    The screens print ``spec.position_text``'s grouped strip instead; the two
    answer different questions (what he can play versus where he is shown), so
    they stay separate rules.
    """
    ranked = sorted(
        ((position_score(player, position), label)
         for label, position in LABEL_POSITIONS.items()),
        key=lambda item: (-item[0], item[1]))
    best = [label for score, label in ranked if score >= minimum][:3]
    return "/".join(best) if best else "—"


# ---------------------------------------------------------------------------
# Role fit
# ---------------------------------------------------------------------------

def squash(text: str) -> str:
    return "".join(char for char in (text or "").lower() if char.isalnum())


@functools.lru_cache(maxsize=1)
def _role_key_table() -> dict[str, str]:
    """Normalised role name -> the original's own key (built once)."""
    table: dict[str, str] = {}
    for _general, roles in R.RATINGS.items():
        for role in roles:
            table.setdefault(squash(role), role)
            label = R.ROLE_LABEL.get(role) or R._humanise(role)
            table.setdefault(squash(label), role)
    return table


def role_keys() -> dict[str, str]:
    """Normalised role name -> the original's own key."""
    return _role_key_table()


def canonical_role(role: str) -> str | None:
    if not role:
        return None
    return role_keys().get(squash(role))


def role_display(role: str) -> str:
    return R.ROLE_LABEL.get(role) or R._humanise(role)


@dataclass(frozen=True)
class Fit:
    score: float
    available: bool
    strengths: tuple[dict[str, Any], ...] = ()
    gaps: tuple[dict[str, Any], ...] = ()

    def to_map(self) -> dict[str, Any]:
        out: dict[str, Any] = {"score": self.score, "available": self.available}
        if self.strengths:
            out["strengths"] = list(self.strengths)
        if self.gaps:
            out["gaps"] = list(self.gaps)
        return out


def _definition(role_key: str, general: str, phase: str):
    """The role's key/preferable attributes, or None when it is not defined."""
    definitions = R.RATINGS.get(general, {}).get(role_key)
    if not definitions:
        # The role exists but not for this position group: try the phase's own
        # group first, then any group, so a role is never scored from nothing.
        for other in R.RATINGS.values():
            candidate = other.get(role_key)
            if candidate:
                definitions = candidate
                break
    if not definitions:
        return None
    return definitions.get(phase) or definitions.get(PHASE_IN) or next(
        iter(definitions.values()))


def role_fit(player: dict[str, Any], role: str, phase: str,
             position: str) -> Fit:
    """One role's 16 / 4 weighted score on a 0..20 scale, with extremes named."""
    key = canonical_role(role)
    if not key:
        return Fit(0.0, False)
    general = general_for(position)
    if not general:
        return Fit(0.0, False)
    definition = _definition(key, general, phase)
    if not definition:
        return Fit(0.0, False)
    key_attributes, preferable = definition
    kw = R.key_weight()
    pw = R.preferable_weight()
    key_sum = sum(attr_value(player, column) for column in key_attributes)
    pref_sum = sum(attr_value(player, column) for column in preferable)
    maximum = len(key_attributes) * kw + len(preferable) * pw
    if not maximum:
        return Fit(0.0, False)
    ratio = (key_sum / R.ATTRIBUTE_MAX * kw
             + pref_sum / R.ATTRIBUTE_MAX * pw) / maximum
    scored = [(attr_value(player, column), column,
               "key" if index < len(key_attributes) else "preferable")
              for index, column in enumerate(
                  tuple(key_attributes) + tuple(preferable))]
    ordered = sorted(scored, key=lambda row: (-row[0], row[1]))
    strengths = tuple({"attribute": column, "value": value, "weight": kind}
                      for value, column, kind in ordered[:3])
    gaps = tuple({"attribute": column, "value": value, "weight": kind}
                 for value, column, kind in ordered[-3:][::-1])
    return Fit(round1(ratio * R.ATTRIBUTE_MAX), True, strengths, gaps)


def best_role_for(player: dict[str, Any], position: str, phase: str,
                  preferred: Sequence[str] = ()) -> tuple[str | None, Fit]:
    """The most preferred role the position offers, else the best-scoring one."""
    general = general_for(position)
    if not general:
        return None, Fit(0.0, False)
    offered = list((R.RATINGS.get(general) or {}).keys())
    if not offered:
        return None, Fit(0.0, False)
    scores: dict[str, Fit] = {
        role: role_fit(player, role, phase, position) for role in offered}
    for wanted in preferred:
        key = canonical_role(wanted)
        if key and key in scores and scores[key].available:
            return key, scores[key]
    best = max(
        ((role, fit) for role, fit in scores.items() if fit.available),
        key=lambda item: (item[1].score, item[0]), default=(None, Fit(0.0, False)))
    return best


@dataclass(frozen=True)
class SlotFit:
    slot: int
    position: str
    position_score: float
    role_score: float | None
    overall: float
    viable: bool
    role: str | None = None
    duty: str | None = None

    def to_map(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "position": short_label(self.position),
            "position_score": self.position_score,
            "role": role_display(self.role) if self.role else None,
            "role_score": self.role_score,
            "overall": self.overall,
            "viable": self.viable,
        }


def _fit_core(player: dict[str, Any], position: str
              ) -> tuple[float, float | None, float, str | None]:
    """Position competence, the best in-possession role, and the blend."""
    here = float(position_score(player, position))
    role, fit = best_role_for(player, position, PHASE_IN)
    role_score = fit.score if fit.available else None
    ability = as_int(player.get("ca"), 0)
    overall = (here / R.ATTRIBUTE_MAX * POSITION_SHARE
               + (role_score / R.ATTRIBUTE_MAX if role_score is not None
                  else here / R.ATTRIBUTE_MAX) * ROLE_SHARE
               + min(1.0, ability / R.ABILITY_MAX) * ABILITY_SHARE)
    return (round1(here),
            round1(role_score) if role_score is not None else None,
            round1(overall), role)


class FitCache:
    """Role-fit memo for exactly one call.

    The old cache was a module-level dict that every entry point had to
    remember to clear, so a caller that forgot read the attributes the player
    had last time.  A cache that is created by the call, handed to the helpers
    that need it, and discarded when the call returns cannot outlive the
    player state it describes.

    A slot fit depends only on the player and the position, never on the slot
    number, so one entry serves every slot of the same position.  A player
    without a ``unique_id`` is scored but not stored, because two such rows
    cannot be told apart.
    """

    __slots__ = ("_core",)

    def __init__(self) -> None:
        self._core: dict[tuple[int, str], tuple[float, float | None, float,
                                               str | None]] = {}

    def cached(self, player: dict[str, Any], position: str
               ) -> tuple[float, float | None, float, str | None]:
        unique_id = as_int(player.get("unique_id"), 0)
        if unique_id:
            key = (unique_id, str(position))
            hit = self._core.get(key)
            if hit is not None:
                return hit
            value = _fit_core(player, position)
            self._core[key] = value
            return value
        return _fit_core(player, position)

    def slot_fit(self, player: dict[str, Any], slot: int, position: str,
                 phase_roles: Sequence[tuple[str, str]] = ()) -> SlotFit:
        return _slot_fit(self, player, slot, position, phase_roles)


def _slot_fit(cache: FitCache, player: dict[str, Any], slot: int,
              position: str,
              phase_roles: Sequence[tuple[str, str]] = ()) -> SlotFit:
    here, role_score, overall, role = cache.cached(player, position)
    if phase_roles:
        preferred = [name for name, _phase in phase_roles]
        wanted, wanted_fit = best_role_for(player, position, PHASE_IN, preferred)
        if wanted and wanted_fit.available:
            role = wanted
            role_score = round1(wanted_fit.score)
            ability = as_int(player.get("ca"), 0)
            overall = round1(
                here / R.ATTRIBUTE_MAX * POSITION_SHARE
                + role_score / R.ATTRIBUTE_MAX * ROLE_SHARE
                + min(1.0, ability / R.ABILITY_MAX) * ABILITY_SHARE)
    return SlotFit(slot, position, here, role_score, overall,
                   here >= 1.0, role, PHASE_IN if role else None)


def slot_fit(player: dict[str, Any], slot: int, position: str,
             phase_roles: Sequence[tuple[str, str]] = ()) -> SlotFit:
    """How well one player fills one tactic slot.

    The role is chosen on the player's own attributes, so the score describes
    the pair rather than the position alone, which is the whole point of the
    role-before-rating rule. An explicit ``phase_roles`` preference wins over
    the automatic choice, the way the planner's own table does.

    This one-shot form keeps no memo; a caller that scores many pairs - the
    lineup optimizer, a shortlist - holds a :class:`FitCache` and calls
    ``cache.slot_fit`` instead, so the memo is bounded by that call.
    """
    return FitCache().slot_fit(player, slot, position, phase_roles)


# ---------------------------------------------------------------------------
# Fitness and availability
# ---------------------------------------------------------------------------

def condition_percent(player: dict[str, Any]) -> int:
    """The stored 0..10000 condition as the 0..100 the screens show."""
    return max(0, min(100, as_int(player.get("condition"), 0) // 100))


def sharpness_percent(player: dict[str, Any]) -> int:
    return max(0, min(100, as_int(player.get("match_sharpness"), 0) // 100))


def fitness_score(player: dict[str, Any]) -> int:
    """Condition and sharpness averaged, the way the planner weighs them."""
    return (condition_percent(player) + sharpness_percent(player)) // 2


def unavailable_reason(player: dict[str, Any], for_club: str = "") -> str | None:
    """Why a player cannot be picked for this club, or None when they can.

    Loaned-out players and players whose transfer is already agreed are
    registered at the club but will not play for it, so naming them would be
    wrong even though the squad list holds them. Players loaned *in* to
    ``for_club`` (where ``playing_club`` matches ``for_club``) ARE available.
    """
    target_club = (for_club or str(player.get("_for_club") or "")).strip().lower()
    playing = str(player.get("playing_club") or "").strip()
    loan_to = str(player.get("loan_club") or "").strip()
    club = str(player.get("club") or "").strip()
    if target_club and (
        (playing and playing.lower() == target_club)
        or (loan_to and loan_to.lower() == target_club)
    ):
        # On loan IN to target_club -> available for target_club
        if truthy(player.get("transfer_agreed")):
            to = player.get("future_transfer_club")
            return f"transfer agreed with {to}" if to else "transfer agreed"
        return None
    if truthy(player.get("is_loaned_out")):
        at = playing or loan_to
        return f"loaned out to {at}" if at else "loaned out"
    if playing and club and playing.lower() != club.lower():
        return f"playing for {playing}"
    if truthy(player.get("transfer_agreed")):
        to = player.get("future_transfer_club")
        return f"transfer agreed with {to}" if to else "transfer agreed"
    return None


# ---------------------------------------------------------------------------
# What a role asks for
# ---------------------------------------------------------------------------

def role_attributes(position: str, role: str = "", phase: str = PHASE_IN
                    ) -> dict[str, Any]:
    """The attributes a role asks for, with the original's own weights."""
    general = general_for(position)
    if not general:
        return {"error": f"{position} is not a position this build rates"}
    roles = R.RATINGS.get(general) or {}
    if not roles:
        return {"error": f"no roles are defined for {general}"}
    key_name = canonical_role(role) if role else None
    if role and key_name not in roles:
        return {
            "position": short_label(position),
            "available_roles": [role_display(name) for name in sorted(roles)],
            "error": f"{role} is not a role {general} offers",
        }
    rows: list[dict[str, Any]] = []
    for name, duties in ([(key_name, roles[key_name])] if key_name
                         else sorted(roles.items())):
        definition = duties.get(phase) or duties.get(PHASE_IN) or next(
            iter(duties.values()))
        key_attributes, preferable = definition
        rows.append({
            "role": role_display(name),
            "phase": phase if phase in duties else PHASE_IN,
            "key_attributes": list(key_attributes),
            "preferable_attributes": list(preferable),
        })
    return {
        "position": short_label(position),
        "general_position": general,
        "key_weight": R.key_weight(),
        "preferable_weight": R.preferable_weight(),
        "roles": rows,
        "guidance": (
            "Key attributes carry the original's 16 weight and the preferable "
            "ones 4, so a player is judged on the role's own list rather than on "
            "a generic average."),
    }
