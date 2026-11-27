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
import re
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import assistant as A
from . import fmplans, fmtools, ratings, spec, staffratings, tacticslab, tacticweb
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
    if preferred and preferred.strip():
        pref = preferred.strip()
        if os.path.isdir(pref):
            return pref
    candidates: list[str] = []
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


_CONFIG_XML_SKIP_DIRS = frozenset({
    "faces", "iconfaces", "kits", "players", "trophies",
    "backgrounds", "skins", "stadiums", "2d", "3d", "panels",
})
_CONFIG_RECORD_RE = re.compile(
    r'<record\b([^>]+)/?>', re.IGNORECASE,
)
_ATTR_FROM_RE = re.compile(r'\bfrom="([^"]+)"', re.IGNORECASE)
_ATTR_TO_RE = re.compile(
    r'\bto="graphics/pictures/(club|nation)/(\d+)/(logo|icon|normal|small)"',
    re.IGNORECASE,
)
_CLUB_PREFIX_RE = re.compile(
    r"^(?:ac|fc|sc|as|ss|us|cf|cd|rc|rcd|sd|ud|fk|sk|nk|sv|vfl|vfb|tsg|rb|afc|ogc|osc)\s+",
    re.IGNORECASE,
)
_CLUB_SUFFIX_RE = re.compile(
    r"\s+(?:fc|cf|sc|ac|fk|sk|afc|bc|calcio)$",
    re.IGNORECASE,
)
_CLUB_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "ac milan": ("milan",),
    "milan": ("ac milan",),
    "inter milan": ("inter", "internazionale", "fc internazionale milano"),
    "inter": ("internazionale", "inter milan", "fc internazionale milano"),
    "man city": ("manchester city", "manchester city fc"),
    "man utd": ("manchester united", "manchester united fc"),
    "manchester utd": ("manchester united", "manchester united fc"),
    "spurs": ("tottenham hotspur", "tottenham"),
    "wolves": ("wolverhampton wanderers", "wolverhampton"),
    "psg": ("paris saint-germain", "paris sg"),
    "bayern": ("fc bayern münchen", "fc bayern munich", "bayern munich"),
    "barca": ("fc barcelona", "barcelona"),
    "atletico madrid": ("atlético de madrid", "atlético madrid", "atletico de madrid"),
}
_NATION_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "中国": ("中华人民共和国", "china pr", "china"),
    "中华人民共和国": ("中国", "china pr", "china"),
    "美国": ("美利坚合众国", "u.s.a.", "usa", "united states"),
    "美利坚合众国": ("美国", "u.s.a.", "usa", "united states"),
    "韩国": ("大韩民国", "south korea", "korea republic"),
    "朝鲜": ("朝鲜民主主义人民共和国", "north korea"),
    "捷克": ("捷克共和国", "czech republic", "czechia"),
    "捷克共和国": ("捷克", "czechia", "czech republic"),
    "荷兰": ("尼德兰", "netherlands", "holland"),
    "英格兰": ("england",),
    "意大利": ("italy",),
    "西班牙": ("spain",),
    "德国": ("germany",),
    "法国": ("france",),
    "巴西": ("brazil",),
    "阿根廷": ("argentina",),
    "葡萄牙": ("portugal",),
}


def _entity_uids_cache_path() -> str:
    return os.path.join(A.data_directory(), "entity_uids.json")


class _FaceResolver:
    """Fast O(1) resolver for FM27 facepacks, club logos, and nation flags."""

    def __init__(self, graphics_dir: str = ""):
        self._lock = threading.Lock()
        self._logo_lock = threading.Lock()
        self._cache: dict[int, str | None] = {}
        self._club_logo_cache: dict[str, str | None] = {}
        self._nation_logo_cache: dict[str, str | None] = {}
        self._club_logos: dict[int, str] = {}
        self._nation_logos: dict[int, str] = {}
        self._club_uids: dict[str, int] = {}
        self._nation_uids: dict[str, int] = {}
        self._logo_index_ready = False
        self.graphics_dir = ""
        self._search_dirs: list[str] = []
        self._load_entity_uids()
        self.configure(graphics_dir)

    def _load_entity_uids(self) -> None:
        try:
            with open(_entity_uids_cache_path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                clubs = data.get("clubs")
                nations = data.get("nations")
                if isinstance(clubs, dict):
                    self._club_uids = {str(k).strip().lower(): int(v) for k, v in clubs.items() if k and v}
                if isinstance(nations, dict):
                    self._nation_uids = {str(k).strip().lower(): int(v) for k, v in nations.items() if k and v}
        except (OSError, ValueError, TypeError):
            pass

    def _save_entity_uids(self) -> None:
        try:
            path = _entity_uids_cache_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {"clubs": self._club_uids, "nations": self._nation_uids},
                    handle, ensure_ascii=False, separators=(",", ":"),
                )
            os.replace(tmp, path)
        except OSError:
            pass

    def warm_entity_uids_from_game(self, game: Any) -> tuple[int, int]:
        """Populate club & nation UID maps from live FM27 memory and persist cache."""
        nations: dict[str, int] = {}
        clubs: dict[str, int] = {}
        try:
            if hasattr(game, "read_nation_uid_map"):
                nations = game.read_nation_uid_map() or {}
        except Exception:  # noqa: BLE001
            nations = {}
        try:
            if hasattr(game, "read_club_uid_map"):
                clubs = game.read_club_uid_map() or {}
        except Exception:  # noqa: BLE001
            clubs = {}
        if nations or clubs:
            with self._lock:
                if nations:
                    self._nation_uids.update(nations)
                    self._nation_logo_cache.clear()
                if clubs:
                    self._club_uids.update(clubs)
                    self._club_logo_cache.clear()
            self._save_entity_uids()
        return len(clubs), len(nations)

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
            self._club_logo_cache.clear()
            self._nation_logo_cache.clear()
        with self._logo_lock:
            self._club_logos.clear()
            self._nation_logos.clear()
            self._logo_index_ready = False

    def ensure_logo_index(self) -> None:
        """Index ``config.xml`` club/nation logo mappings under ``graphics_dir`` once."""
        if self._logo_index_ready:
            return
        with self._logo_lock:
            if self._logo_index_ready:
                return
            root_dir = self.graphics_dir
            club_map: dict[int, str] = {}
            club_prio: dict[int, int] = {}
            nation_map: dict[int, str] = {}
            nation_prio: dict[int, int] = {}
            if root_dir and os.path.isdir(root_dir):
                for cur, dirnames, filenames in os.walk(root_dir):
                    dirnames[:] = [
                        d for d in dirnames
                        if d.lower() not in _CONFIG_XML_SKIP_DIRS and not d.startswith(".")
                    ]
                    cfg_name = next((f for f in filenames if f.lower() == "config.xml"), None)
                    if not cfg_name:
                        continue
                    cfg_path = os.path.join(cur, cfg_name)
                    try:
                        with open(cfg_path, "r", encoding="utf-8", errors="ignore") as handle:
                            text = handle.read()
                    except OSError:
                        continue
                    if "graphics/pictures/" not in text.lower():
                        continue
                    file_lookup = {os.path.splitext(f)[0].lower(): f for f in filenames
                                   if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))}
                    for rec_m in _CONFIG_RECORD_RE.finditer(text):
                        attrs = rec_m.group(1)
                        to_m = _ATTR_TO_RE.search(attrs)
                        if not to_m:
                            continue
                        from_m = _ATTR_FROM_RE.search(attrs)
                        if not from_m:
                            continue
                        kind = to_m.group(1).lower()
                        uid = int(to_m.group(2))
                        variant = to_m.group(3).lower()
                        prio = 2 if variant in ("logo", "normal") else 1
                        rel_from = from_m.group(1).strip().replace("\\", "/")
                        if "/" in rel_from:
                            base_p = os.path.join(cur, *rel_from.split("/"))
                            resolved_file = None
                            for ext in ("", ".png", ".jpg", ".webp"):
                                cand = base_p + ext
                                if os.path.isfile(cand):
                                    resolved_file = cand
                                    break
                        else:
                            stem = os.path.splitext(rel_from)[0].lower()
                            fname = file_lookup.get(stem)
                            resolved_file = os.path.join(cur, fname) if fname else None
                        if not resolved_file:
                            continue
                        if kind == "club":
                            if prio > club_prio.get(uid, 0):
                                club_map[uid] = resolved_file
                                club_prio[uid] = prio
                        else:
                            if prio > nation_prio.get(uid, 0):
                                nation_map[uid] = resolved_file
                                nation_prio[uid] = prio
            self._club_logos = club_map
            self._nation_logos = nation_map
            self._logo_index_ready = True

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

    def _direct_logo_file(self, uid: int, subdirs: tuple[str, ...]) -> str | None:
        root = self.graphics_dir
        if not uid or not root or not os.path.isdir(root):
            return None
        for sub in subdirs:
            folder = os.path.join(root, sub)
            if not os.path.isdir(folder):
                continue
            for ext in (".png", ".jpg", ".webp"):
                cand = os.path.join(folder, f"{uid}{ext}")
                if os.path.isfile(cand):
                    return cand
        return None

    def _lookup_club_uid(self, name: str) -> int | None:
        raw = (name or "").strip()
        if not raw:
            return None
        disp = Snapshot.display_club(raw).strip()
        candidates: list[str] = []
        for s in (raw, disp):
            low = s.lower()
            if low and low not in candidates:
                candidates.append(low)
            for alias in _CLUB_NAME_ALIASES.get(low, ()):
                if alias not in candidates:
                    candidates.append(alias)
            stripped = _CLUB_PREFIX_RE.sub("", low).strip()
            if stripped and stripped not in candidates:
                candidates.append(stripped)
            stripped2 = _CLUB_SUFFIX_RE.sub("", stripped).strip()
            if stripped2 and stripped2 not in candidates:
                candidates.append(stripped2)
        with self._lock:
            for key in candidates:
                uid = self._club_uids.get(key)
                if uid:
                    return uid
        return None

    def _lookup_nation_uid(self, name: str) -> int | None:
        raw = (name or "").strip()
        if not raw:
            return None
        low = raw.lower()
        candidates = [low, *_NATION_NAME_ALIASES.get(low, ())]
        if raw.endswith("共和国") and len(raw) > 3:
            candidates.append(raw[:-3].lower())
        with self._lock:
            for key in candidates:
                uid = self._nation_uids.get(key)
                if uid:
                    return uid
        return None

    def resolve_club(self, name: str = "", *, uid: int | None = None) -> str | None:
        if not self.graphics_dir:
            return None
        self.ensure_logo_index()
        cache_key = f"uid:{uid}" if uid else f"name:{(name or '').strip().lower()}"
        with self._lock:
            if cache_key in self._club_logo_cache:
                return self._club_logo_cache[cache_key]
        target_uid = int(uid) if uid else self._lookup_club_uid(name)
        found: str | None = None
        if target_uid:
            found = self._club_logos.get(target_uid) or self._direct_logo_file(
                target_uid, ("logos", "clubs", "pictures/club", "pictures/logos"),
            )
        if found or self._club_uids:
            with self._lock:
                if len(self._club_logo_cache) > 8192:
                    self._club_logo_cache.clear()
                self._club_logo_cache[cache_key] = found
        return found

    def resolve_nation(self, name: str = "", *, uid: int | None = None) -> str | None:
        if not self.graphics_dir:
            return None
        self.ensure_logo_index()
        cache_key = f"uid:{uid}" if uid else f"name:{(name or '').strip().lower()}"
        with self._lock:
            if cache_key in self._nation_logo_cache:
                return self._nation_logo_cache[cache_key]
        target_uid = int(uid) if uid else self._lookup_nation_uid(name)
        found: str | None = None
        if target_uid:
            found = self._nation_logos.get(target_uid) or self._direct_logo_file(
                target_uid, ("flags", "nations", "pictures/nation", "Others/Federations"),
            )
        if found or self._nation_uids:
            with self._lock:
                if len(self._nation_logo_cache) > 2048:
                    self._nation_logo_cache.clear()
                self._nation_logo_cache[cache_key] = found
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


_STAFF_ATTRIBUTE_GROUPS = (
    ("Coaching", [
        ("Attacking", "attacking"),
        ("Defending", "defending"),
        ("Fitness", "fitness"),
        ("Mental", "mental"),
        ("Tactical", "tactical"),
        ("Technical", "technical"),
        ("Working With Youngsters", "working_with_youngsters"),
        ("Set Pieces", "set_pieces"),
    ]),
    ("Goalkeeping", [
        ("GK Shot Stopping", "gk_shot_stopping"),
        ("GK Distribution", "gk_distribution"),
        ("GK Handling", "gk_handling"),
    ]),
    ("Mental", [
        ("Adaptability", "adaptability"),
        ("Determination", "determination"),
        ("Level of Discipline", "level_of_discipline"),
        ("Man Management", "man_management"),
        ("Motivating", "motivating"),
        ("Tactical Knowledge", "tactical_knowledge"),
    ]),
    ("Scouting & Medical", [
        ("Judging Player Ability", "judging_player_ability"),
        ("Judging Player Potential", "judging_player_potential"),
        ("Judging Staff Ability", "judging_staff_ability"),
        ("Negotiating", "negotiating"),
        ("Physiotherapy", "physiotherapy"),
        ("Sports Science", "sports_science"),
        ("Analysing Data", "analysing_data"),
    ]),
    ("Personality", [
        ("Ambition", "ambition"),
        ("Loyalty", "loyalty"),
        ("Pressure", "pressure"),
        ("Professionalism", "professionalism"),
        ("Sportsmanship", "sportsmanship"),
        ("Temperament", "temperament"),
        ("Controversy", "controversy"),
    ]),
)

_STAFF_ALL_ATTR_COLS = frozenset(
    col for _, fields in _STAFF_ATTRIBUTE_GROUPS for _, col in fields
)


def _chat_sessions_path() -> str:
    return os.path.join(A.data_directory(), "chat_sessions.json")


class _ChatSessionStore:
    """Thread-safe persistent multi-session AI chat history store."""

    def __init__(self, path: str | None = None):
        self._lock = threading.RLock()
        self.path = path or _chat_sessions_path()
        self.active_id: str = ""
        self.sessions: list[dict[str, Any]] = []
        self._load()

    def _new_empty(self, title: str = "New Chat") -> dict[str, Any]:
        now = int(time.time())
        return {
            "id": secrets.token_hex(6),
            "title": title,
            "created_at": now,
            "updated_at": now,
            "turns": [],
        }

    def _load(self) -> None:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("sessions"), list):
                    self.sessions = [s for s in data["sessions"] if isinstance(s, dict) and s.get("id")]
                    self.active_id = str(data.get("active_id") or "")
            except (OSError, ValueError):
                self.sessions = []
                self.active_id = ""
            if not self.sessions:
                first = self._new_empty("New Chat")
                self.sessions = [first]
                self.active_id = first["id"]
            elif not any(s["id"] == self.active_id for s in self.sessions):
                self.active_id = self.sessions[0]["id"]

    def _save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(
                        {"active_id": self.active_id, "sessions": self.sessions[:40]},
                        handle, ensure_ascii=False, indent=1, default=str,
                    )
                os.replace(tmp, self.path)
            except OSError:
                pass

    def active_session(self) -> dict[str, Any]:
        with self._lock:
            cur = next((s for s in self.sessions if s["id"] == self.active_id), None)
            if cur is None:
                cur = self._new_empty("New Chat")
                self.sessions.insert(0, cur)
                self.active_id = cur["id"]
                self._save()
            return cur

    def list_payload(self) -> dict[str, Any]:
        with self._lock:
            cur = self.active_session()
            ordered = sorted(self.sessions, key=lambda s: int(s.get("updated_at") or 0), reverse=True)
            return {
                "ok": True,
                "active_id": cur["id"],
                "active": {
                    "id": cur["id"],
                    "title": cur.get("title") or "New Chat",
                    "turns": list(cur.get("turns") or []),
                },
                "sessions": [
                    {
                        "id": s["id"],
                        "title": s.get("title") or "New Chat",
                        "updated_at": int(s.get("updated_at") or 0),
                        "turn_count": len(s.get("turns") or []),
                    }
                    for s in ordered
                ],
            }

    def create_session(self, title: str = "New Chat") -> dict[str, Any]:
        with self._lock:
            cur = self.active_session()
            # Reuse current session if it is already empty
            if not (cur.get("turns") or []):
                return self.list_payload()
            sess = self._new_empty(title or "New Chat")
            self.sessions.insert(0, sess)
            self.active_id = sess["id"]
            self._save()
            return self.list_payload()

    def select_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            sid = (session_id or "").strip()
            found = next((s for s in self.sessions if s["id"] == sid), None)
            if found is None:
                return {"ok": False, "error": "Chat session not found."}
            self.active_id = found["id"]
            self._save()
            return self.list_payload()

    def delete_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            sid = (session_id or "").strip() or self.active_id
            self.sessions = [s for s in self.sessions if s["id"] != sid]
            if not self.sessions:
                fresh = self._new_empty("New Chat")
                self.sessions = [fresh]
                self.active_id = fresh["id"]
            elif self.active_id == sid:
                self.sessions.sort(key=lambda s: int(s.get("updated_at") or 0), reverse=True)
                self.active_id = self.sessions[0]["id"]
            self._save()
            return self.list_payload()

    def clear_active(self) -> dict[str, Any]:
        with self._lock:
            cur = self.active_session()
            cur["turns"] = []
            cur["title"] = "New Chat"
            cur["updated_at"] = int(time.time())
            self._save()
            return self.list_payload()

    def append_turns(self, user_turn: dict[str, Any], assistant_turn: dict[str, Any]) -> None:
        with self._lock:
            cur = self.active_session()
            turns = cur.setdefault("turns", [])
            if not turns or cur.get("title") in ("", "New Chat"):
                raw_t = str(user_turn.get("text") or "").strip()
                if raw_t:
                    cur["title"] = (raw_t[:36] + "…") if len(raw_t) > 36 else raw_t
            # Strip heavy base64 image payloads from persisted attachments to keep file compact
            u_clean = dict(user_turn)
            if isinstance(u_clean.get("attachments"), list):
                u_clean["attachments"] = [
                    {"kind": a.get("kind"), "name": a.get("name")}
                    for a in u_clean["attachments"] if isinstance(a, dict)
                ]
            turns.append(u_clean)
            turns.append(assistant_turn)
            if len(turns) > 120:
                cur["turns"] = turns[-120:]
            cur["updated_at"] = int(time.time())
            self._save()


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


# Tactical position order: GK -> CB -> FB/WB -> DM -> CM -> Wingers/Wide Mid -> AM -> ST
_POSITION_SORT_ORDER: tuple[tuple[str, int], ...] = (
    ("goalkeeper", 10),
    ("defender_central", 20),
    ("sweeper", 21),
    ("defender_left", 30),
    ("defender_right", 31),
    ("wing_back_left", 32),
    ("wing_back_right", 33),
    ("defensive_midfielder", 40),
    ("midfielder_central", 50),
    ("attacking_midfielder_left", 60),
    ("attacking_midfielder_right", 61),
    ("midfielder_left", 62),
    ("midfielder_right", 63),
    ("attacking_midfielder_central", 70),
    ("striker", 80),
)


def _position_sort_rank(row: dict[str, Any]) -> int:
    """Numeric tactical rank for position sorting (GK -> CB -> FB/WB -> DM -> CM -> Winger -> AM -> ST)."""
    best_val = -1
    best_rank = 99
    for col, rank in _POSITION_SORT_ORDER:
        val = int(row.get(col) or 0)
        if val > best_val:
            best_val = val
            best_rank = rank
    return best_rank


def _loan_info(row: dict[str, Any], club_context: str = "") -> tuple[str, str, str]:
    """Return ``(loan_status, parent_club, loan_to_club)`` relative to ``club_context``.

    ``loan_status`` is ``"in"`` (loaned in to this club), ``"out"`` (owned by this club
    and loaned out elsewhere), or ``""`` (regular squad player).
    """
    own = str(row.get("club") or "").strip()
    playing = str(row.get("playing_club") or "").strip()
    loan_club = str(row.get("loan_club") or "").strip()
    is_out = str(row.get("is_loaned_out") or "").strip().lower() in ("yes", "1", "true")
    ctx = (club_context or "").strip().lower()
    if ctx:
        if (playing.lower() == ctx or loan_club.lower() == ctx) and own.lower() != ctx:
            return "in", Snapshot.display_club(own), Snapshot.display_club(playing or loan_club)
        if own.lower() == ctx and (
            is_out
            or (playing and playing.lower() != ctx)
            or (loan_club and loan_club.lower() != ctx)
        ):
            dest = loan_club or playing
            return "out", Snapshot.display_club(own), Snapshot.display_club(dest)
        return "", Snapshot.display_club(own), ""
    if is_out or (playing and own and playing.lower() != own.lower()):
        return "out", Snapshot.display_club(own), Snapshot.display_club(loan_club or playing)
    return "", Snapshot.display_club(own), ""


def _player_summary(row: dict[str, Any], club_context: str = "") -> dict[str, Any]:
    loan_status, parent_club, loan_to = _loan_info(row, club_context=club_context)
    has_attrs = "finishing" in row or "reflexes" in row
    rating_val = round(Snapshot.rating(row), 2) if has_attrs else None
    return {
        "id": row.get("id"),
        "unique_id": int(row.get("unique_id") or 0),
        "name": Snapshot.display_name(row),
        "full_name": row.get("name") or "",
        "club": Snapshot.display_club(row.get("club")),
        "club_raw": row.get("club") or "",
        "playing_club": Snapshot.display_club(row.get("playing_club") or row.get("club")),
        "loan_status": loan_status,
        "loan_from": parent_club if loan_status == "in" else "",
        "loan_to": loan_to if loan_status == "out" else "",
        "age": Snapshot.age(row),
        "nationality": row.get("nationality") or "—",
        "pos": Snapshot.position_text(row),
        "pos_rank": _position_sort_rank(row),
        "role": Snapshot.role_name(row),
        "rating": rating_val,
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
        self.chat_store = _ChatSessionStore()
        self._sync_conversation_from_active_session()
        self._attach_lock = threading.Lock()
        self._live_sync_ready = threading.Event()
        self._staff_live_cache: dict[int, dict[str, Any]] = {}
        self._prefs = _load_studio_prefs()
        self.faces = _FaceResolver(str(self._prefs.get("graphics_dir") or ""))
        self.lab = tacticslab.TacticsLabClient(str(self._prefs.get("lab_dir") or ""))
        fmtools.set_lab_client(self.lab)
        if self.faces.graphics_dir:
            threading.Thread(
                target=self.faces.ensure_logo_index,
                name="fm27-logo-index",
                daemon=True,
            ).start()

    def _sync_conversation_from_active_session(self) -> None:
        cur = self.chat_store.active_session()
        self.conversation.clear()
        for t in (cur.get("turns") or []):
            role = t.get("role")
            text = str(t.get("text") or "")
            if role in ("user", "assistant") and text:
                self.conversation.add(role, text, failed=bool(t.get("error")))

    def close(self) -> None:
        try:
            self.lab.stop()
        except Exception:
            pass
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
            "staff_jobs": self.db.staff_jobs(30),
            "staff_categories": staffratings.categories_catalogue(),
            "default_players": top_players[:5],
            "ai": self.ai_settings_payload(),
        }

    # -- live attach / detach ---------------------------------------------
    def attach_game(self, *, refresh_all: bool = False) -> dict[str, Any]:
        with self._attach_lock:
            from . import livewrite

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
            club_finances = None
            sync_err = ""
            if refresh_all:
                self._live_sync_ready.clear()
                try:
                    game = getattr(editor, "game", None)
                    if game is not None and hasattr(game, "read_live_snapshot"):
                        records, club_finances = game.read_live_snapshot(
                            self.db.unique_ids(),
                            game_date=report.game_date,
                            include_clubs=True,
                        )
                except Exception as exc:  # noqa: BLE001
                    records = None
                    club_finances = None
                    sync_err = str(exc)

            synced = self.db.attach_live(
                editor,
                records=records,
                report=report,
                club_finances=club_finances,
            )
            self._staff_live_cache.clear()
            if refresh_all and records is not None:
                self._live_sync_ready.set()
            else:
                self._live_sync_ready.clear()

            def _warm_live_caches(ed: Any, gdate: str, need_live_sync: bool) -> None:
                game = getattr(ed, "game", None)
                if game is None:
                    self._live_sync_ready.set()
                    return
                try:
                    if hasattr(game, "ensure_people_index"):
                        game.ensure_people_index()
                except Exception:  # noqa: BLE001
                    pass
                if need_live_sync:
                    try:
                        if hasattr(game, "read_live_snapshot") and self.db.editor is ed:
                            recs, cfins = game.read_live_snapshot(
                                self.db.unique_ids(),
                                game_date=gdate,
                                include_clubs=True,
                            )
                            if self.db.editor is ed and recs:
                                self.db.apply_live(recs, club_finances=cfins)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        self._live_sync_ready.set()
                try:
                    self.faces.warm_entity_uids_from_game(game)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self.faces.ensure_logo_index()
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(
                target=_warm_live_caches,
                args=(editor, report.game_date or "", not (refresh_all and records is not None)),
                name="fm27-live-warm",
                daemon=True,
            ).start()
            mode = "read-only" if self.db.read_only else "read & write"
            st = self.status_payload()
            club = report.managed_club or st["managed_club"]
            sync_note = (
                f" · {synced:,} live players & {len(club_finances or {}):,} club budgets synced"
                if synced else (f" · sync warning: {sync_err}" if sync_err else " · syncing live data…")
            )
            return {
                "ok": True,
                "message": (
                    f"Attached to FM27 ({mode}) · {st['game_date']} · "
                    f"{Snapshot.display_club(club)}{sync_note}"
                ),
                "live_synced": synced,
                "club_budgets_synced": len(club_finances or {}),
                "flushed": list(self.db.last_flush),
                "status": st,
            }

    def detach_game(self) -> dict[str, Any]:
        with self._attach_lock:
            self.db.detach_live()
            self._staff_live_cache.clear()
            return {"ok": True, "message": "Detached from FM27.", "status": self.status_payload()}

    def _live_staff_attrs(self, uid: int, *, force_refresh: bool = False) -> dict[str, Any]:
        """Read (and cache) live FM27 staff attributes when attached."""
        if not uid or not self.db.attached:
            return {}
        if not force_refresh and uid in self._staff_live_cache:
            return self._staff_live_cache[uid]
        editor = getattr(self.db, "editor", None)
        if editor is None or not hasattr(editor, "read_staff"):
            return {}
        vals: dict[str, Any] = {}
        try:
            rec = editor.read_staff(int(uid))
            if rec and rec.values:
                vals = dict(rec.values)
        except Exception:  # noqa: BLE001
            vals = {}
        if len(self._staff_live_cache) > 4096:
            self._staff_live_cache.clear()
        self._staff_live_cache[uid] = vals
        return vals

    # -- entities ---------------------------------------------------------
    def get_player(self, uid: int) -> dict[str, Any] | None:
        row = self.db.player(int(uid))
        if not row:
            return None
        if self.db.attached:
            editor = getattr(self.db, "editor", None)
            if editor is not None and hasattr(editor, "read_live_values"):
                try:
                    p_uid = int(row.get("unique_id") or uid)
                    live_vals = editor.read_live_values(p_uid)
                    if live_vals:
                        merged_vals = dict(live_vals)
                        if merged_vals.get("asking_price_raw") and int(merged_vals["asking_price_raw"]) > 0:
                            merged_vals["asking_price"] = int(merged_vals["asking_price_raw"])
                        if "transfer_status" in merged_vals:
                            ts = int(merged_vals["transfer_status"] or 0)
                            merged_vals["transfer_listed"] = 1 if ts == 1 else 0
                            merged_vals["listed_for_loan"] = 1 if ts == 2 else 0
                        self.db.live.setdefault(p_uid, {}).update(merged_vals)
                        row.update(merged_vals)
                except Exception:
                    pass
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
        squad_raw = self.db.players_for_club(raw_name, limit=250)
        squad = [_player_summary(p, club_context=raw_name) for p in squad_raw]
        staff = [_staff_summary(s) for s in self.db.query_staff(club=raw_name, limit=120)]
        next_fix = self.db.next_fixture(raw_name)
        fixtures = [dict(f) for f in self.db.fixtures(raw_name, 20)]
        active_squad = [p for p in squad if p.get("loan_status") != "out"]
        fit_count = sum(1 for p in active_squad if not p["injured"])
        loan_in_count = sum(1 for p in squad if p.get("loan_status") == "in")
        loan_out_count = sum(1 for p in squad if p.get("loan_status") == "out")
        morales = [int(p["morale"]) for p in active_squad if p.get("morale") is not None]
        avg_morale = round(sum(morales) / len(morales), 1) if morales else 0
        total_wage = sum(p["wage_raw"] for p in active_squad)
        return {
            **_club_summary(club),
            "squad": squad,
            "staff": staff,
            "staff_counts": self.db.staff_counts(raw_name),
            "fit_count": fit_count,
            "squad_count": len(active_squad),
            "loan_in_count": loan_in_count,
            "loan_out_count": loan_out_count,
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
        live_vals: dict[str, Any] = {}
        live_attrs_available = False
        editor = getattr(self.db, "editor", None)
        if self.db.attached and editor is not None and hasattr(editor, "read_staff"):
            try:
                rec = editor.read_staff(summary["unique_id"])
                if rec and rec.values:
                    live_vals = dict(rec.values)
                    live_attrs_available = True
            except Exception:
                live_vals = {}

        # Merge snapshot + live values + any local overlay edits on this staff row
        merged = dict(row)
        merged.update(live_vals)
        # Re-apply any staged overlay edits for staff attrs stored in self.db.edits
        row_id = row.get("id")
        if row_id is not None:
            prefix = f"staff:{row_id}:"
            for k, v in (self.db.edits or {}).items():
                if k.startswith(prefix):
                    col = k[len(prefix):]
                    merged[col] = v
                    if col in _STAFF_ALL_ATTR_COLS:
                        live_attrs_available = True

        summary["ca"] = int(merged.get("ca") or summary["ca"] or 0)
        summary["pa"] = int(merged.get("pa") or summary["pa"] or 0)
        summary["age"] = merged.get("age") if merged.get("age") is not None else summary["age"]
        summary["job"] = merged.get("job") or summary["job"]

        attrs: dict[str, list[dict[str, Any]]] = {}
        for group_name, fields in _STAFF_ATTRIBUTE_GROUPS:
            attrs[group_name] = [
                {
                    "label": lbl,
                    "column": col,
                    "value": int(merged[col]) if merged.get(col) is not None else None,
                }
                for lbl, col in fields
            ]

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
            "home_reputation": int(merged.get("home_reputation") or 0),
            "current_reputation": int(merged.get("current_reputation") or 0),
            "world_reputation": int(merged.get("world_reputation") or 0),
            "live_attrs_available": live_attrs_available,
            "can_live_write": bool(self.db.attached and not self.db.read_only and live_attrs_available),
            "attributes": attrs,
            "star_ratings": staffratings.evaluate_staff(merged, job=summary["job"]),
            "club_id": club_obj["id"] if club_obj else None,
            "colleagues": colleagues[:25],
        }

    def edit_staff(self, uid: int, column: str, value: Any) -> dict[str, Any]:
        row = self.db.staff_member(int(uid))
        if not row:
            return {"ok": False, "error": f"Staff #{uid} not found."}
        col = str(column or "").strip()
        staff_uid = int(row.get("unique_id") or row.get("id") or uid)

        # Text fields (job)
        if col == "job":
            val_str = str(value or "").strip()
            if not val_str:
                return {"ok": False, "error": "Job title cannot be empty."}
            self.db.set_value("staff", row, col, val_str)
            return {
                "ok": True,
                "in_game": False,
                "message": f"Updated job → {val_str} (staged).",
                "staff": self.get_staff(staff_uid),
                "status": self.status_payload(),
            }

        allowed_numeric = (
            {"ca", "pa", "age", "home_reputation", "current_reputation", "world_reputation"}
            | _STAFF_ALL_ATTR_COLS
        )
        if col not in allowed_numeric:
            return {"ok": False, "error": f"Staff column {col!r} is not editable."}
        try:
            num = int(round(float(value)))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"Invalid numeric value: {value!r}"}

        if col in ("ca", "pa"):
            clamped = max(1, min(200, num))
        elif col == "age":
            clamped = max(18, min(95, num))
        elif col.endswith("_reputation"):
            clamped = max(0, min(10000, num))
        else:
            clamped = max(1, min(20, num))

        in_game = False
        editor = getattr(self.db, "editor", None)
        if (col != "age" and self.db.attached and not self.db.read_only
                and editor is not None and hasattr(editor, "write_staff")):
            try:
                clamped = editor.write_staff(staff_uid, col, clamped)
                in_game = True
                self._staff_live_cache.pop(staff_uid, None)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        else:
            self.db.set_value("staff", row, col, clamped)

        where = "in FM27" if in_game else "staged"
        return {
            "ok": True,
            "in_game": in_game,
            "value": clamped,
            "message": f"Updated {col.replace('_', ' ').title()} → {clamped} ({where}).",
            "staff": self.get_staff(staff_uid),
            "status": self.status_payload(),
        }

    def staff_preset(self, uid: int, preset: str) -> dict[str, Any]:
        row = self.db.staff_member(int(uid))
        if not row:
            return {"ok": False, "error": f"Staff #{uid} not found."}
        staff_uid = int(row.get("unique_id") or row.get("id") or uid)
        kind = str(preset or "").strip().lower()
        if kind != "max_staff":
            return {"ok": False, "error": f"Unknown staff preset: {preset!r}"}

        editor = getattr(self.db, "editor", None)
        can_live = bool(self.db.attached and not self.db.read_only
                        and editor is not None and hasattr(editor, "write_staff"))
        touched = 0
        targets: list[tuple[str, int]] = [("ca", 200), ("pa", 200)]
        for col in sorted(_STAFF_ALL_ATTR_COLS):
            targets.append((col, 1 if col == "controversy" else 20))
        if can_live:
            for col, val in targets:
                try:
                    editor.write_staff(staff_uid, col, val)
                    touched += 1
                except Exception:
                    continue
            msg = f"Maxed {touched} staff attributes to 20 & CA/PA → 200 (in FM27)."
        else:
            self.db.set_value("staff", row, "ca", 200)
            self.db.set_value("staff", row, "pa", 200)
            msg = "CA/PA → 200 (staged; attach FM27 to max live coaching attributes)."
        return {
            "ok": True,
            "in_game": can_live,
            "message": msg,
            "staff": self.get_staff(staff_uid),
            "status": self.status_payload(),
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
               max_ca: int | None = None, min_pa: int | None = None,
               max_pa: int | None = None,
               min_pos_rating: float | None = None,
               attr_filters: list[tuple[str, int | None, int | None]] | None = None,
               staff_cat: str = "", min_stars: float | None = None,
               listed_only: bool = False, limit: int = 250) -> dict[str, Any]:
        scope_key = (scope or "players").strip().lower()
        cap = max(1, min(400, int(limit)))
        if scope_key == "staff":
            cat_key = (staff_cat or "").strip().lower()
            if cat_key == "any" or cat_key == "best" or cat_key not in staffratings.CATEGORY_BY_KEY:
                cat_key = ""
            # Warm the O(1) people UID->ptr index once before reading live staff attrs.
            if self.db.attached:
                ed = getattr(self.db, "editor", None)
                game = getattr(ed, "game", None) if ed is not None else None
                if game is not None and hasattr(game, "ensure_people_index"):
                    try:
                        game.ensure_people_index()
                    except Exception:  # noqa: BLE001
                        pass
            # Scan deeper when filtering/sorting by coaching star rating
            scan_limit = max(cap, 600 if (cat_key or min_stars is not None) else cap)
            raw_staff = self.db.query_staff(
                query=query,
                job=(None if not job or job == "Any" else job),
                min_age=min_age,
                max_age=max_age,
                min_ca=min_ca,
                max_ca=max_ca,
                min_pa=min_pa,
                max_pa=max_pa,
                limit=scan_limit,
            )
            rows = []
            for s in raw_staff:
                summary = _staff_summary(s)
                uid = summary["unique_id"]
                merged = dict(s)
                live_vals = self._live_staff_attrs(uid)
                if live_vals:
                    merged.update(live_vals)
                    summary["ca"] = int(merged.get("ca") or summary["ca"] or 0)
                    summary["pa"] = int(merged.get("pa") or summary["pa"] or 0)
                ev = staffratings.evaluate_staff(
                    merged, job=summary["job"], focus_category=cat_key
                )
                if min_stars is not None and ev["stars"] < float(min_stars):
                    continue
                summary["stars"] = ev["stars"]
                summary["star_score"] = ev["score"]
                summary["live_stars"] = ev["live_stars"]
                summary["star_category"] = ev["category"]
                summary["star_category_label"] = ev["category_label"]
                summary["star_category_zh"] = ev["category_zh"]
                summary["star_ddm"] = ev["ddm"]
                summary["star_summary"] = ev["key_summary"]
                rows.append(summary)
            if cat_key or min_stars is not None:
                rows.sort(key=lambda r: (-float(r["stars"]), -float(r["star_score"]), -int(r["ca"])))
            rows = rows[:cap]
        elif scope_key == "clubs":
            rows = [_club_summary(c) for c in self.db.clubs(query, cap)]
        elif scope_key == "nations":
            rows = self.db.nations(query, cap)
        else:
            scope_key = "players"
            pos_val = None if not pos or pos == "Any" else pos
            need_full_attrs = bool(pos_val or min_pos_rating is not None or attr_filters)
            scan_limit = max(cap, 2000 if (pos_val or min_pos_rating is not None) else cap)
            raw_players = self.db.query_players(
                search=query or None,
                position=pos_val,
                position_min=(10 if (pos_val and min_pos_rating is not None) else (15 if pos_val else None)),
                min_age=min_age,
                max_age=max_age,
                min_ca=min_ca,
                max_ca=max_ca,
                min_pa=min_pa,
                max_pa=max_pa,
                transfer_listed=True if listed_only else None,
                attr_filters=attr_filters or None,
                order="ca",
                descending=True,
                limit=scan_limit,
                columns=("*" if need_full_attrs else SQUAD_COLUMNS),
            )
            rows = []
            for p in raw_players:
                summary = _player_summary(p)
                if pos_val:
                    pr, pr_role, pr_comp = ratings.position_role_rating(p, pos_val)
                    summary["pos_rating"] = pr
                    summary["pos_role"] = pr_role
                    summary["pos_comp"] = pr_comp
                    summary["pos_label"] = pos_val
                else:
                    summary["pos_rating"] = summary["rating"] if summary["rating"] is not None else round(Snapshot.rating(p), 2)
                    summary["pos_role"] = summary["role"]
                    summary["pos_comp"] = None
                    summary["pos_label"] = ""
                if min_pos_rating is not None and (summary["pos_rating"] or 0.0) < float(min_pos_rating):
                    continue
                if attr_filters:
                    summary["matched_attrs"] = {
                        col: int(p.get(col) or 0) for col, _lo, _hi in attr_filters
                        if col in spec.KNOWN_COLUMN_SET
                    }
                rows.append(summary)
            if pos_val or min_pos_rating is not None:
                rows.sort(key=lambda r: (-float(r["pos_rating"] or 0.0), -int(r["ca"] or 0)))
            rows = rows[:cap]
        return {"scope": scope_key, "count": len(rows), "rows": rows, "pos_filter": pos or "Any", "staff_cat": staff_cat or ""}

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
        week["local_schedules"] = tacticweb.list_local_fmf("schedules")
        return week

    def export_training_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        from . import schedulefiles
        club = str(payload.get("club") or "").strip() or self.status_payload()["managed_club_raw"]
        focus = str(payload.get("focus") or "Balanced").strip() or "Balanced"
        name = str(payload.get("name") or "").strip() or f"{Snapshot.display_club(club)} - {focus}"
        match_day = str(payload.get("match_day") or "").strip()
        raw_days = payload.get("days")
        description = str(payload.get("description") or "").strip()
        week = fmplans.training_week(focus, name, match_day, days=raw_days)
        if week.get("error"):
            return {"ok": False, "error": week["error"]}
        week_ids = [[int(s["id"]) for s in d["sessions"]] for d in week["days"]]
        desc_text = description or (" ".join(week.get("notes") or [])[:240] or f"Generated by FM27 Studio ({focus})")
        try:
            exported = schedulefiles.export_schedule_fmf(
                week["name"], week_ids, description=desc_text
            )
            exported["local_schedules"] = tacticweb.list_local_fmf("schedules")
            exported["message"] = f"Exported {exported['filename']} to FM27/schedules"
            return exported
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def online_tactics(self, *, source: str = "fm-arena", query: str = "",
                       formation: str = "", limit: int = 25) -> dict[str, Any]:
        return tacticweb.search_online_tactics(
            source=source, query=query, formation=formation, limit=limit)

    def download_tactic(self, url: str) -> dict[str, Any]:
        return tacticweb.download_fm_arena_tactic(url)

    def local_tactics_and_schedules(self) -> dict[str, Any]:
        return {
            "tactics": tacticweb.list_local_fmf("tactics"),
            "schedules": tacticweb.list_local_fmf("schedules"),
        }

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
            return self.chat_store.clear_active()

    def list_chat_sessions(self) -> dict[str, Any]:
        with self._ai_lock:
            return self.chat_store.list_payload()

    def new_chat_session(self, title: str = "New Chat") -> dict[str, Any]:
        with self._ai_lock:
            res = self.chat_store.create_session(title)
            self._sync_conversation_from_active_session()
            return res

    def select_chat_session(self, session_id: str) -> dict[str, Any]:
        with self._ai_lock:
            res = self.chat_store.select_session(session_id)
            if res.get("ok"):
                self._sync_conversation_from_active_session()
            return res

    def delete_chat_session(self, session_id: str) -> dict[str, Any]:
        with self._ai_lock:
            res = self.chat_store.delete_session(session_id)
            self._sync_conversation_from_active_session()
            return res

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
        if cmd == "/lab":
            target = rest or "my current save tactic vs the best local or FM-Arena tactic"
            return (
                f"Use the native FM27 Tactics Lab match engine tools (fm27_lab_*) directly "
                f"to run a real paired league A/B test for {club}: {target}. "
                f"Load the save if needed, execute the simulation yourself, and report "
                f"verified_matches, validation_scope, PPG, W-D-L, GD, xGD, and the winner."
            ), None
        return text, None

    def _prepare_chat_attachments(self, attachments: list[dict[str, Any]] | None
                                  ) -> tuple[list[dict[str, str]], str]:
        """Split chat attachments into Vision images + formatted text/FMF context."""
        if not attachments or not isinstance(attachments, list):
            return [], ""
        import base64
        images: list[dict[str, str]] = []
        notes: list[str] = []
        upload_dir = os.path.join(A.data_directory(), "uploads")
        for att in attachments[:6]:
            if not isinstance(att, dict):
                continue
            name = os.path.basename(str(att.get("name") or "attachment")).strip() or "attachment"
            kind = str(att.get("kind") or "").strip().lower()
            mime = str(att.get("mime") or "").strip().lower()
            if kind == "image" or mime.startswith("image/"):
                data_url = str(att.get("data_url") or "").strip()
                if data_url.startswith("data:image/") and len(data_url) < 12_000_000:
                    images.append({"name": name, "mime": mime or "image/png", "data_url": data_url})
                    notes.append(f"[Attached image: {name}]")
                continue
            if name.lower().endswith(".fmf") or kind == "fmf":
                b64 = str(att.get("base64") or "").strip()
                if b64:
                    try:
                        raw = base64.b64decode(b64, validate=True)
                    except ValueError:
                        notes.append(f"[Attached .fmf file {name}: invalid base64]")
                        continue
                    os.makedirs(upload_dir, exist_ok=True)
                    safe_name = tacticweb._safe_fmf_filename(name)
                    saved_path = os.path.join(upload_dir, safe_name)
                    try:
                        with open(saved_path, "wb") as handle:
                            handle.write(raw)
                    except OSError:
                        saved_path = ""
                    decoded_note = ""
                    if self.lab.installed:
                        try:
                            info = self.lab.inspect_fmf_bytes(raw)
                            rep = info.get("report") or {}
                            decoded_note = (
                                f" (Native FM27 decode OK: tactic_name={info.get('tactic_name')!r}, "
                                f"mentality={info.get('mentality')}, phases={info.get('phases_count')}, "
                                f"notice={rep.get('notice', '')})"
                            )
                        except Exception as exc:
                            decoded_note = f" (Native decode note: {exc})"
                    notes.append(
                        f"[Attached FM27 .fmf file: {safe_name}, {len(raw):,} bytes"
                        + (f", saved at {saved_path}" if saved_path else "")
                        + decoded_note
                        + (f". You can test this tactic with fm27_lab_run_ab_test using tactic_b_kind='local_fmf' and tactic_b_ref='{saved_path}'." if saved_path else "")
                        + "]"
                    )
                continue
            text_body = str(att.get("text") or "")
            if text_body:
                snippet = text_body[:20000]
                notes.append(f"[Attached file: {name}]\n```\n{snippet}\n```")
        return images, ("\n\n".join(notes) + "\n\n" if notes else "")

    def stream_chat(self, prompt: str, context: dict[str, Any],
                    emit: Any,
                    attachments: list[dict[str, Any]] | None = None) -> None:
        """Execute one chat turn, emitting SSE dicts to ``emit(dict)``."""
        raw_prompt = (prompt or "").strip()
        user_images, attach_notes = self._prepare_chat_attachments(attachments)
        if not raw_prompt and not user_images and not attach_notes:
            emit({"type": "error", "message": "Please enter a question, attach a file, or use a /command."})
            return

        expanded, instant = self._expand_slash(raw_prompt, context or {})
        if instant is not None:
            for st in instant.get("steps", []):
                emit({"type": "step", **st})
            for tl in instant.get("tools", []):
                emit({"type": "tool_done", "id": "local_1", **tl})
            self.chat_store.append_turns(
                {"role": "user", "text": raw_prompt, "attachments": attachments or []},
                {
                    "role": "assistant",
                    "text": instant["answer"],
                    "steps": list(instant.get("steps") or []),
                    "thinking": "",
                    "tools": [dict(id="local_1", **tl) for tl in (instant.get("tools") or [])],
                    "elapsedMs": 10,
                    "error": False,
                },
            )
            emit({
                "type": "done",
                "text": instant["answer"],
                "refresh_entity": instant.get("refresh_entity"),
                "status": self.status_payload(),
                "sessions": self.chat_store.list_payload()["sessions"],
                "active_session_id": self.chat_store.active_id,
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
        full_prompt = ctx_note + attach_notes + (expanded or raw_prompt or "Please analyze the attached file(s).")

        with self._ai_lock:
            settings = self.settings
            key = self.keys.load()
            history = [{"role": m.role, "text": m.text}
                       for m in self.conversation.messages[-12:] if not m.failed]
            self.conversation.add("user", raw_prompt or "[Attachment]")

        # Always anchor the AI's save context on the user's managed club (e.g. Milan),
        # never hijacked to another club when viewing a loaned-in or scouted player.
        # If FM27 is attached and background live sync is still finishing, wait briefly
        # so the AI always reads live injuries, contracts, loans, and budgets.
        if self.db.attached and self.db.live_count() == 0:
            self._live_sync_ready.wait(timeout=4.0)
        club_name = self.status_payload()["managed_club_raw"]
        active_effort = (settings.reasoning or "Default").strip() or "Default"
        t0 = time.perf_counter()
        rec_steps: list[dict[str, Any]] = []
        rec_thinking: list[str] = []
        rec_tools: list[dict[str, Any]] = []
        rec_reasoning_tokens = 0

        def _capture_emit(ev: dict[str, Any]) -> None:
            nonlocal rec_reasoning_tokens
            etype = ev.get("type")
            if etype == "step":
                rec_steps.append({"stage": ev.get("stage"), "label": ev.get("label"), "ms": ev.get("ms", 0)})
            elif etype == "thinking":
                if ev.get("text"):
                    rec_thinking.append(str(ev["text"]))
                rt = ev.get("reasoning_tokens")
                if isinstance(rt, int) and rt > 0:
                    rec_reasoning_tokens += rt
            elif etype == "tool_done":
                rec_tools.append({
                    "id": ev.get("id"), "name": ev.get("name"), "args": ev.get("args") or {},
                    "ms": ev.get("ms", 0), "ok": ev.get("ok", True), "summary": ev.get("summary") or "",
                })
            emit(ev)

        try:
            answer = A.chat_with_tools(
                settings, key, self.db, full_prompt,
                history=history,
                club_name=club_name,
                on_event=_capture_emit,
                user_images=user_images or None,
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            with self._ai_lock:
                self.conversation.add("assistant", answer)
                self.chat_store.append_turns(
                    {"role": "user", "text": raw_prompt or "[Attachment]", "attachments": attachments or []},
                    {
                        "role": "assistant",
                        "text": answer,
                        "steps": rec_steps,
                        "thinking": "\n\n".join(rec_thinking),
                        "reasoningEffort": active_effort,
                        "reasoningTokens": rec_reasoning_tokens,
                        "tools": rec_tools,
                        "elapsedMs": elapsed_ms,
                        "error": False,
                    },
                )
            emit({
                "type": "done",
                "text": answer,
                "elapsed_ms": elapsed_ms,
                "reasoning_effort": active_effort,
                "reasoning_tokens": rec_reasoning_tokens,
                "status": self.status_payload(),
                "sessions": self.chat_store.list_payload()["sessions"],
                "active_session_id": self.chat_store.active_id,
            })
        except A.AssistantError as exc:
            err_text = str(exc) + (f" ({exc.hint})" if exc.hint else "")
            with self._ai_lock:
                self.conversation.add("assistant", str(exc), failed=True)
                self.chat_store.append_turns(
                    {"role": "user", "text": raw_prompt or "[Attachment]", "attachments": attachments or []},
                    {"role": "assistant", "text": err_text, "steps": rec_steps,
                     "thinking": "\n\n".join(rec_thinking), "tools": rec_tools, "error": True},
                )
            emit({"type": "error", "message": str(exc), "hint": exc.hint or ""})
        except Exception as exc:  # noqa: BLE001
            with self._ai_lock:
                self.conversation.add("assistant", f"Assistant error: {exc}", failed=True)
                self.chat_store.append_turns(
                    {"role": "user", "text": raw_prompt or "[Attachment]", "attachments": attachments or []},
                    {"role": "assistant", "text": f"Assistant failed: {exc}", "steps": rec_steps,
                     "thinking": "\n\n".join(rec_thinking), "tools": rec_tools, "error": True},
                )
            emit({"type": "error", "message": f"Assistant failed: {exc}"})


# ---------------------------------------------------------------------------
# Threaded local HTTP + SSE Server
# ---------------------------------------------------------------------------

def _make_handler(backend: StudioBackend, token: str,
                  on_activity: Any = None):
    class StudioHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _touch_activity(self) -> None:
            if callable(on_activity):
                on_activity()

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
            if length <= 0 or length > 16_000_000:
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
            self._touch_activity()
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
            if path == "/api/ping":
                self._send_json({"ok": True})
            elif path == "/api/bootstrap":
                self._send_json(backend.bootstrap())
            elif path == "/api/status":
                self._send_json(backend.status_payload())
            elif path == "/api/search":
                def _opt_float(val: str) -> float | None:
                    s = str(val or "").strip()
                    if not s:
                        return None
                    try:
                        return float(s)
                    except ValueError:
                        return None

                parsed_attrs: list[tuple[str, int | None, int | None]] = []
                raw_attrs = q1("attrs", "").strip()
                if raw_attrs:
                    for part in raw_attrs.split(","):
                        bits = part.strip().split(":")
                        if bits and bits[0].strip():
                            col = bits[0].strip().lower()
                            lo = _opt_int(bits[1]) if len(bits) > 1 else None
                            hi = _opt_int(bits[2]) if len(bits) > 2 else None
                            if lo is not None or hi is not None:
                                parsed_attrs.append((col, lo, hi))
                self._send_json(backend.search(
                    scope=q1("scope", "players"),
                    query=q1("q", ""),
                    pos=q1("pos", ""),
                    job=q1("job", ""),
                    min_age=_opt_int(q1("min_age")),
                    max_age=_opt_int(q1("max_age")),
                    min_ca=_opt_int(q1("min_ca")),
                    max_ca=_opt_int(q1("max_ca")),
                    min_pa=_opt_int(q1("min_pa")),
                    max_pa=_opt_int(q1("max_pa")),
                    min_pos_rating=_opt_float(q1("min_pos_rating")),
                    attr_filters=parsed_attrs or None,
                    staff_cat=q1("staff_cat", ""),
                    min_stars=_opt_float(q1("min_stars")),
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
            elif path == "/api/tactics/online":
                self._send_json(backend.online_tactics(
                    source=q1("source", "fm-arena"),
                    query=q1("q", ""),
                    formation=q1("formation", ""),
                    limit=_opt_int(q1("limit")) or 25,
                ))
            elif path == "/api/tactics/local":
                self._send_json(backend.local_tactics_and_schedules())
            elif path == "/api/lab/status":
                self._send_json(backend.lab.status())
            elif path == "/api/lab/jobs":
                try:
                    self._send_json({"ok": True, "jobs": backend.lab.list_jobs()})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc), "jobs": []})
            elif path == "/api/ai/settings":
                self._send_json(backend.ai_settings_payload())
            elif path == "/api/ai/sessions":
                self._send_json(backend.list_chat_sessions())
            elif path.startswith("/api/ai/sessions/"):
                sid = urllib.parse.unquote(path[len("/api/ai/sessions/"):])
                self._send_json(backend.select_chat_session(sid))
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
                kind = parts[3].strip().lower() if len(parts) >= 4 else ""
                raw_key = urllib.parse.unquote("/".join(parts[4:])).strip() if len(parts) >= 5 else ""
                file_path: str | None = None
                if kind in ("player", "staff"):
                    uid = _opt_int(raw_key)
                    file_path = backend.faces.resolve(uid) if uid else None
                elif kind == "club" and raw_key:
                    if raw_key.isdigit():
                        club_row = backend.db.club(int(raw_key))
                        if club_row:
                            file_path = backend.faces.resolve_club(
                                str(club_row.get("name") or ""),
                                uid=_opt_int(club_row.get("unique_id")),
                            )
                        else:
                            file_path = backend.faces.resolve_club("", uid=int(raw_key))
                    else:
                        club_row = backend.db.club_by_name(raw_key)
                        c_name = str(club_row.get("name") or raw_key) if club_row else raw_key
                        c_uid = _opt_int(club_row.get("unique_id")) if club_row else None
                        file_path = backend.faces.resolve_club(c_name, uid=c_uid)
                        if not file_path and c_name != raw_key:
                            file_path = backend.faces.resolve_club(raw_key)
                elif kind == "nation" and raw_key:
                    n_uid = _opt_int(raw_key) if raw_key.isdigit() else None
                    file_path = backend.faces.resolve_nation(
                        "" if n_uid is not None else raw_key,
                        uid=n_uid,
                    )
                if not file_path:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
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
            self._touch_activity()
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
            elif path == "/api/ai/sessions":
                act = str(body.get("action") or "new").strip().lower()
                if act == "select":
                    self._send_json(backend.select_chat_session(str(body.get("id") or "")))
                elif act == "delete":
                    self._send_json(backend.delete_chat_session(str(body.get("id") or "")))
                else:
                    self._send_json(backend.new_chat_session(str(body.get("title") or "New Chat")))
            elif path == "/api/tactics/download":
                self._send_json(backend.download_tactic(str(body.get("url") or "")))
            elif path == "/api/plans/training/export":
                self._send_json(backend.export_training_plan(body))
            elif path == "/api/lab/install":
                res = tacticslab.download_lab_from_github(str(body.get("dest_dir") or "") or None)
                backend.lab.refresh_install()
                res["status"] = backend.lab.status()
                self._send_json(res)
            elif path == "/api/lab/refresh":
                custom = body.get("dir")
                if isinstance(custom, str) and custom.strip():
                    backend._prefs["lab_dir"] = custom.strip()
                    _save_studio_prefs(backend._prefs)
                    backend.lab.refresh_install(custom.strip())
                else:
                    backend.lab.refresh_install()
                self._send_json(backend.lab.status())
            elif path == "/api/lab/load-save":
                try:
                    res = backend.lab.import_and_load_save(
                        str(body.get("save_path") or ""),
                        club_name_hint=str(body.get("club") or ""),
                        club_uid=_opt_int(body.get("club_uid")),
                        tactic_index=int(body.get("tactic_index") or 0),
                    )
                    self._send_json(res)
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            elif path == "/api/lab/run":
                try:
                    res = backend.lab.start_ab_test(
                        tactic_b=body.get("tactic_b") or {"kind": "save", "tactic_index": 0},
                        tactic_a=body.get("tactic_a"),
                        mode=str(body.get("mode") or "league"),
                        opponent_count=int(body.get("opponent_count") or 4),
                        matches_per_opponent=int(body.get("matches_per_opponent") or 2),
                        event_details=bool(body.get("event_details", False)),
                        name=str(body.get("name") or ""),
                    )
                    self._send_json(res)
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            elif path == "/api/lab/cancel":
                try:
                    self._send_json({"ok": True, "result": backend.lab.cancel_job()})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
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
            elif path.startswith("/api/staff/"):
                parts = path.split("/")
                uid = _opt_int(parts[3]) if len(parts) >= 4 else None
                action = parts[4] if len(parts) >= 5 else ""
                if uid is None:
                    self._send_json({"error": "Invalid staff id"}, HTTPStatus.BAD_REQUEST)
                    return
                if action == "edit":
                    self._send_json(backend.edit_staff(uid, body.get("column", ""), body.get("value")))
                elif action == "preset":
                    self._send_json(backend.staff_preset(uid, body.get("preset", "")))
                else:
                    self._send_json({"error": "Unknown staff action"}, HTTPStatus.NOT_FOUND)
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
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
            except OSError:
                return

            events: queue.Queue[dict[str, Any] | None] = queue.Queue()

            def runner() -> None:
                try:
                    backend.stream_chat(
                        str(body.get("prompt") or ""),
                        body.get("context") if isinstance(body.get("context"), dict) else {},
                        events.put,
                        attachments=body.get("attachments") if isinstance(body.get("attachments"), list) else None,
                    )
                finally:
                    events.put(None)

            threading.Thread(target=runner, daemon=True).start()
            while True:
                try:
                    item = events.get(timeout=15.0)
                except queue.Empty:
                    self._touch_activity()
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except OSError:
                        break
                    continue
                if item is None:
                    break
                self._touch_activity()
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
        self.had_client = False
        self.last_activity_at = 0.0

        def _mark_active() -> None:
            self.had_client = True
            self.last_activity_at = time.monotonic()

        self.httpd = ThreadingHTTPServer(
            (host, port),
            _make_handler(self.backend, self.token, on_activity=_mark_active),
        )
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address
        self._thread: threading.Thread | None = None
        self._prune_stop = threading.Event()
        self._prune_interval = max(60.0, float(prune_interval_seconds))
        self._prune_thread: threading.Thread | None = None

    def is_client_active(self, within_seconds: float = 35.0) -> bool:
        return self.had_client and (time.monotonic() - self.last_activity_at) <= within_seconds

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
