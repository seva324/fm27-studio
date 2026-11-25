"""The game's own tactic templates: ``Tactics/*.json``, read from disk.

This is the file-format reader and nothing else: a template is a name and the
eleven positions the game wrote into it.  It used to live in ``assistant``,
which is why ``fmtools.build_tactic_from_squad`` had to import the assistant
*inside* the function - and why the dependency between the two modules was
circular.  A template file is a fact about the game, not about the assistant,
so the reader sits below both of them here.

Nothing in this module imports anything from this build, so it can never take
part in a cycle.
"""

from __future__ import annotations

import json
import os
from typing import Any

# A template is tiny; anything larger is not one, and must not be read into
# memory (TacticFileService.MAX_TEMPLATE_BYTES).
MAX_TEMPLATE_BYTES = 4194304

# Built-in canonical FM27 formation templates so tactic generation ALWAYS works
# out of the box without requiring an external ``FMRTE_GAME_DIR/Tactics/*.json``
# folder.  Any user-installed Tactics/*.json files are merged on top.
BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "file": "builtin-4-2-3-1-dm-am-wide.json",
        "name": "4-2-3-1 DM AM Wide",
        "positions": [
            "Goalkeeper",
            "DefenderLeft", "DefenderCentral", "DefenderCentral", "DefenderRight",
            "DefensiveMidfielder", "DefensiveMidfielder",
            "AttackingMidfielderLeft", "AttackingMidfielderCentral", "AttackingMidfielderRight",
            "Striker",
        ],
    },
    {
        "file": "builtin-4-3-3-dm-wide.json",
        "name": "4-3-3 DM Wide",
        "positions": [
            "Goalkeeper",
            "DefenderLeft", "DefenderCentral", "DefenderCentral", "DefenderRight",
            "DefensiveMidfielder", "MidfielderCentral", "MidfielderCentral",
            "AttackingMidfielderLeft", "AttackingMidfielderRight",
            "Striker",
        ],
    },
    {
        "file": "builtin-4-4-2.json",
        "name": "4-4-2",
        "positions": [
            "Goalkeeper",
            "DefenderLeft", "DefenderCentral", "DefenderCentral", "DefenderRight",
            "MidfielderLeft", "MidfielderCentral", "MidfielderCentral", "MidfielderRight",
            "Striker", "Striker",
        ],
    },
    {
        "file": "builtin-3-4-2-1-dm-am.json",
        "name": "3-4-2-1 DM AM",
        "positions": [
            "Goalkeeper",
            "DefenderCentral", "DefenderCentral", "DefenderCentral",
            "WingBackLeft", "DefensiveMidfielder", "DefensiveMidfielder", "WingBackRight",
            "AttackingMidfielderCentral", "AttackingMidfielderCentral",
            "Striker",
        ],
    },
    {
        "file": "builtin-3-5-2-dm-wb.json",
        "name": "3-5-2 DM WB",
        "positions": [
            "Goalkeeper",
            "DefenderCentral", "DefenderCentral", "DefenderCentral",
            "WingBackLeft", "DefensiveMidfielder", "MidfielderCentral", "MidfielderCentral", "WingBackRight",
            "Striker", "Striker",
        ],
    },
    {
        "file": "builtin-4-2-4-wide.json",
        "name": "4-2-4 Wide",
        "positions": [
            "Goalkeeper",
            "DefenderLeft", "DefenderCentral", "DefenderCentral", "DefenderRight",
            "DefensiveMidfielder", "DefensiveMidfielder",
            "AttackingMidfielderLeft", "AttackingMidfielderRight",
            "Striker", "Striker",
        ],
    },
    {
        "file": "builtin-4-1-2-1-2-diamond.json",
        "name": "4-1-2-1-2 Diamond Narrow",
        "positions": [
            "Goalkeeper",
            "DefenderLeft", "DefenderCentral", "DefenderCentral", "DefenderRight",
            "DefensiveMidfielder", "MidfielderCentral", "MidfielderCentral",
            "AttackingMidfielderCentral",
            "Striker", "Striker",
        ],
    },
    {
        "file": "builtin-5-2-3-dm-wide.json",
        "name": "5-2-3 DM Wide",
        "positions": [
            "Goalkeeper",
            "WingBackLeft", "DefenderCentral", "DefenderCentral", "DefenderCentral", "WingBackRight",
            "DefensiveMidfielder", "DefensiveMidfielder",
            "AttackingMidfielderLeft", "AttackingMidfielderRight",
            "Striker",
        ],
    },
)


def tactic_templates(directory: str) -> list[dict[str, Any]]:
    """Read the game's own tactic templates: a name and eleven positions."""
    found: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            # A template is tiny; anything larger is not one, and must not be
            # read into memory (TacticFileService.MAX_TEMPLATE_BYTES).
            if os.path.getsize(path) > MAX_TEMPLATE_BYTES:
                continue
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        positions = payload.get("Positions")
        if not isinstance(positions, list) or len(positions) != 11:
            continue
        found.append({
            "file": name,
            "name": payload.get("Name") or os.path.splitext(name)[0],
            "positions": [str(p) for p in positions],
        })
    return found


def game_data_directory() -> str:
    """Locate optional tactic template folders (Steam / Xbox / FMRTE)."""
    candidates = []
    configured = os.environ.get("FMRTE_GAME_DIR", "")
    if configured:
        candidates.append(configured)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "FMRTE"))
    candidates.extend([
        r"C:\Program Files (x86)\Steam\steamapps\common\Football Manager 27",
        r"C:\Program Files (x86)\Steam\steamapps\common\Football Manager 2027",
        r"C:\Program Files\Steam\steamapps\common\Football Manager 27",
        r"C:\XboxGames\Football Manager 27\Content",
    ])
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "Tactics")):
            return os.path.abspath(candidate)
    return ""


def load_templates() -> list[dict[str, Any]]:
    """Tactic templates from disk (when available) merged with built-in FM27 shapes."""
    disk: list[dict[str, Any]] = []
    directory = game_data_directory()
    if directory:
        disk = tactic_templates(os.path.join(directory, "Tactics"))
    seen = {t["name"].lower() for t in disk}
    merged = list(disk)
    for builtin in BUILTIN_TEMPLATES:
        if builtin["name"].lower() not in seen:
            merged.append({
                "file": builtin["file"],
                "name": builtin["name"],
                "positions": list(builtin["positions"]),
            })
    return merged
