"""The assistant's prompt: fixed instructions, findings, and the save's facts.

The assistant assembles its prompt in three layers, exactly as the assistant's
own ``CompositeAiPromptContext`` does it:

1. **Fixed instructions** (:data:`INSTRUCTIONS`) - who the assistant is.
2. **The always-on conclusions** (:data:`ALWAYS_ON`) - the few measured
   findings that change whether advice is right, kept small because every
   message pays for them.
3. **The save's own facts** (:func:`save_context`) - rendered from the snapshot
   this editor already holds, so the model answers about the save that is open
   rather than about a save it cannot see.

This module was split out of ``assistant``: provider transport, prompt
assembly and plan generation were three unrelated concerns in one file.  It has
no transport and no plan in it, and it reads the data layer only through its
public interface (``meta``, ``counts``, ``game_date``, ``club_by_name``,
``squad``, ``injured_players``).
"""

from __future__ import annotations

from typing import Any

INSTRUCTIONS = (
    "You are the FM27 AI Assistant integrated inside FMRTE. "
    "You help the manager with tactical setup, training schedules, lineup "
    "recommendations, and transfer/recruitment advice using FM AI Assistant's "
    "measured Match Lab rules and FMRTE's loaded save/live memory data. "
    "Always call tools to inspect real squad, player, club, fixture, tactic, "
    "and Match Lab knowledge data before recommending names, numbers, or roles. "
    "Never invent a player, club, price, attribute, or date that is absent. "
    "Follow Match Lab evidence: S20-CA-INERT (never sort by CA/PA alone; judge "
    "players on role attributes and physicals like Pace/Acceleration/Stamina), "
    "S5-SHARPNESS-FIRST & S5-CONDITION-SECOND (sharpness & condition outrank "
    "tactic tweaks), S5-PRESSING-TRAP (avoid pressing traps inside/outside), "
    "and S3-FAMILIARITY-COST (tactical familiarity matters; training doubles it). "
    "Use FM unique_ids for player identity, keep asking_price=null explicitly "
    "unknown, and clearly distinguish snapshot vs live-memory values. "
    "Reply in the user's language, concisely and concretely for a side panel."
)

# The assistant's own "always on" conclusions: the few findings that change
# whether advice is right, kept small because every message pays for them.
ALWAYS_ON = [
    ("S20-CA-INERT",
     "Current Ability does not decide matches. Sorting a squad by CA/PA is "
     "sorting by a number the match engine ignores; judge players on the "
     "attributes their role actually uses."),
    ("S5-SHARPNESS-FIRST",
     "Match sharpness matters more than most other condition signals. A player "
     "who is fit but not match-sharp underperforms a slightly tired player who "
     "is sharp."),
    ("S5-PRESSING-TRAP",
     "Setting pressing trap to inside or outside instead of standard costs "
     "~0.22 points/game; keep pressing traps on Standard unless clearly needed."),
    ("ROLE-BEFORE-RATING",
     "A rating is only meaningful for a role. The same player can be excellent "
     "as a pressing forward and ordinary as a target forward, so always name "
     "the role a judgement is about."),
]


def knowledge_context() -> str:
    lines = ["Known conclusions about the match engine (measured, not guessed):"]
    for code, text in ALWAYS_ON:
        lines.append(f"- {code}: {text}")
    return "\n".join(lines)


def resolve_active_club(db, club_name: str = "") -> str:
    """Resolve the active club from explicit arg, live manager, or default."""
    if club_name and club_name.strip():
        return club_name.strip()
    try:
        meta = db.meta() if hasattr(db, "meta") else {}
    except Exception:  # noqa: BLE001
        meta = {}
    if isinstance(meta, dict):
        live_club = (meta.get("managed_club") or "").strip()
        if live_club:
            return live_club
    return "Milan"


def _injured_rows(db, limit: int = 6) -> list[dict[str, Any]]:
    """The most notable injured players, from the data layer's own reader."""
    reader = getattr(db, "injured_players", None)
    if not callable(reader):
        return []
    try:
        return [dict(row) for row in reader(limit)]
    except Exception:  # noqa: BLE001 - context must render even when a read fails
        return []


def save_context(db, club_name: str = "") -> str:
    """The save's own facts, rendered from the snapshot + live memory."""
    try:
        meta = db.meta()
    except Exception:  # noqa: BLE001 - a partial data layer must still answer
        meta = {}
    try:
        counts = db.counts()
    except Exception:  # noqa: BLE001
        counts = {}
    try:
        game_date = db.game_date(meta)
    except Exception:  # noqa: BLE001
        game_date = str(meta.get("game_date") or "unknown")
    attached = bool(getattr(db, "attached", False))
    read_only = bool(getattr(db, "read_only", False))
    live_count = len(getattr(db, "live", {}) or {})
    mode_text = (
        f"Live memory attached ({'read-only' if read_only else 'read/write'}, "
        f"{live_count:,} live player records)"
        if attached else "Snapshot only"
    )
    active_club = resolve_active_club(db, club_name)
    lines = [
        "The loaded save:",
        f"- Data mode: {mode_text}",
        f"- Game date: {game_date}",
        f"- Football Manager build: {meta.get('fm_build', 'unknown')} "
        f"(version {meta.get('game_version', 'unknown')})",
        f"- Rows in the snapshot: {counts.get('players', 0):,} players, "
        f"{counts.get('staff', 0):,} staff, {counts.get('clubs', 0):,} clubs, "
        f"{counts.get('competitions', 0):,} competitions.",
    ]
    try:
        club = db.club_by_name(active_club) or db.club_by_name("Milan")
    except Exception:  # noqa: BLE001
        club = None
    if club:
        lines.append(
            f"- Active club context: {club.get('name')} "
            f"({club.get('competition') or 'unknown competition'}), "
            f"reputation {club.get('reputation')}, "
            f"balance {club.get('balance')}, "
            f"transfer budget {club.get('transfer_budget')}, "
            f"payroll budget {club.get('payroll_budget')}.")
        try:
            squad = db.squad(club.get("name") or "", 12)
        except Exception:  # noqa: BLE001
            squad = []
        if squad:
            lines.append("- Top squad players:")
            for row in squad[:10]:
                lines.append(
                    f"  · [{row.get('unique_id')}] {row.get('name')} — "
                    f"{row.get('age')}y, pos {row.get('goalkeeper')}/{row.get('defender_central')}/"
                    f"{row.get('midfielder_central')}/{row.get('striker')}, "
                    f"CA {row.get('ca')}, PA {row.get('pa')}, "
                    f"cond {row.get('condition')}, sharp {row.get('match_sharpness')}, "
                    f"morale {row.get('morale')}"
                    + (", injured" if row.get("injured") else ""))
    injured = _injured_rows(db, 6)
    if injured:
        lines.append("- Notable injured players:")
        for row in injured:
            lines.append(f"  · {row.get('name')} ({row.get('club')}) — "
                         f"{row.get('injury')}")
    return "\n".join(lines)


def build_prompt(db, user_message: str, club_name: str = "") -> str:
    """The assistant's own envelope: context first, then the person's words."""
    parts = [knowledge_context(), save_context(db, club_name=club_name)]
    body = "\n\n".join(part for part in parts if part and part.strip())
    return (
        "The following Football Manager 2027 context was loaded locally by the "
        "application. Treat it as factual context for the current save. Use it "
        "when relevant, and do not invent values that are absent.\n\n"
        f"{body}\n\n"
        "User message:\n"
        f"{user_message}"
    )
