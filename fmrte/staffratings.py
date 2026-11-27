"""Football Manager staff role & coach training star rating calculator.

Implements the standard FM coach training star rating formulas (0..300 points,
30-point half-star bands from 0.5★ to 5.0★) using ``DDM = Determination +
Level of Discipline + Motivating``, plus specialist role star ratings for
Scouts, Physios, Sports Scientists, Head of Youth Development, Directors of
Football, and Performance Analysts.

Pure Python 3.12 stdlib only; no SQLite, Tkinter, or ctypes imports.
"""

from __future__ import annotations

from typing import Any

COACH_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("attacking_tactical", "Attacking (Tactical)", "进攻战术"),
    ("attacking_technical", "Attacking (Technical)", "进攻技术"),
    ("defending_tactical", "Defending (Tactical)", "防守战术"),
    ("defending_technical", "Defending (Technical)", "防守技术"),
    ("possession_tactical", "Possession (Tactical)", "控球战术"),
    ("possession_technical", "Possession (Technical)", "控球技术"),
    ("fitness", "Fitness", "体能训练"),
    ("gk_shot_stopping", "GK Shot Stopping", "门将扑救"),
    ("gk_handling", "GK Handling & Dist", "门将手控/发球"),
    ("set_pieces", "Set Pieces", "定位球"),
)

ROLE_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("scout", "Scout", "球探"),
    ("physio", "Physiotherapist", "理疗师"),
    ("sports_scientist", "Sports Scientist", "运动科学"),
    ("hoyd", "Head of Youth Dev", "青训主管"),
    ("dof", "Director of Football", "足球总监"),
    ("analyst", "Performance Analyst", "数据分析师"),
)

ALL_CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    tuple((k, en, zh, "Coaching") for k, en, zh in COACH_CATEGORIES)
    + tuple((k, en, zh, "Specialist") for k, en, zh in ROLE_CATEGORIES)
)

CATEGORY_BY_KEY: dict[str, tuple[str, str, str, str]] = {
    row[0]: row for row in ALL_CATEGORIES
}

# Primary attributes displayed as a compact summary per category
CATEGORY_KEY_ATTRS: dict[str, tuple[tuple[str, str], ...]] = {
    "attacking_tactical": (("Att", "attacking"), ("Tac", "tactical")),
    "attacking_technical": (("Att", "attacking"), ("Tec", "technical")),
    "defending_tactical": (("Def", "defending"), ("Tac", "tactical")),
    "defending_technical": (("Def", "defending"), ("Tec", "technical")),
    "possession_tactical": (("Men", "mental"), ("Tac", "tactical")),
    "possession_technical": (("Tec", "technical"), ("Men", "mental")),
    "fitness": (("Fit", "fitness"),),
    "gk_shot_stopping": (("Shot", "gk_shot_stopping"),),
    "gk_handling": (("Hand", "gk_handling"), ("Dist", "gk_distribution")),
    "set_pieces": (("TacK", "tactical_knowledge"), ("SetP", "set_pieces")),
    "scout": (("JPA", "judging_player_ability"), ("JPP", "judging_player_potential"), ("Adp", "adaptability")),
    "physio": (("Phy", "physiotherapy"), ("Det", "determination")),
    "sports_scientist": (("Sci", "sports_science"), ("Phy", "physiotherapy")),
    "hoyd": (("WwY", "working_with_youngsters"), ("JPP", "judging_player_potential"), ("JPA", "judging_player_ability")),
    "dof": (("JPA", "judging_player_ability"), ("JPP", "judging_player_potential"), ("Neg", "negotiating")),
    "analyst": (("Data", "analysing_data"), ("TacK", "tactical_knowledge")),
}

_COACH_JOBS = frozenset({
    "coach", "assistant manager", "manager", "fitness coach",
    "goalkeeping coach", "head of youth development", "set piece coach",
})


def _attr(attrs: dict[str, Any], key: str) -> int:
    try:
        val = attrs.get(key)
        if val is None:
            return 0
        return max(0, min(20, int(val)))
    except (TypeError, ValueError):
        return 0


def score_to_stars(score: float) -> float:
    """Convert an FM 0..300 training/role score into 0.5★..5.0★ (30-pt bands)."""
    if score <= 0:
        return 0.5
    if score >= 271:
        return 5.0
    band = int((score - 1) // 30) + 1
    return max(0.5, min(5.0, band * 0.5))


def _default_category_for_job(job: str) -> str | None:
    low = (job or "").strip().lower()
    if "goalkeep" in low:
        return "gk_shot_stopping"
    if "fitness" in low:
        return "fitness"
    if "scout" in low:
        return "scout"
    if "physio" in low or "doctor" in low:
        return "physio"
    if "sports scien" in low:
        return "sports_scientist"
    if "youth" in low:
        return "hoyd"
    if "director of football" in low or "technical director" in low or "loan" in low:
        return "dof"
    if "analyst" in low:
        return "analyst"
    return None


def has_live_staff_attributes(attrs: dict[str, Any]) -> bool:
    """True when real FM27 staff coaching/scouting/medical attributes are present."""
    for key in ("attacking", "defending", "fitness", "tactical", "technical",
                "determination", "judging_player_ability", "physiotherapy"):
        if attrs.get(key) is not None:
            return True
    return False


def evaluate_staff(attrs: dict[str, Any], *, job: str = "",
                   focus_category: str = "") -> dict[str, Any]:
    """Compute FM coach training star ratings & specialist role star ratings.

    When live staff attributes are present, computes exact 0.5★..5.0★ ratings
    from the FM formulas (DDM + weighted primary/secondary attributes).
    When only snapshot CA/PA is available, falls back to a clearly flagged
    CA-based estimate so the UI never shows empty/broken stars.
    """
    focus = (focus_category or "").strip().lower()
    if focus not in CATEGORY_BY_KEY:
        focus = ""

    if not has_live_staff_attributes(attrs):
        ca = 0
        try:
            ca = max(0, min(200, int(attrs.get("ca") or 0)))
        except (TypeError, ValueError):
            ca = 0
        est_score = round(ca * 1.45, 1)
        est_stars = score_to_stars(est_score) if ca > 0 else 0.5
        default_cat = focus or _default_category_for_job(job) or "attacking_tactical"
        meta = CATEGORY_BY_KEY[default_cat]
        return {
            "live_stars": False,
            "stars": est_stars,
            "score": est_score,
            "category": default_cat,
            "category_label": meta[1],
            "category_zh": meta[2],
            "ddm": None,
            "key_summary": "Attach FM27 for exact attrs",
            "coach_stars": {},
            "role_stars": {},
        }

    det = _attr(attrs, "determination")
    disc = _attr(attrs, "level_of_discipline")
    mot = _attr(attrs, "motivating")
    ddm = det + disc + mot

    att = _attr(attrs, "attacking")
    dfn = _attr(attrs, "defending")
    fit = _attr(attrs, "fitness")
    men = _attr(attrs, "mental")
    tac = _attr(attrs, "tactical")
    tec = _attr(attrs, "technical")
    wwy = _attr(attrs, "working_with_youngsters")
    setp = _attr(attrs, "set_pieces")
    gk_ss = _attr(attrs, "gk_shot_stopping")
    gk_h = _attr(attrs, "gk_handling")
    gk_d = _attr(attrs, "gk_distribution")
    tack = _attr(attrs, "tactical_knowledge")
    man = _attr(attrs, "man_management")
    adp = _attr(attrs, "adaptability")
    jpa = _attr(attrs, "judging_player_ability")
    jpp = _attr(attrs, "judging_player_potential")
    neg = _attr(attrs, "negotiating")
    phy = _attr(attrs, "physiotherapy")
    sci = _attr(attrs, "sports_science")
    ana = _attr(attrs, "analysing_data")

    coach_scores: dict[str, float] = {
        "attacking_tactical": att * 6 + tac * 3 + ddm * 2,
        "attacking_technical": att * 6 + tec * 3 + ddm * 2,
        "defending_tactical": dfn * 6 + tac * 3 + ddm * 2,
        "defending_technical": dfn * 6 + tec * 3 + ddm * 2,
        "possession_tactical": men * 6 + tac * 3 + ddm * 2,
        "possession_technical": tec * 6 + men * 3 + ddm * 2,
        "fitness": fit * 9 + ddm * 2,
        "gk_shot_stopping": gk_ss * 9 + ddm * 2,
        "gk_handling": gk_h * 6 + gk_d * 3 + ddm * 2,
        "set_pieces": tack * 6 + setp * 3 + ddm * 2,
    }
    role_scores: dict[str, float] = {
        "scout": jpa * 5 + jpp * 5 + adp * 3 + det * 2,
        "physio": phy * 9 + det * 3 + disc * 1.5 + mot * 1.5,
        "sports_scientist": sci * 8 + phy * 4 + det * 2 + disc * 1,
        "hoyd": wwy * 6 + jpp * 4 + jpa * 3 + det * 2,
        "dof": jpa * 4 + jpp * 4 + neg * 4 + adp * 1.5 + man * 1.5,
        "analyst": ana * 7 + tack * 4 + jpa * 2 + jpp * 2,
    }

    coach_stars = {k: score_to_stars(v) for k, v in coach_scores.items()}
    role_stars = {k: score_to_stars(v) for k, v in role_scores.items()}
    all_scores = {**coach_scores, **role_scores}
    all_stars = {**coach_stars, **role_stars}

    if focus:
        chosen_key = focus
    else:
        job_hint = _default_category_for_job(job)
        if job_hint and job_hint in role_scores:
            chosen_key = job_hint
        elif job_hint and job_hint in ("gk_shot_stopping", "fitness"):
            # For GK coaches compare both GK categories
            if job_hint == "gk_shot_stopping":
                chosen_key = max(("gk_shot_stopping", "gk_handling"), key=lambda k: coach_scores[k])
            else:
                chosen_key = "fitness"
        elif (job or "").strip().lower() in _COACH_JOBS or not job_hint:
            chosen_key = max(coach_scores, key=lambda k: coach_scores[k])
        else:
            chosen_key = max(all_scores, key=lambda k: all_scores[k])

    meta = CATEGORY_BY_KEY[chosen_key]
    parts = [f"{lbl} {_attr(attrs, col)}" for lbl, col in CATEGORY_KEY_ATTRS.get(chosen_key, ())]
    if chosen_key in coach_scores:
        parts.append(f"DDM {ddm}")

    return {
        "live_stars": True,
        "stars": all_stars[chosen_key],
        "score": round(all_scores[chosen_key], 1),
        "category": chosen_key,
        "category_label": meta[1],
        "category_zh": meta[2],
        "ddm": ddm,
        "key_summary": " · ".join(parts),
        "coach_stars": coach_stars,
        "role_stars": role_stars,
        "coach_scores": {k: round(v, 1) for k, v in coach_scores.items()},
        "role_scores": {k: round(v, 1) for k, v in role_scores.items()},
    }


def categories_catalogue() -> list[dict[str, str]]:
    return [
        {"key": k, "label": en, "zh": zh, "group": grp}
        for k, en, zh, grp in ALL_CATEGORIES
    ]
