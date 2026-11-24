"""Local HTTP + SSE backend for FM27 Studio (Scheme A modern desktop UI).

Reuses 100% of the existing pure-Python core (``db.Snapshot``, ``livewrite``,
``fmmemory``, ``assistant``, ``fmtools``, ``fmplans``, ``ratings``, ``spec``)
with zero third-party dependencies.  Listens strictly on ``127.0.0.1`` and
authenticates API calls with a per-launch random token so no external site can
reach the live editor.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import assistant as A
from . import fmplans, fmtools, ratings, spec
from .db import DATE_COLUMNS, SQUAD_COLUMNS, Snapshot

DEFAULT_CLUB = "Milan"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio_ui")

_OUTFIELD_RADAR_AXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Attacking", ("finishing", "off_the_ball", "composure", "long_shots")),
    ("Technical", ("dribbling", "first_touch", "technique", "passing")),
    ("Vision", ("vision", "flair", "decisions", "anticipation")),
    ("Defending", ("tackling", "marking", "positioning", "concentration")),
    ("Speed", ("pace", "acceleration", "agility")),
    ("Physical", ("strength", "stamina", "balance", "natural_fitness")),
    ("Aerial", ("heading", "jumping_reach", "strength", "bravery")),
    ("Mental", ("determination", "work_rate", "teamwork", "leadership")),
)

_GK_RADAR_AXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Reflexes", ("reflexes", "agility", "one_on_ones")),
    ("Handling", ("handling", "first_touch", "composure")),
    ("Aerial", ("aerial_reach", "jumping_reach", "bravery")),
    ("Command", ("command_of_area", "communication", "leadership")),
    ("Sweeping", ("rushing_out", "anticipation", "acceleration")),
    ("Distribution", ("kicking", "throwing", "passing", "vision")),
    ("Positioning", ("positioning", "concentration", "decisions")),
    ("Physical", ("strength", "balance", "natural_fitness")),
)


def _studio_prefs_path() -> str:
    return os.path.join(A.data_directory(), "studio.json")


def _detect_graphics_dir(preferred: str = "") -> str:
    candidates: list[str] = []
    if preferred and preferred.strip():
        candidates.append(preferred.strip())
    env_dir = os.environ.get("FMRTE_GRAPHICS_DIR", "").strip()
    if env_dir:
        candidates.append(env_dir)
    home = os.path.expanduser("~")
    for base in (
        os.path.join(home, "Documents", "Sports Interactive"),
        os.path.join(home, "OneDrive", "Documents", "Sports Interactive"),
    ):
        for game in ("Football Manager 27", "Football Manager 2027",
                     "Football Manager 26", "Football Manager 2024"):
            candidates.append(os.path.join(base, game, "graphics"))
    for path in candidates:
        if path and os.path.isdir(path):
            if (os.path.isdir(os.path.join(path, "faces"))
                    or os.path.isdir(os.path.join(path, "iconfaces"))
                    or any(f.lower().endswith((".png", ".jpg", ".webp"))
                           for f in os.listdir(path)[:25])):
                return path
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return preferred.strip() if preferred else ""


class _FaceResolver:
    """Fast O(1) resolver for local FM27 facepack portraits (``faces/<uid>.png``)."""

    def __init__(self, graphics_dir: str = ""):
        self._lock = threading.Lock()
        self._cache: dict[int, str | None] = {}
        self.graphics_dir = ""
        self._search_dirs: list[str] = []
        self.configure(graphics_dir)

    def configure(self, graphics_dir: str = "") -> None:
        resolved = _detect_graphics_dir(graphics_dir)
        dirs: list[str] = []
        if resolved and os.path.isdir(resolved):
            for sub in ("faces", "iconfaces", ""):
                cand = os.path.join(resolved, sub) if sub else resolved
                if os.path.isdir(cand) and cand not in dirs:
                    dirs.append(cand)
        with self._lock:
            self.graphics_dir = resolved
            self._search_dirs = dirs
            self._cache.clear()

    @property
    def available(self) -> bool:
        return bool(self._search_dirs)

    def resolve(self, uid: int) -> str | None:
        if not uid or not self._search_dirs:
            return None
        with self._lock:
            if uid in self._cache:
                return self._cache[uid]
            dirs = list(self._search_dirs)
        found: str | None = None
        names = (
            f"{uid}.png", f"r-{uid}.png",
            f"{uid}.jpg", f"{uid}.webp",
        )
        for folder in dirs:
            for fname in names:
                full = os.path.join(folder, fname)
                if os.path.isfile(full):
                    found = full
                    break
            if found:
                break
        with self._lock:
            if len(self._cache) > 8192:
                self._cache.clear()
            self._cache[uid] = found
        return found


def _load_studio_prefs() -> dict[str, Any]:
    try:
        with open(_studio_prefs_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_studio_prefs(prefs: dict[str, Any]) -> None:
    try:
        path = _studio_prefs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(prefs, handle, ensure_ascii=False, indent=1)
    except OSError:
        pass

_ATTRIBUTE_GROUPS = (
    ("Technical", [(a.label, a.column) for a in spec.ATTRIBUTE_COLUMNS if a.group == "Technical"]),
    ("Mental", [(a.label, a.column) for a in spec.ATTRIBUTE_COLUMNS if a.group == "Mental"]),
    ("Physical", [(a.label, a.column) for a in spec.ATTRIBUTE_COLUMNS if a.group == "Physical"]),
    ("Goalkeeping", sorted((a.label, a.column) for a in spec.ATTRIBUTE_COLUMNS if a.group == "Goalkeeping")),
    ("Hidden", [
        ("Consistency", "consistency"),
        ("Dirtiness", "dirtiness"),
        ("Important Matches", "important_matches"),
        ("Versatility", "versatility"),
        ("Injury Proneness", "injury_proneness"),
    ]),
    ("Personality", [
        (col.replace("_", " ").title(), col)
        for col in ("adaptability", "ambition", "loyalty", "pressure",
                    "professionalism", "sportsmanship", "temperament", "controversy")
    ]),
)


def _money(value: Any) -> str:
    try:
        num = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if num <= 0:
        return "—"
    if num >= 1_000_000:
        return f"£{num / 1_000_000:.1f}M"
    if num >= 1_000:
        return f"£{num / 1_000:.1f}k"
    return f"£{num:,}"


def _opt_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _player_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "unique_id": int(row.get("unique_id") or 0),
        "name": Snapshot.display_name(row),
        "full_name": row.get("name") or "",
        "club": Snapshot.display_club(row.get("club")),
        "club_raw": row.get("club") or "",
        "age": Snapshot.age(row),
        "nationality": row.get("nationality") or "—",
        "pos": Snapshot.position_text(row),
        "role": Snapshot.role_name(row),
        "ca": int(row.get("ca") or 0),
        "pa": int(row.get("pa") or 0),
        "morale": row.get("morale"),
        "condition": row.get("condition"),
        "condition_pct": round(Snapshot.condition_pct(row), 1),
        "sharpness": row.get("match_sharpness"),
        "jadedness": row.get("jadedness"),
        "injured": bool(row.get("injured")),
        "injury": row.get("injury") or "",
        "value": _money(row.get("asking_price_raw") or row.get("asking_price")),
        "value_raw": int(row.get("asking_price_raw") or row.get("asking_price") or 0),
        "wage": _money(row.get("salary_weekly_raw")),
        "wage_raw": int(row.get("salary_weekly_raw") or 0),
        "contract": Snapshot.date(row.get("contract_end_date")),
        "contract_iso": row.get("contract_end_date") or "",
    }


def _staff_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "unique_id": int(row.get("unique_id") or row.get("id") or 0),
        "name": row.get("name") or "—",
        "club": Snapshot.display_club(row.get("club")) if row.get("club") else "Unattached",
        "club_raw": row.get("club") or "",
        "job": row.get("job") or "—",
        "age": row.get("age"),
        "nationality": row.get("nationality") or "—",
        "ca": int(row.get("ca") or 0),
        "pa": int(row.get("pa") or 0),
    }


def _club_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "unique_id": row.get("unique_id"),
        "name": Snapshot.display_club(row.get("name")),
        "raw_name": row.get("name") or "",
        "nation": row.get("nation") or "—",
        "competition": row.get("competition") or "—",
        "reputation": int(row.get("reputation") or 0),
        "balance": _money(row.get("balance")),
        "balance_raw": row.get("balance"),
        "transfer_budget": _money(row.get("transfer_budget")),
        "transfer_budget_raw": row.get("transfer_budget"),
        "training_facilities": row.get("training_facilities"),
        "youth_facilities": row.get("youth_facilities"),
        "youth_coaching": row.get("youth_coaching"),
        "youth_recruitment": row.get("youth_recruitment"),
        "stadium": row.get("stadium") or "—",
        "founded": row.get("founded") or "—",
    }


class StudioBackend:
    """Thread-safe service facade over ``Snapshot``, ``LiveEditor`` & ``assistant``."""

    def __init__(self, db: Snapshot | None = None):
        self.db = db if db is not None else Snapshot()
        self._owns_db = db is None
        self._ai_lock = threading.Lock()
        self.settings = A.AssistantSettings.load()
        self.keys = A.KeyStore()
        self.conversation = A.Conversation()
        self._attach_lock = threading.Lock()
        self._prefs = _load_studio_prefs()
        self.faces = _FaceResolver(str(self._prefs.get("graphics_dir") or ""))

    def close(self) -> None:
        try:
            self.db.detach_live()
        except Exception:
            pass
        if self._owns_db:
            try:
                self.db.close()
            except Exception:
                pass

    # -- status & bootstrap -----------------------------------------------
    def status_payload(self) -> dict[str, Any]:
        meta = self.db.meta()
        counts = self.db.counts()
        managed_raw = (meta.get("managed_club") or "").strip() or DEFAULT_CLUB
        managed_club = self.db.club_by_name(managed_raw) or self.db.club_by_name(DEFAULT_CLUB)
        return {
            "attached": bool(self.db.attached),
            "read_only": bool(self.db.read_only),
            "live_count": self.db.live_count(),
            "game_date": Snapshot.game_date(meta),
            "game_date_iso": meta.get("game_date") or "",
            "game_version": Snapshot.game_version(meta),
            "fm_build": meta.get("fm_build") or "2505117",
            "managed_club": Snapshot.display_club(managed_raw),
            "managed_club_raw": managed_raw,
            "managed_club_id": managed_club["id"] if managed_club else None,
            "counts": counts,
            "dirty": self.db.dirty(),
            "journal_count": len(self.db.journal),
            "redo_count": len(self.db.redo_stack),
        }

    def bootstrap(self) -> dict[str, Any]:
        status = self.status_payload()
        top_players = [_player_summary(p) for p in self.db.query_players(
            order="ca", descending=True, limit=6, columns=SQUAD_COLUMNS)]
        pulisic = self.db.player(92074020)
        if pulisic and all(p["unique_id"] != 92074020 for p in top_players):
            top_players.insert(1, _player_summary(pulisic))
        return {
            "status": status,
            "attribute_groups": [
                {"name": g, "fields": [{"label": lbl, "column": col} for lbl, col in fields]}
                for g, fields in _ATTRIBUTE_GROUPS
            ],
            "positions": [{"label": p.label, "column": p.column} for p in spec.POSITIONS],
            "staff_jobs": self.db.staff_jobs(25),
            "default_players": top_players[:5],
            "ai": self.ai_settings_payload(),
        }

    # -- live attach / detach ---------------------------------------------
    def attach_game(self, *, refresh_all: bool = False) -> dict[str, Any]:
        with self._attach_lock:
            from . import fmmemory, livewrite

            try:
                editor, report = livewrite.LiveEditor.attach()
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": str(exc),
                    "hint": getattr(exc, "hint", "") or "",
                    "status": self.status_payload(),
                }

            records = None
            if refresh_all:
                try:
                    found, live_rep = fmmemory.read_live(self.db.unique_ids())
                    records = found
                    if live_rep.game_date:
                        report.game_date = live_rep.game_date
                    if live_rep.managed_club:
                        report.managed_club = live_rep.managed_club
                except Exception:
                    records = None

            self.db.attach_live(editor, records=records, report=report)
            mode = "read-only" if self.db.read_only else "read & write"
            club = report.managed_club or self.status_payload()["managed_club"]
            return {
                "ok": True,
                "message": (
                    f"Attached to FM27 ({mode}) · Build {getattr(editor, 'build', '')} · "
                    f"{Snapshot.display_club(club)}"
                ),
                "flushed": list(self.db.last_flush),
                "status": self.status_payload(),
            }

    def detach_game(self) -> dict[str, Any]:
        with self._attach_lock:
            self.db.detach_live()
            return {"ok": True, "message": "Detached from FM27.", "status": self.status_payload()}

    # -- entities ---------------------------------------------------------
    def get_player(self, uid: int) -> dict[str, Any] | None:
        row = self.db.player(int(uid))
        if not row:
            return None
        if self.db.attached:
            self.db.refresh_display_name(row)
            self.db.refresh_injury(row)
        summary = _player_summary(row)
        best_role = ratings.role_label(row)
        best_score = ratings.weighted_rating(row)
        attrs: dict[str, list[dict[str, Any]]] = {}
        for group_name, fields in _ATTRIBUTE_GROUPS:
            attrs[group_name] = [
                {"label": lbl, "column": col, "value": int(row.get(col) or 0)}
                for lbl, col in fields
            ]
        positions = [
            {"label": p.label, "column": p.column, "value": int(row.get(p.column) or 0)}
            for p in spec.POSITIONS
        ]
        club_obj = self.db.club_by_name(row.get("club")) if row.get("club") else None
        return {
            **summary,
            "shirt_number": Snapshot.shirt_number(row),
            "dob": Snapshot.dob(row),
            "height_cm": row.get("height_cm"),
            "left_foot": int(row.get("left_foot") or 0),
            "right_foot": int(row.get("right_foot") or 0),
            "home_reputation": int(row.get("home_reputation") or 0),
            "current_reputation": int(row.get("current_reputation") or 0),
            "world_reputation": int(row.get("world_reputation") or 0),
            "joined_club_date": Snapshot.date(row.get("joined_club_date")),
            "joined_club_iso": row.get("joined_club_date") or "",
            "transfer_listed": bool(row.get("transfer_listed")),
            "listed_for_loan": bool(row.get("listed_for_loan")),
            "injury_start_date": Snapshot.date(row.get("injury_start_date")),
            "injury_expected_return": Snapshot.date(row.get("injury_expected_return")),
            "injury_light_days": row.get("injury_light_training_days_remaining") or "",
            "injury_full_days": row.get("injury_full_training_days_remaining") or "",
            "rating": round(Snapshot.rating(row), 2),
            "best_role": best_role or summary["role"],
            "best_role_score": round(best_score, 2),
            "club_id": club_obj["id"] if club_obj else None,
            "attributes": attrs,
            "positions": positions,
        }

    def edit_player(self, uid: int, column: str, value: Any) -> dict[str, Any]:
        row = self.db.player(int(uid))
        if not row:
            return {"ok": False, "error": f"Player {uid} not found."}
        col = str(column or "").strip()
        if col in DATE_COLUMNS:
            ok, msg = self.db.set_date(int(uid), col, str(value or ""))
            return {
                "ok": ok,
                "message": msg,
                "in_game": self.db.last_edit == "game",
                "player": self.get_player(uid),
                "status": self.status_payload(),
            }
        if col in spec.KNOWN_COLUMN_SET or col in (
            "condition", "match_sharpness", "jadedness", "morale", "ca", "pa",
            "asking_price_raw", "salary_weekly_raw", "height_cm",
            "home_reputation", "current_reputation", "world_reputation",
        ):
            parsed = _opt_int(value)
            if parsed is None:
                return {"ok": False, "error": f"Invalid number for {col}."}
            ok = self.db.set_value("players", row, col, parsed)
            if not ok:
                err = self.db.last_error or f"{col} is already {parsed}."
                return {
                    "ok": self.db.last_edit != "error",
                    "error": err if self.db.last_edit == "error" else "",
                    "message": err,
                    "player": self.get_player(uid),
                    "status": self.status_payload(),
                }
            applied = self.db.last_value if self.db.last_value is not None else parsed
            where = "in FM27" if self.db.last_edit == "game" else "staged locally"
            return {
                "ok": True,
                "applied": applied,
                "in_game": self.db.last_edit == "game",
                "message": f"{col.replace('_', ' ').title()} → {applied} ({where})",
                "player": self.get_player(uid),
                "status": self.status_payload(),
            }
        return {"ok": False, "error": f"Field {col!r} is not editable."}

    def player_preset(self, uid: int, preset: str) -> dict[str, Any]:
        row = self.db.player(int(uid))
        if not row:
            return {"ok": False, "error": f"Player {uid} not found."}
        kind = str(preset or "").strip().lower()
        if kind == "heal":
            healed, msg = self.db.heal_player(row)
            return {
                "ok": True,
                "in_game": bool(healed),
                "message": msg,
                "player": self.get_player(uid),
                "status": self.status_payload(),
            }
        if kind == "inspire":
            for col, val in (("morale", 100), ("jadedness", 0)):
                self.db.set_value("players", row, col, val)
            where = "in FM27" if (self.db.attached and not self.db.read_only) else "staged"
            return {
                "ok": True,
                "in_game": bool(self.db.attached and not self.db.read_only),
                "message": f"Inspired ({where}): morale 100, jadedness 0.",
                "player": self.get_player(uid),
                "status": self.status_payload(),
            }
        if kind == "wonderkid":
            self.db.set_value("players", row, "pa", 200)
            where = "in FM27" if (self.db.attached and not self.db.read_only) else "staged"
            return {
                "ok": True,
                "in_game": bool(self.db.attached and not self.db.read_only),
                "message": f"Wonderkid applied ({where}): PA → 200.",
                "player": self.get_player(uid),
                "status": self.status_payload(),
            }
        if kind == "destroy":
            for col, val in (("morale", 0), ("jadedness", 1000),
                             ("match_sharpness", 5000), ("condition", 5000)):
                self.db.set_value("players", row, col, val)
            where = "in FM27" if (self.db.attached and not self.db.read_only) else "staged"
            return {
                "ok": True,
                "in_game": bool(self.db.attached and not self.db.read_only),
                "message": f"Destroyed ({where}): morale 0, jadedness 1000, cond/sharp 5000.",
                "player": self.get_player(uid),
                "status": self.status_payload(),
            }
        return {"ok": False, "error": f"Unknown preset: {preset!r}"}

    def compare_players(self, uid1: int, uid2: int) -> dict[str, Any] | None:
        p1 = self.get_player(uid1)
        p2 = self.get_player(uid2)
        if not p1 or not p2:
            return None
        flat1: dict[str, int] = {}
        flat2: dict[str, int] = {}
        rows = []
        for group_name, fields in _ATTRIBUTE_GROUPS[:4]:
            for lbl, col in fields:
                v1 = next((x["value"] for x in p1["attributes"][group_name] if x["column"] == col), 0)
                v2 = next((x["value"] for x in p2["attributes"][group_name] if x["column"] == col), 0)
                flat1[col] = v1
                flat2[col] = v2
                rows.append({"group": group_name, "label": lbl, "column": col,
                             "a": v1, "b": v2, "diff": v1 - v2})
        both_gk = (p1.get("pos", "") == "GK" and p2.get("pos", "") == "GK")
        axes_spec = _GK_RADAR_AXES if both_gk else _OUTFIELD_RADAR_AXES
        radar = []
        for label, cols in axes_spec:
            vals1 = [flat1.get(c, 0) for c in cols]
            vals2 = [flat2.get(c, 0) for c in cols]
            a_avg = round(sum(vals1) / len(vals1), 1) if vals1 else 0.0
            b_avg = round(sum(vals2) / len(vals2), 1) if vals2 else 0.0
            radar.append({
                "label": label,
                "a": a_avg,
                "b": b_avg,
                "diff": round(a_avg - b_avg, 1),
            })
        overview = {
            "ca_diff": int(p1["ca"]) - int(p2["ca"]),
            "pa_diff": int(p1["pa"]) - int(p2["pa"]),
            "rating_diff": round(float(p1["rating"]) - float(p2["rating"]), 2),
            "role_score_diff": round(float(p1["best_role_score"]) - float(p2["best_role_score"]), 2),
            "value_diff": int(p1["value_raw"]) - int(p2["value_raw"]),
            "wage_diff": int(p1["wage_raw"]) - int(p2["wage_raw"]),
            "age_diff": int(p1["age"]) - int(p2["age"]),
        }
        return {"a": p1, "b": p2, "radar": radar, "overview": overview, "rows": rows}

    def get_club(self, key: str | int) -> dict[str, Any] | None:
        club = None
        if isinstance(key, int) or str(key).isdigit():
            club = self.db.club(int(key))
        if club is None:
            club = self.db.club_by_name(str(key))
        if club is None:
            return None
        raw_name = str(club.get("name") or "")
        squad_raw = self.db.squad(raw_name, 120)
        squad = [_player_summary(p) for p in squad_raw]
        staff = [_staff_summary(s) for s in self.db.query_staff(club=raw_name, limit=120)]
        next_fix = self.db.next_fixture(raw_name)
        fixtures = [dict(f) for f in self.db.fixtures(raw_name, 20)]
        fit_count = sum(1 for p in squad if not p["injured"])
        morales = [int(p["morale"]) for p in squad if p.get("morale") is not None]
        avg_morale = round(sum(morales) / len(morales), 1) if morales else 0
        total_wage = sum(p["wage_raw"] for p in squad)
        return {
            **_club_summary(club),
            "squad": squad,
            "staff": staff,
            "staff_counts": self.db.staff_counts(raw_name),
            "fit_count": fit_count,
            "squad_count": len(squad),
            "avg_morale": avg_morale,
            "weekly_wage_bill": _money(total_wage),
            "next_fixture": next_fix,
            "fixtures": fixtures,
        }

    def club_preset(self, key: str | int, preset: str) -> dict[str, Any]:
        club_data = self.get_club(key)
        if not club_data:
            return {"ok": False, "error": "Club not found."}
        squad = self.db.squad(club_data["raw_name"], 60)
        kind = str(preset or "").strip().lower()
        touched = 0
        if kind == "heal_squad":
            for p in squad:
                full = self.db.player(int(p["unique_id"]))
                if full:
                    self.db.heal_player(full)
                    touched += 1
            msg = f"Healed {touched} players in {club_data['name']}."
        elif kind == "inspire_squad":
            for p in squad:
                full = self.db.player(int(p["unique_id"]))
                if full:
                    self.db.set_value("players", full, "morale", 100)
                    self.db.set_value("players", full, "jadedness", 0)
                    touched += 1
            msg = f"Inspired {touched} players in {club_data['name']} (Morale 100, Jadedness 0)."
        else:
            return {"ok": False, "error": f"Unknown club preset: {preset!r}"}
        return {
            "ok": True,
            "in_game": bool(self.db.attached and not self.db.read_only),
            "message": msg,
            "club": self.get_club(club_data["id"]),
            "status": self.status_payload(),
        }

    def get_staff(self, uid: int) -> dict[str, Any] | None:
        row = self.db.staff_member(int(uid))
        if not row:
            return None
        summary = _staff_summary(row)
        club_raw = summary["club_raw"]
        club_obj = self.db.club_by_name(club_raw) if club_raw else None
        colleagues = (
            [_staff_summary(s) for s in self.db.query_staff(club=club_raw, limit=40)
             if int(s.get("unique_id") or s.get("id") or 0) != summary["unique_id"]]
            if club_raw else []
        )
        return {
            **summary,
            "dob": Snapshot.dob(row),
            "club_id": club_obj["id"] if club_obj else None,
            "colleagues": colleagues[:25],
        }

    def get_nation(self, name: str) -> dict[str, Any] | None:
        summary = self.db.nation_summary(name)
        if not summary:
            return None
        return {
            "name": summary["name"],
            "counts": summary["counts"],
            "best_ca": summary["best_ca"],
            "players": [_player_summary(p) for p in summary["players"]],
            "clubs": [_club_summary(c) for c in summary["clubs"]],
            "staff": [_staff_summary(s) for s in summary["staff"]],
            "competitions": summary["competitions"],
        }

    # -- search / transfers / retirements / history -----------------------
    def search(self, *, scope: str = "players", query: str = "", pos: str = "",
               job: str = "", min_age: int | None = None,
               max_age: int | None = None, min_ca: int | None = None,
               listed_only: bool = False, limit: int = 250) -> dict[str, Any]:
        scope_key = (scope or "players").strip().lower()
        cap = max(1, min(400, int(limit)))
        if scope_key == "staff":
            rows = [_staff_summary(s) for s in self.db.query_staff(
                query=query, job=(None if not job or job == "Any" else job),
                min_age=min_age, max_age=max_age, min_ca=min_ca, limit=cap)]
        elif scope_key == "clubs":
            rows = [_club_summary(c) for c in self.db.clubs(query, cap)]
        elif scope_key == "nations":
            rows = self.db.nations(query, cap)
        else:
            scope_key = "players"
            pos_val = None if not pos or pos == "Any" else pos
            rows = [_player_summary(p) for p in self.db.query_players(
                search=query or None,
                position=pos_val,
                position_min=15 if pos_val else None,
                min_age=min_age,
                max_age=max_age,
                min_ca=min_ca,
                transfer_listed=True if listed_only else None,
                order="ca",
                descending=True,
                limit=cap,
                columns=SQUAD_COLUMNS,
            )]
        return {"scope": scope_key, "count": len(rows), "rows": rows}

    def transfers(self, *, mode: str = "listed", query: str = "", limit: int = 250) -> dict[str, Any]:
        mode_key = (mode or "listed").strip().lower()
        agreed = self.db.future_transfers(query=query, limit=limit)
        if mode_key == "agreed":
            rows = [
                {
                    **_player_summary(p),
                    "to_club": Snapshot.display_club(p.get("future_transfer_club")),
                    "transfer_date": Snapshot.date(p.get("future_transfer_date")),
                }
                for p in agreed
            ]
        else:
            rows = [_player_summary(p) for p in self.db.transfer_market(
                kind=mode_key, query=query, limit=limit)]
        return {"mode": mode_key, "agreed_count": len(agreed), "count": len(rows), "rows": rows}

    def retirements(self, *, kind: str = "players", min_age: int | None = None,
                    query: str = "", limit: int = 250) -> dict[str, Any]:
        is_staff = (kind or "players").strip().lower() == "staff"
        age = min_age if min_age is not None else (60 if is_staff else 35)
        raw = self.db.retirements(min_age=age, query=query, include_staff=is_staff, limit=limit)
        rows = [_staff_summary(r) for r in raw] if is_staff else [_player_summary(r) for r in raw]
        return {"kind": "staff" if is_staff else "players", "min_age": age,
                "count": len(rows), "rows": rows}

    def history(self) -> dict[str, Any]:
        entries = []
        for action in reversed(self.db.journal[-100:]):
            for change in reversed(action.changes):
                target_label = f"{change.table.title()} #{change.row_id}"
                target_kind = change.table.rstrip("s")
                target_id = change.uid or change.row_id
                if change.table == "players":
                    p = self.db.player(change.uid) if change.uid else self.db.player_by_id(change.row_id)
                    if p:
                        target_label = Snapshot.display_name(p)
                        target_id = int(p["unique_id"])
                        target_kind = "player"
                elif change.table == "clubs":
                    c = self.db.club(change.row_id)
                    if c:
                        target_label = Snapshot.display_club(c["name"])
                        target_id = c["id"]
                        target_kind = "club"
                entries.append({
                    "key": change.key,
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "target_name": target_label,
                    "column": change.column,
                    "label": change.column.replace("_", " ").title(),
                    "old": change.old,
                    "new": change.new,
                    "in_game": bool(change.in_game or change.superseded),
                })
        return {
            "actions": len(self.db.journal),
            "redo": len(self.db.redo_stack),
            "dirty": self.db.dirty(),
            "entries": entries,
        }

    def undo(self) -> dict[str, Any]:
        res = self.db.undo()
        if not res:
            return {"ok": False, "message": "Nothing to undo.", "status": self.status_payload()}
        err = self.db.last_error if self.db.last_edit == "error" else ""
        return {
            "ok": True,
            "message": err or f"Undid {res[0].split(':')[-1]}.",
            "warning": bool(err),
            "status": self.status_payload(),
        }

    def redo(self) -> dict[str, Any]:
        res = self.db.redo()
        if not res:
            return {"ok": False, "message": "Nothing to redo.", "status": self.status_payload()}
        err = self.db.last_error if self.db.last_edit == "error" else ""
        return {
            "ok": True,
            "message": err or f"Redid {res[0].split(':')[-1]}.",
            "warning": bool(err),
            "status": self.status_payload(),
        }

    def discard_staged(self) -> dict[str, Any]:
        self.db.reset_edits()
        return {"ok": True, "message": "Discarded staged edits.", "status": self.status_payload()}

    def save_edits(self) -> dict[str, Any]:
        """Flush staged edits into FM27 when attached, or save local snapshot edits."""
        dirty = self.db.dirty()
        if self.db.attached and not self.db.read_only:
            written, failed = self.db.flush_pending()
            if written:
                msg = f"Flushed {written} edit(s) into FM27" + (f" ({failed} failed)." if failed else ".")
            elif failed:
                msg = f"{failed} staged edit(s) could not be written into FM27."
            else:
                msg = "All changes are already synced live in FM27."
            return {
                "ok": failed == 0,
                "written": written,
                "failed": failed,
                "in_game": True,
                "message": msg,
                "status": self.status_payload(),
            }
        if dirty > 0:
            self.db.save()
            return {
                "ok": True,
                "written": dirty,
                "in_game": False,
                "message": f"Saved {dirty} staged edit(s). Attach FM27 (Load Game) to apply in-game.",
                "status": self.status_payload(),
            }
        return {
            "ok": True,
            "written": 0,
            "in_game": False,
            "message": "No unsaved edits to save.",
            "status": self.status_payload(),
        }

    # -- tactics & training plans -----------------------------------------
    def tactic_plan(self, club_name: str = "", style: str = "Balanced") -> dict[str, Any]:
        club = (club_name or "").strip() or self.status_payload()["managed_club_raw"]
        res = fmplans.build_tactic_from_squad(self.db, club, style=style or "Balanced")
        if res.get("error") and "template" in str(res.get("error")).lower():
            # Fallback when FMRTE_GAME_DIR / Tactics/*.json is absent: compute a
            # real optimal 4-2-3-1 Best XI directly from the squad database.
            shape = [
                "Goalkeeper",
                "DefenderLeft", "DefenderCentral", "DefenderCentral", "DefenderRight",
                "DefensiveMidfielder", "DefensiveMidfielder",
                "AttackingMidfielderLeft", "AttackingMidfielderCentral", "AttackingMidfielderRight",
                "Striker",
            ]
            lineup = fmtools.execute_tool(
                self.db, "fm27_optimize_lineup",
                {"club": club, "shape": shape},
                default_club=club,
            )
            if lineup.get("error"):
                return lineup
            squad = self.db.squad(club, 300)
            by_id = {int(p.get("unique_id") or 0): p for p in squad}
            eleven = [by_id[r["player_unique_id"]]
                      for r in lineup.get("assignments", [])
                      if r.get("player_unique_id") in by_id]
            instructions = fmplans.derive_instructions(eleven) if eleven else fmplans.style_instructions(style or "Balanced")
            roles = [
                {
                    "slot": r["slot"],
                    "position": fmplans.short_label(shape[int(r["slot"]) - 1]),
                    "player_unique_id": r["player_unique_id"],
                    "player_name": r.get("player_name"),
                    "in_possession": {"role": fmplans.short_label(shape[int(r["slot"]) - 1]), "score": r.get("position_score")},
                    "out_of_possession": {"role": None, "score": None},
                }
                for r in lineup.get("assignments", [])
            ]
            return {
                "plan": f"4-2-3-1 — {Snapshot.display_club(club)}",
                "club": club,
                "template": "4-2-3-1",
                "shape": "4-2-3-1",
                "style": style or "Balanced",
                "roles": roles,
                "instructions": instructions,
                "team_score": lineup.get("team_score"),
                "writes_game": False,
            }
        return res

    def training_plan(self, focus: str = "Balanced", club_name: str = "") -> dict[str, Any]:
        club = (club_name or "").strip() or self.status_payload()["managed_club_raw"]
        week = fmplans.training_week(focus or "Balanced", f"{Snapshot.display_club(club)} — {focus or 'Balanced'}")
        return week

    # -- AI settings & chat -----------------------------------------------
    def ai_settings_payload(self) -> dict[str, Any]:
        with self._ai_lock:
            s = self.settings
            return {
                "provider": s.provider,
                "base_url": s.base_url,
                "model": s.model,
                "reasoning": s.reasoning,
                "temperature": s.temperature,
                "enabled": s.enabled,
                "has_key": self.keys.present(),
                "key_status": self.keys.describe(),
                "graphics_dir": self.faces.graphics_dir,
                "faces_available": self.faces.available,
                "providers": [
                    {"id": pid, "label": info["label"], "kind": info["kind"],
                     "default_url": info.get("base_url", ""), "default_model": info.get("model", ""),
                     "note": info.get("note", "")}
                    for pid, info in A.PROVIDERS.items()
                ],
            }

    def update_ai_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._ai_lock:
            s = self.settings
            for field_name in ("provider", "base_url", "model", "reasoning"):
                val = payload.get(field_name)
                if isinstance(val, str) and val.strip():
                    setattr(s, field_name, val.strip())
            if "temperature" in payload:
                try:
                    s.temperature = max(0.0, min(2.0, float(payload["temperature"])))
                except (TypeError, ValueError):
                    pass
            s.save()
            api_key = payload.get("api_key")
            if isinstance(api_key, str) and api_key.strip():
                self.keys.save(api_key.strip())
            elif payload.get("clear_key"):
                self.keys.clear()
            if "graphics_dir" in payload:
                gdir = str(payload.get("graphics_dir") or "").strip()
                self._prefs["graphics_dir"] = gdir
                _save_studio_prefs(self._prefs)
                self.faces.configure(gdir)
        return {"ok": True, "ai": self.ai_settings_payload()}

    def list_ai_models(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        with self._ai_lock:
            provider = str(body.get("provider") or self.settings.provider).strip()
            info = A.PROVIDERS.get(provider, A.PROVIDERS["commandcode"])
            base_url = str(body.get("base_url") or info.get("base_url") or self.settings.base_url).strip()
            model = str(body.get("model") or info.get("model") or self.settings.model).strip()
            temp_settings = A.AssistantSettings(
                provider=provider,
                base_url=base_url,
                model=model,
                reasoning=self.settings.reasoning,
                temperature=self.settings.temperature,
                enabled=self.settings.enabled,
            )
            api_key = str(body.get("api_key") or "").strip() or self.keys.load()
        try:
            models = A.list_models(temp_settings, api_key)
            return {
                "ok": True,
                "provider": provider,
                "count": len(models),
                "models": models,
            }
        except A.AssistantError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "hint": exc.hint or "",
                "models": [],
            }

    def prune_reloads(self, *, max_age_hours: float = 24.0, keep_latest: int = 3) -> dict[str, Any]:
        stats = Snapshot.prune_reloads(
            max_age_hours=max_age_hours,
            keep_latest=keep_latest,
            include_legacy_root=True,
        )
        return {
            "ok": True,
            "removed": stats["removed"],
            "remaining": stats["remaining"],
            "reloads_dir": Snapshot.reload_dir(),
            "message": (
                f"Cleaned {stats['removed']} stale reload folder(s)."
                if stats["removed"] else "Reload cache is already clean."
            ),
        }

    def clear_chat(self) -> dict[str, Any]:
        with self._ai_lock:
            self.conversation.clear()
        return {"ok": True}

    def _expand_slash(self, prompt: str, context: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        """Handle local instant slash commands or expand AI slash macros.

        Returns ``(expanded_prompt, instant_result_or_None)``.
        """
        text = prompt.strip()
        if not text.startswith("/"):
            return text, None
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        ctx_kind = context.get("kind") or ""
        ctx_id = context.get("id")
        ctx_name = context.get("name") or ""
        club = context.get("club") or self.status_payload()["managed_club_raw"]

        if cmd in ("/heal", "/inspire"):
            if ctx_kind == "player" and ctx_id:
                res = self.player_preset(int(ctx_id), cmd[1:])
                return None, {
                    "answer": f"**{cmd}** applied to **{ctx_name or ctx_id}**: {res.get('message')}",
                    "steps": [{"stage": "action", "label": f"Executed {cmd} on player #{ctx_id}", "ms": 8}],
                    "tools": [{"name": f"studio_{cmd[1:]}", "args": {"uid": int(ctx_id)},
                               "ms": 8, "ok": res.get("ok", False), "summary": res.get("message", "")}],
                    "refresh_entity": {"kind": "player", "id": int(ctx_id)},
                }
            if ctx_kind == "club" and ctx_id:
                res = self.club_preset(ctx_id, f"{cmd[1:]}_squad")
                return None, {
                    "answer": f"**{cmd}** applied to **{ctx_name or club}**: {res.get('message')}",
                    "steps": [{"stage": "action", "label": f"Executed {cmd} on squad", "ms": 25}],
                    "tools": [{"name": f"studio_{cmd[1:]}_squad", "args": {"club": club},
                               "ms": 25, "ok": res.get("ok", False), "summary": res.get("message", "")}],
                    "refresh_entity": {"kind": "club", "id": ctx_id},
                }
            return None, {
                "answer": f"Open a Player or Club tab first to run `{cmd}`.",
                "steps": [],
                "tools": [],
            }

        if cmd == "/analyze":
            target = rest or club
            return f"Analyze squad depth, weakest positions, morale and injury risks for {target}. Be concise with bullet points.", None
        if cmd == "/compare":
            if ctx_kind == "player" and ctx_name:
                target = rest or "Christian Pulisic"
                return f"Compare {ctx_name} (UID {ctx_id}) with {target} across attributes, role fit, and value.", None
            return f"Compare {rest or 'Kylian Mbappé and Christian Pulisic'} across attributes and role fit.", None
        if cmd == "/bestxi":
            target = rest or club
            return f"Build the optimal Best XI lineup and tactical instructions for {target} based on current squad attributes.", None
        if cmd == "/shortlist":
            target = rest or "U23 right-back or striker under £35M"
            return f"Find a realistic transfer shortlist for {club}: {target}. Include CA/PA, age, club and asking price.", None
        if cmd == "/train":
            target = rest or "Balanced match week"
            return f"Create a weekly FM27 training plan for {club} focused on: {target}.", None
        return text, None

    def stream_chat(self, prompt: str, context: dict[str, Any],
                    emit: Any) -> None:
        """Execute one chat turn, emitting SSE dicts to ``emit(dict)``."""
        raw_prompt = (prompt or "").strip()
        if not raw_prompt:
            emit({"type": "error", "message": "Please enter a question or /command."})
            return

        expanded, instant = self._expand_slash(raw_prompt, context or {})
        if instant is not None:
            for st in instant.get("steps", []):
                emit({"type": "step", **st})
            for tl in instant.get("tools", []):
                emit({"type": "tool_done", "id": "local_1", **tl})
            emit({
                "type": "done",
                "text": instant["answer"],
                "refresh_entity": instant.get("refresh_entity"),
                "status": self.status_payload(),
            })
            return

        # Inject active center-pane entity context transparently
        ctx_note = ""
        if context and context.get("kind") == "player" and context.get("id"):
            ctx_note = f"[Active screen context: Player {context.get('name')} (UID {context.get('id')}, Club {context.get('club')})]\n"
        elif context and context.get("kind") == "club" and context.get("name"):
            ctx_note = f"[Active screen context: Club {context.get('name')}]\n"
        elif context and context.get("kind") == "staff" and context.get("name"):
            ctx_note = f"[Active screen context: Staff {context.get('name')} (UID {context.get('id')})]\n"
        full_prompt = ctx_note + (expanded or raw_prompt)

        with self._ai_lock:
            settings = self.settings
            key = self.keys.load()
            history = [{"role": m.role, "text": m.text}
                       for m in self.conversation.messages[-12:] if not m.failed]
            self.conversation.add("user", raw_prompt)

        club_name = (context or {}).get("club") or self.status_payload()["managed_club_raw"]
        t0 = time.perf_counter()
        try:
            answer = A.chat_with_tools(
                settings, key, self.db, full_prompt,
                history=history,
                club_name=club_name,
                on_event=emit,
            )
            with self._ai_lock:
                self.conversation.add("assistant", answer)
            emit({
                "type": "done",
                "text": answer,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "status": self.status_payload(),
            })
        except A.AssistantError as exc:
            with self._ai_lock:
                self.conversation.add("assistant", str(exc), failed=True)
            emit({"type": "error", "message": str(exc), "hint": exc.hint or ""})
        except Exception as exc:  # noqa: BLE001
            with self._ai_lock:
                self.conversation.add("assistant", f"Assistant error: {exc}", failed=True)
            emit({"type": "error", "message": f"Assistant failed: {exc}"})


# ---------------------------------------------------------------------------
# Threaded local HTTP + SSE Server
# ---------------------------------------------------------------------------

def _make_handler(backend: StudioBackend, token: str):
    class StudioHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _check_token(self, query_params: dict[str, list[str]]) -> bool:
            hdr = self.headers.get("X-Studio-Token", "")
            q_tok = (query_params.get("token") or [""])[0]
            return secrets.compare_digest(hdr or q_tok, token)

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 2_000_000:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}

        def _serve_static(self, rel_path: str) -> None:
            cleaned = rel_path.lstrip("/") or "index.html"
            if cleaned == "" or cleaned == "/":
                cleaned = "index.html"
            target = os.path.normpath(os.path.join(STATIC_DIR, cleaned))
            if not target.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(target):
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            mime, _ = mimetypes.guess_type(target)
            with open(target, "rb") as handle:
                data = handle.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", (mime or "application/octet-stream") + (
                "; charset=utf-8" if (mime or "").startswith("text/") or target.endswith(".js") else ""
            ))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if not path.startswith("/api/"):
                self._serve_static(path)
                return
            if not self._check_token(qs):
                self._send_json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return

            q1 = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731
            if path == "/api/bootstrap":
                self._send_json(backend.bootstrap())
            elif path == "/api/status":
                self._send_json(backend.status_payload())
            elif path == "/api/search":
                self._send_json(backend.search(
                    scope=q1("scope", "players"),
                    query=q1("q", ""),
                    pos=q1("pos", ""),
                    job=q1("job", ""),
                    min_age=_opt_int(q1("min_age")),
                    max_age=_opt_int(q1("max_age")),
                    min_ca=_opt_int(q1("min_ca")),
                    listed_only=q1("listed") in ("1", "true", "yes"),
                    limit=_opt_int(q1("limit")) or 250,
                ))
            elif path == "/api/transfers":
                self._send_json(backend.transfers(mode=q1("mode", "listed"), query=q1("q", "")))
            elif path == "/api/retirements":
                self._send_json(backend.retirements(
                    kind=q1("kind", "players"),
                    min_age=_opt_int(q1("min_age")),
                    query=q1("q", ""),
                ))
            elif path == "/api/history":
                self._send_json(backend.history())
            elif path == "/api/plans/tactic":
                self._send_json(backend.tactic_plan(q1("club", ""), q1("style", "Balanced")))
            elif path == "/api/plans/training":
                self._send_json(backend.training_plan(q1("focus", "Balanced"), q1("club", "")))
            elif path == "/api/ai/settings":
                self._send_json(backend.ai_settings_payload())
            elif path.startswith("/api/player/"):
                parts = path.split("/")
                uid = _opt_int(parts[3]) if len(parts) >= 4 else None
                if uid is None:
                    self._send_json({"error": "Invalid player id"}, HTTPStatus.BAD_REQUEST)
                    return
                if len(parts) >= 5 and parts[4] == "compare":
                    other = _opt_int(q1("other"))
                    res = backend.compare_players(uid, other) if other else None
                    if not res:
                        self._send_json({"error": "Players not found"}, HTTPStatus.NOT_FOUND)
                        return
                    self._send_json(res)
                    return
                data = backend.get_player(uid)
                if not data:
                    self._send_json({"error": "Player not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(data)
            elif path.startswith("/api/club/"):
                key = urllib.parse.unquote(path[len("/api/club/"):])
                data = backend.get_club(key)
                if not data:
                    self._send_json({"error": "Club not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(data)
            elif path.startswith("/api/staff/"):
                uid = _opt_int(path[len("/api/staff/"):])
                data = backend.get_staff(uid) if uid is not None else None
                if not data:
                    self._send_json({"error": "Staff not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(data)
            elif path.startswith("/api/avatar/"):
                parts = path.split("/")
                uid = _opt_int(parts[4]) if len(parts) >= 5 else None
                file_path = backend.faces.resolve(uid) if uid else None
                if not file_path:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "public, max-age=300")
                    self.end_headers()
                    return
                mime, _ = mimetypes.guess_type(file_path)
                try:
                    with open(file_path, "rb") as handle:
                        img = handle.read()
                except OSError:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime or "image/png")
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(img)
            elif path.startswith("/api/nation/"):
                name = urllib.parse.unquote(path[len("/api/nation/"):])
                data = backend.get_nation(name)
                if not data:
                    self._send_json({"error": "Nation not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(data)
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if not self._check_token(qs):
                self._send_json({"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            body = self._read_json()

            if path == "/api/attach":
                self._send_json(backend.attach_game(refresh_all=bool(body.get("refresh_all"))))
            elif path == "/api/detach":
                self._send_json(backend.detach_game())
            elif path == "/api/undo":
                self._send_json(backend.undo())
            elif path == "/api/redo":
                self._send_json(backend.redo())
            elif path == "/api/discard":
                self._send_json(backend.discard_staged())
            elif path == "/api/save":
                self._send_json(backend.save_edits())
            elif path == "/api/ai/settings":
                self._send_json(backend.update_ai_settings(body))
            elif path == "/api/ai/models":
                self._send_json(backend.list_ai_models(body))
            elif path == "/api/saves/prune":
                self._send_json(backend.prune_reloads(
                    max_age_hours=float(body.get("max_age_hours", 0.0)),
                    keep_latest=int(body.get("keep_latest", 0)),
                ))
            elif path == "/api/ai/clear":
                self._send_json(backend.clear_chat())
            elif path == "/api/ai/chat":
                self._stream_ai_chat(body)
            elif path.startswith("/api/player/"):
                parts = path.split("/")
                uid = _opt_int(parts[3]) if len(parts) >= 4 else None
                action = parts[4] if len(parts) >= 5 else ""
                if uid is None:
                    self._send_json({"error": "Invalid player id"}, HTTPStatus.BAD_REQUEST)
                    return
                if action == "edit":
                    self._send_json(backend.edit_player(uid, body.get("column", ""), body.get("value")))
                elif action == "preset":
                    self._send_json(backend.player_preset(uid, body.get("preset", "")))
                else:
                    self._send_json({"error": "Unknown player action"}, HTTPStatus.NOT_FOUND)
            elif path.startswith("/api/club/"):
                parts = path.split("/")
                key = urllib.parse.unquote(parts[3]) if len(parts) >= 4 else ""
                action = parts[4] if len(parts) >= 5 else ""
                if action == "preset":
                    self._send_json(backend.club_preset(key, body.get("preset", "")))
                else:
                    self._send_json({"error": "Unknown club action"}, HTTPStatus.NOT_FOUND)
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

        def _stream_ai_chat(self, body: dict[str, Any]) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            events: queue.Queue[dict[str, Any] | None] = queue.Queue()

            def runner() -> None:
                try:
                    backend.stream_chat(
                        str(body.get("prompt") or ""),
                        body.get("context") if isinstance(body.get("context"), dict) else {},
                        events.put,
                    )
                finally:
                    events.put(None)

            threading.Thread(target=runner, daemon=True).start()
            while True:
                item = events.get()
                if item is None:
                    break
                payload = f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n".encode("utf-8")
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except OSError:
                    break

    return StudioHandler


class StudioServer:
    """Lifecycle wrapper for the local loopback Studio server."""

    def __init__(self, backend: StudioBackend | None = None,
                 host: str = "127.0.0.1", port: int = 0,
                 prune_interval_seconds: float = 1800.0):
        self.backend = backend if backend is not None else StudioBackend()
        self._owns_backend = backend is None
        self.token = secrets.token_urlsafe(24)
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(self.backend, self.token))
        self.host, self.port = self.httpd.server_address
        self._thread: threading.Thread | None = None
        self._prune_stop = threading.Event()
        self._prune_interval = max(60.0, float(prune_interval_seconds))
        self._prune_thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def _periodic_prune_loop(self) -> None:
        while not self._prune_stop.wait(self._prune_interval):
            try:
                Snapshot.prune_reloads(max_age_hours=24.0, keep_latest=3, include_legacy_root=True)
            except Exception:
                pass

    def start(self) -> str:
        try:
            Snapshot.prune_reloads(max_age_hours=24.0, keep_latest=3, include_legacy_root=True)
        except Exception:
            pass
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self._prune_stop.clear()
        self._prune_thread = threading.Thread(target=self._periodic_prune_loop, daemon=True)
        self._prune_thread.start()
        return self.url

    def stop(self) -> None:
        self._prune_stop.set()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        if self._owns_backend:
            self.backend.close()
