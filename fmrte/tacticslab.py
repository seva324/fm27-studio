"""Integration bridge to FM27 Tactics Lab (native FM27 match engine).

Follows ``HANDOVER-FM27-ENGINE-INTEGRATION.md`` (Path A: ``desktop_server.py --stdio``):
- Never hand-rolls scenario binary encoding or bypasses engine compatibility gates.
- Uses a dedicated ``FM27_DESKTOP_DATA`` directory per Studio instance to respect
  the single-instance exclusive lease rule.
- Always surfaces ``verified`` and ``validation_scope`` honestly and never treats
  ``trial_allowed`` as verified.
- If FM27 Tactics Lab is not installed locally, reports ``installed: False`` with
  ``repo_url`` (https://github.com/seva324/fm-tactics-lab) so the UI or AI can ask
  the user before downloading.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from . import assistant as A
from . import tacticweb

LAB_REPO_URL = "https://github.com/seva324/fm-tactics-lab"
LAB_ARCHIVE_URL = "https://github.com/seva324/fm-tactics-lab/archive/refs/heads/main.zip"
LAB_RELEASES_API = "https://api.github.com/repos/seva324/fm-tactics-lab/releases/latest"

_USER_AGENT = "FM27-Studio/1.0"


def detect_fm27_games_dir() -> str:
    """Locate the user's FM27 ``games`` directory containing ``.fm`` saves."""
    env_dir = os.environ.get("FMRTE_GAMES_DIR", "").strip()
    if env_dir:
        return env_dir
    home = os.path.expanduser("~")
    candidates: list[str] = []
    for base in (
        os.path.join(home, "OneDrive", "Documents", "Sports Interactive"),
        os.path.join(home, "Documents", "Sports Interactive"),
    ):
        for game in ("Football Manager 27", "Football Manager 2027", "Football Manager 26"):
            candidates.append(os.path.join(base, game, "games"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0] if candidates else ""


def list_local_saves() -> dict[str, Any]:
    """List ``.fm`` save files in FM27's ``games/`` folder, newest first."""
    folder = detect_fm27_games_dir()
    files: list[dict[str, Any]] = []
    if folder and os.path.isdir(folder):
        try:
            for entry in os.scandir(folder):
                if entry.is_file() and entry.name.lower().endswith(".fm"):
                    st = entry.stat()
                    files.append({
                        "name": os.path.splitext(entry.name)[0],
                        "filename": entry.name,
                        "path": entry.path,
                        "size_mb": round(st.st_size / (1024 * 1024), 2),
                        "mtime": int(st.st_mtime),
                    })
        except OSError:
            pass
    files.sort(key=lambda x: (-x["mtime"], x["name"].lower()))
    return {"dir": folder, "exists": bool(folder and os.path.isdir(folder)),
            "count": len(files), "saves": files}


def detect_game_root(data_hint: str = "") -> str:
    """Locate the installed Football Manager 27 folder (Steam / Xbox / Epic)."""
    candidates: list[str] = []
    for env_key in ("FM27_GAME_ROOT", "FMRTE_GAME_DIR"):
        v = os.environ.get(env_key, "").strip()
        if v:
            candidates.append(v)
    if data_hint:
        gr = os.path.join(data_hint, "game-root.txt")
        if os.path.isfile(gr):
            try:
                txt = Path(gr).read_text(encoding="utf-8-sig").strip()
                if txt:
                    candidates.append(txt)
            except OSError:
                pass
    candidates.extend([
        r"C:\Program Files (x86)\Steam\steamapps\common\Football Manager 27",
        r"C:\Program Files (x86)\Steam\steamapps\common\Football Manager 2027",
        r"C:\Program Files\Steam\steamapps\common\Football Manager 27",
        r"D:\SteamLibrary\steamapps\common\Football Manager 27",
        r"E:\SteamLibrary\steamapps\common\Football Manager 27",
        r"C:\XboxGames\Football Manager 27\Content",
        r"C:\XboxGames\Football Manager 27",
        r"C:\Program Files\Epic Games\FootballManager27",
    ])
    for cand in candidates:
        if not cand or not os.path.isdir(cand):
            continue
        if os.path.isdir(os.path.join(cand, "Content", "fm_Data")):
            return os.path.join(cand, "Content")
        if os.path.isdir(os.path.join(cand, "fm_Data")) or os.path.isfile(os.path.join(cand, "fm.exe")):
            return cand
    return ""


def _find_release_in_data_dir(data_dir: str) -> str | None:
    releases = os.path.join(data_dir, "releases")
    if not os.path.isdir(releases):
        return None
    dirs: list[tuple[float, str]] = []
    try:
        for entry in os.scandir(releases):
            if not entry.is_dir():
                continue
            srv = os.path.join(entry.path, "source", "desktop_server.py")
            eng = os.path.join(entry.path, "engine", "fm27_native")
            if os.path.isfile(srv) and os.path.isdir(eng):
                try:
                    mt = entry.stat().st_mtime
                except OSError:
                    mt = 0.0
                dirs.append((mt, entry.path))
    except OSError:
        return None
    if not dirs:
        return None
    dirs.sort(key=lambda x: x[0], reverse=True)
    return dirs[0][1]


def detect_lab_installation(custom_dir: str = "") -> dict[str, Any]:
    """Discover a local FM27 Tactics Lab release (with ``desktop_server.py`` + ``fm27_native``).

    Checks ``FM27TacticsDesktop.settings.json`` first (Handover §6.5), then
    explicit custom dir / env var, then Downloads and Studio's managed folder.
    """
    home = os.path.expanduser("~")
    local_app = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    settings_file = os.path.join(local_app, "FM27TacticsDesktop.settings.json")

    search_roots: list[str] = []
    if custom_dir and custom_dir.strip():
        search_roots.append(custom_dir.strip())
    env_dir = os.environ.get("FM27_TACTICS_LAB_DIR", "").strip()
    if env_dir:
        search_roots.append(env_dir)

    # Handover §6.5: active desktop settings.json data_directory first
    if os.path.isfile(settings_file):
        try:
            cfg = json.loads(Path(settings_file).read_text(encoding="utf-8-sig"))
            dd = str(cfg.get("data_directory") or "").strip()
            if dd:
                search_roots.append(dd)
        except (OSError, ValueError):
            pass

    search_roots.extend([
        os.path.join(home, "Downloads", "FM27-Tactics-Lab", "data"),
        os.path.join(home, "Downloads", "FM27-Tactics-Lab"),
        os.path.join(A.data_directory(), "fm-tactics-lab"),
        os.path.join(A.data_directory(), "fm-tactics-lab", "data"),
    ])

    checked: list[str] = []
    for root in search_roots:
        if not root or root in checked or not os.path.isdir(root):
            continue
        checked.append(root)
        # Case 1: root itself IS a release folder (contains source/desktop_server.py + engine/fm27_native)
        if (os.path.isfile(os.path.join(root, "source", "desktop_server.py"))
                and os.path.isdir(os.path.join(root, "engine", "fm27_native"))):
            return _build_install_info(root, os.path.dirname(root))
        # Case 2: root is a data directory with releases/<hash>/
        rel = _find_release_in_data_dir(root)
        if rel:
            return _build_install_info(rel, root)
        # Case 3: root/data/releases/<hash>/
        rel2 = _find_release_in_data_dir(os.path.join(root, "data"))
        if rel2:
            return _build_install_info(rel2, os.path.join(root, "data"))
        # Case 4: source repo checkout (src/fm27_tactics_server.py + fm27_native)
        if (os.path.isfile(os.path.join(root, "src", "desktop_server.py"))
                and os.path.isdir(os.path.join(root, "fm27_native"))):
            return {
                "installed": True,
                "ready": True,
                "release_dir": root,
                "source_dir": os.path.join(root, "src"),
                "server_script": os.path.join(root, "src", "desktop_server.py"),
                "vendor_dir": os.path.join(root, "vendor"),
                "engine_root": root,
                "python_exe": sys.executable,
                "game_root": detect_game_root(root),
                "repo_url": LAB_REPO_URL,
            }

    # Also check if a source-only download exists (needs native build / EXE run)
    managed = os.path.join(A.data_directory(), "fm-tactics-lab")
    has_exe = None
    for folder in (os.path.join(home, "Downloads", "FM27-Tactics-Lab"), managed):
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.lower().startswith("fm27-tactics-lab") and name.lower().endswith(".exe"):
                    has_exe = os.path.join(folder, name)
                    break
    return {
        "installed": False,
        "ready": False,
        "release_dir": "",
        "installer_exe": has_exe or "",
        "game_root": detect_game_root(),
        "repo_url": LAB_REPO_URL,
        "message": (
            "FM27 Tactics Lab engine is not installed locally. "
            f"Download it from {LAB_REPO_URL} to enable native match-engine A/B testing."
        ),
    }


def _build_install_info(release_dir: str, data_hint: str) -> dict[str, Any]:
    bundled_py = os.path.join(release_dir, "python", "python.exe")
    # Prefer the system Python 3.12 (avoids OneDrive/Downloads Low-IL DLL issues)
    py_exe = sys.executable if sys.version_info[:2] == (3, 12) else (
        bundled_py if os.path.isfile(bundled_py) else sys.executable
    )
    return {
        "installed": True,
        "ready": True,
        "release_dir": release_dir,
        "source_dir": os.path.join(release_dir, "source"),
        "server_script": os.path.join(release_dir, "source", "desktop_server.py"),
        "vendor_dir": os.path.join(release_dir, "source", "vendor"),
        "engine_root": os.path.join(release_dir, "engine"),
        "python_exe": py_exe,
        "game_root": detect_game_root(data_hint),
        "repo_url": LAB_REPO_URL,
    }


def download_lab_from_github(dest_dir: str | None = None) -> dict[str, Any]:
    """Download ``seva324/fm-tactics-lab`` from GitHub (prefers Release asset, falls back to repo ZIP)."""
    target = (dest_dir or "").strip() or os.path.join(A.data_directory(), "fm-tactics-lab")
    os.makedirs(target, exist_ok=True)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}

    # 1) Check GitHub Releases for a packaged Windows EXE / ZIP first
    asset_url = ""
    asset_name = ""
    try:
        req = urllib.request.Request(LAB_RELEASES_API, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            rel = json.loads(resp.read(1024 * 1024).decode("utf-8", "replace"))
        for asset in rel.get("assets") or []:
            name = str(asset.get("name") or "")
            dl = str(asset.get("browser_download_url") or "")
            if dl and name.lower().endswith((".exe", ".zip")):
                asset_url, asset_name = dl, name
                break
    except Exception:
        asset_url = ""

    if asset_url and asset_name:
        out_path = os.path.join(target, os.path.basename(asset_name))
        try:
            req = urllib.request.Request(asset_url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, open(out_path, "wb") as handle:
                shutil.copyfileobj(resp, handle)
            if out_path.lower().endswith(".zip"):
                with zipfile.ZipFile(out_path, "r") as zf:
                    zf.extractall(target)
                os.remove(out_path)
            return {
                "ok": True,
                "kind": "release",
                "path": target,
                "file": out_path,
                "repo_url": LAB_REPO_URL,
                "message": (
                    f"Downloaded {asset_name} to {target}. "
                    "If it is an .exe launcher, run it once to unpack the native engine release, "
                    "then click Re-check Engine."
                ),
            }
        except Exception as exc:
            return {"ok": False, "error": f"Failed to download release asset: {exc}", "repo_url": LAB_REPO_URL}

    # 2) Fallback: download repository source ZIP from main branch
    zip_path = os.path.join(target, "fm-tactics-lab-main.zip")
    try:
        req = urllib.request.Request(LAB_ARCHIVE_URL, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as handle:
            shutil.copyfileobj(resp, handle)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(target)
        os.remove(zip_path)
        return {
            "ok": True,
            "kind": "source",
            "path": target,
            "repo_url": LAB_REPO_URL,
            "message": (
                f"Downloaded fm-tactics-lab source to {target}. "
                "Follow its README to build or unpack fm27_native (or download a prebuilt release EXE), "
                "then click Re-check Engine."
            ),
        }
    except Exception as exc:
        return {"ok": False, "error": f"Download failed: {exc}", "repo_url": LAB_REPO_URL}


class TacticsLabError(RuntimeError):
    """Error raised when communicating with the FM27 Tactics Lab stdio backend."""


class TacticsLabClient:
    """Thread-safe persistent stdio client for ``desktop_server.py --stdio``."""

    def __init__(self, custom_dir: str = ""):
        self._lock = threading.RLock()
        self._custom_dir = custom_dir
        self._install: dict[str, Any] = detect_lab_installation(custom_dir)
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._studio_data_dir = os.path.join(A.data_directory(), "tactics-lab-data")
        # Session state for loaded save / tactics board
        self.loaded_save_id: str = ""
        self.loaded_save_path: str = ""
        self.loaded_club_uid: int = 0
        self.loaded_club_name: str = ""
        self.base_scenario: dict[str, Any] | None = None
        self.opponents_cache: list[dict[str, Any]] = []

    def refresh_install(self, custom_dir: str | None = None) -> dict[str, Any]:
        with self._lock:
            if custom_dir is not None:
                self._custom_dir = custom_dir
            self.stop()
            self._install = detect_lab_installation(self._custom_dir)
            return dict(self._install)

    @property
    def install_info(self) -> dict[str, Any]:
        return dict(self._install)

    @property
    def installed(self) -> bool:
        return bool(self._install.get("installed"))

    def _ensure_started(self) -> None:
        if not self._install.get("installed"):
            raise TacticsLabError(self._install.get("message") or "FM27 Tactics Lab is not installed.")
        if self._proc is not None and self._proc.poll() is None:
            return
        os.makedirs(self._studio_data_dir, exist_ok=True)
        env = {
            **os.environ,
            "FM27_ENGINE_ROOT": self._install["engine_root"],
            "FM27_DESKTOP_DATA": self._studio_data_dir,
            "PYTHONPATH": self._install["vendor_dir"],
            "PYTHONUTF8": "1",
        }
        if self._install.get("game_root"):
            env["FM27_GAME_ROOT"] = self._install["game_root"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        # NOTE: Do NOT pass -S here because desktop_server.py relies on PYTHONPATH vendor imports.
        self._proc = subprocess.Popen(
            [self._install["python_exe"], self._install["server_script"], "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            creationflags=creationflags,
        )
        assert self._proc.stdout is not None
        ready_line = self._proc.stdout.readline()
        if not ready_line:
            err = (self._proc.stderr.read() if self._proc.stderr else "")[:400]
            self.stop()
            raise TacticsLabError(f"Tactics Lab backend exited before ready: {err}")
        try:
            ready = json.loads(ready_line)
        except ValueError as exc:
            self.stop()
            raise TacticsLabError(f"Invalid Tactics Lab ready banner: {ready_line[:160]}") from exc
        if not ready.get("ready"):
            self.stop()
            raise TacticsLabError(f"Unexpected Tactics Lab startup message: {ready}")

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is not None:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    try:
                        if pipe:
                            pipe.close()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except OSError:
                        pass

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    def call(self, method: str, target: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one stdio frame (``id``, ``method``, ``target``, JSON-string ``body``)."""
        with self._lock:
            self._ensure_started()
            assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
            self._req_id += 1
            rid = self._req_id
            frame: dict[str, Any] = {"id": rid, "method": method.upper(), "target": target}
            if body is not None:
                frame["body"] = json.dumps(body, ensure_ascii=False)
            try:
                self._proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                raw = self._proc.stdout.readline()
            except OSError as exc:
                self.stop()
                raise TacticsLabError(f"Tactics Lab pipe error: {exc}") from exc
            if not raw:
                err = (self._proc.stderr.read() if self._proc.stderr else "")[:300]
                self.stop()
                raise TacticsLabError(f"Tactics Lab backend closed unexpectedly: {err}")
            try:
                resp = json.loads(raw)
            except ValueError as exc:
                raise TacticsLabError(f"Invalid JSON from Tactics Lab: {raw[:200]}") from exc
            status = int(resp.get("status") or 500)
            data = resp.get("data")
            if status >= 400:
                msg = (data.get("error") if isinstance(data, dict) else str(data)) or f"HTTP {status}"
                raise TacticsLabError(msg)
            return data if isinstance(data, dict) else {"value": data}

    # -- High-level operations --------------------------------------------
    def status(self) -> dict[str, Any]:
        if not self.installed:
            return {
                "installed": False,
                "can_simulate": False,
                "repo_url": LAB_REPO_URL,
                "installer_exe": self._install.get("installer_exe", ""),
                "message": self._install.get("message", ""),
                "local_saves": list_local_saves(),
                "local_tactics": tacticweb.list_local_fmf("tactics"),
            }
        try:
            st = self.call("GET", "/api/status")
            ec = st.get("engine_compatibility") or {}
            return {
                "installed": True,
                "can_simulate": bool(ec.get("can_simulate")),
                "compatible": bool(ec.get("compatible")),
                "trial_allowed": bool(ec.get("trial_allowed")),
                "adapter": ec.get("adapter") or "",
                "reason": ec.get("reason") or "",
                "engine_compatibility": ec,
                "release_dir": self._install.get("release_dir", ""),
                "game_root": self._install.get("game_root", ""),
                "repo_url": LAB_REPO_URL,
                "loaded_save": {
                    "id": self.loaded_save_id,
                    "path": self.loaded_save_path,
                    "club_uid": self.loaded_club_uid,
                    "club_name": self.loaded_club_name,
                    "opponents_count": len(self.opponents_cache),
                },
                "local_saves": list_local_saves(),
                "local_tactics": tacticweb.list_local_fmf("tactics"),
            }
        except TacticsLabError as exc:
            return {
                "installed": True,
                "can_simulate": False,
                "error": str(exc),
                "release_dir": self._install.get("release_dir", ""),
                "repo_url": LAB_REPO_URL,
                "local_saves": list_local_saves(),
                "local_tactics": tacticweb.list_local_fmf("tactics"),
            }

    def get_defaults(self) -> dict[str, Any]:
        res = self.call("GET", "/api/defaults")
        return res.get("scenario", res)

    def inspect_fmf_bytes(self, payload: bytes, *, base_scenario: dict[str, Any] | None = None,
                          side: int = 0) -> dict[str, Any]:
        """Decode a real FM27 ``.fmf`` tactic file through the native game parser."""
        scenario = base_scenario if base_scenario is not None else self.get_defaults()
        b64 = base64.b64encode(payload).decode("ascii")
        res = self.call("POST", "/api/import-fmf", {
            "side": int(side),
            "scenario": scenario,
            "file_base64": b64,
        })
        sc = res.get("scenario") or {}
        team = (sc.get("teams") or [{}])[side]
        report = res.get("report") or {}
        phases = team.get("phases") or []
        return {
            "ok": True,
            "tactic_name": report.get("tactic_name") or team.get("tactic_name") or "Imported Tactic",
            "mentality": team.get("mentality"),
            "phases_count": len(phases),
            "report": report,
            "scenario": sc,
        }

    def inspect_fmf_file(self, path: str, *, base_scenario: dict[str, Any] | None = None) -> dict[str, Any]:
        if not os.path.isfile(path):
            raise TacticsLabError(f"Tactic file not found: {path}")
        return self.inspect_fmf_bytes(Path(path).read_bytes(), base_scenario=base_scenario)

    def import_and_load_save(self, save_path: str, club_name_hint: str = "",
                             club_uid: int | None = None, tactic_index: int = 0,
                             timeout_seconds: float = 180.0) -> dict[str, Any]:
        """Import a ``.fm`` save, load the club onto the tactics board, and gather league opponents."""
        if not os.path.isfile(save_path):
            raise TacticsLabError(f"Save file not found: {save_path}")

        # 1) Start async save import and poll /api/save/status
        started = self.call("POST", "/api/save/import", {"path": os.path.abspath(save_path)})
        save_id = str(started.get("id") or "")
        deadline = time.monotonic() + timeout_seconds
        save_status: dict[str, Any] = started
        while time.monotonic() < deadline:
            st = self.call("GET", "/api/save/status")
            save_status = st
            active = st.get("active")
            saves = st.get("saves") or []
            if not save_id and saves:
                matched = next((s for s in saves if os.path.abspath(str(s.get("path") or "")) == os.path.abspath(save_path)), None)
                if matched:
                    save_id = str(matched.get("id") or "")
            if not active:
                if st.get("error"):
                    raise TacticsLabError(str(st["error"]))
                break
            time.sleep(0.5)

        saves = save_status.get("saves") or []
        if not save_id and saves:
            save_id = str(saves[0].get("id") or "")
        if not save_id:
            raise TacticsLabError("Save import completed but no save ID was returned.")

        # 2) Pick managed club (or match club_name_hint / club_uid)
        clubs_resp = self.call("POST", "/api/save/board-clubs", {"id": save_id, "managed": True})
        clubs = clubs_resp.get("clubs") or []
        if not clubs:
            clubs_resp = self.call("POST", "/api/save/board-clubs", {"id": save_id, "managed": False})
            clubs = clubs_resp.get("clubs") or []
        chosen = None
        if club_uid:
            chosen = next((c for c in clubs if int(c.get("club_uid") or c.get("uid") or 0) == int(club_uid)), None)
        if chosen is None and club_name_hint:
            hint = club_name_hint.strip().lower()
            chosen = next((c for c in clubs if hint in str(c.get("name") or "").lower()), None)
        if chosen is None and clubs:
            chosen = clubs[0]
        if chosen is None:
            raise TacticsLabError("No clubs found inside the imported save.")

        cid = int(chosen.get("club_uid") or chosen.get("uid") or 0)
        cname = str(chosen.get("name") or f"Club #{cid}")

        # 3) Load club tactics + load tactic_index onto tactics board
        club_info = self.call("POST", "/api/save/club", {"id": save_id, "club_uid": cid})
        tactics_list = club_info.get("tactics") or []
        t_idx = min(max(0, int(tactic_index)), max(0, len(tactics_list) - 1))
        defaults = self.get_defaults()
        loaded = self.call("POST", "/api/save/load", {
            "id": save_id,
            "club_uid": cid,
            "tactic_index": t_idx,
            "scenario": defaults,
        })
        scenario = loaded.get("scenario")
        if not scenario:
            raise TacticsLabError("Failed to load club onto tactics board.")

        # 4) Inspect league opponents (async if needed)
        opp_res = self.call("POST", "/api/save/opponents", {"id": save_id, "club_uid": cid})
        opp_deadline = time.monotonic() + 90.0
        while time.monotonic() < opp_deadline:
            st = self.call("GET", "/api/save/status")
            if not st.get("active"):
                break
            time.sleep(0.5)
        opp_res = self.call("POST", "/api/save/opponents", {"id": save_id, "club_uid": cid})
        opponents = [o for o in (opp_res.get("opponents") or []) if o.get("available", True)]

        self.loaded_save_id = save_id
        self.loaded_save_path = os.path.abspath(save_path)
        self.loaded_club_uid = cid
        self.loaded_club_name = cname
        self.base_scenario = scenario
        self.opponents_cache = opponents

        return {
            "ok": True,
            "save_id": save_id,
            "save_path": self.loaded_save_path,
            "club_uid": cid,
            "club_name": cname,
            "tactics": [
                {"index": idx, "name": t.get("name") or f"Tactic {idx + 1}",
                 "formation": t.get("formation") or ""}
                for idx, t in enumerate(tactics_list)
            ],
            "opponents_count": len(opponents),
            "opponents": [
                {"key": o.get("key"), "club_uid": o.get("club_uid"),
                 "name": o.get("club_name") or o.get("name") or o.get("key")}
                for o in opponents[:25]
            ],
        }

    def _resolve_tactic_scenario(self, spec_dict: dict[str, Any],
                                 base_scenario: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Build a validated scenario for one tactic source: ``save``, ``local_fmf``, ``fm_arena``, ``base64``."""
        kind = str(spec_dict.get("kind") or "save").strip().lower()
        if kind == "save":
            if self.loaded_save_id and self.loaded_club_uid:
                t_idx = int(spec_dict.get("tactic_index") or 0)
                loaded = self.call("POST", "/api/save/load", {
                    "id": self.loaded_save_id,
                    "club_uid": self.loaded_club_uid,
                    "tactic_index": t_idx,
                    "scenario": base_scenario,
                })
                sc = loaded.get("scenario") or base_scenario
                name = ((sc.get("teams") or [{}])[0]).get("tactic_name") or f"Save Tactic #{t_idx + 1}"
                return sc, str(name)
            return base_scenario, str(((base_scenario.get("teams") or [{}])[0]).get("tactic_name") or "Default Tactic")

        if kind == "fm_arena":
            url = str(spec_dict.get("url") or "").strip()
            dl = tacticweb.download_fm_arena_tactic(url)
            if not dl.get("ok"):
                raise TacticsLabError(dl.get("error") or "Failed to download FM-Arena tactic.")
            imp = self.inspect_fmf_file(dl["path"], base_scenario=base_scenario)
            return imp["scenario"], imp["tactic_name"]

        if kind == "local_fmf":
            path = str(spec_dict.get("path") or "").strip()
            if not os.path.isabs(path):
                path = os.path.join(tacticweb.detect_fm27_tactics_dir(), path)
            imp = self.inspect_fmf_file(path, base_scenario=base_scenario)
            return imp["scenario"], imp["tactic_name"]

        if kind == "base64":
            raw = base64.b64decode(str(spec_dict.get("file_base64") or ""), validate=True)
            imp = self.inspect_fmf_bytes(raw, base_scenario=base_scenario)
            return imp["scenario"], imp["tactic_name"]

        raise TacticsLabError(f"Unknown tactic source kind: {kind!r}")

    def start_ab_test(self, *, tactic_b: dict[str, Any],
                      tactic_a: dict[str, Any] | None = None,
                      mode: str = "league",
                      opponent_count: int = 4,
                      matches_per_opponent: int = 2,
                      event_details: bool = False,
                      name: str = "") -> dict[str, Any]:
        """Start a native FM27 match-engine test (single or A/B comparison)."""
        st = self.call("GET", "/api/status")
        ec = st.get("engine_compatibility") or {}
        if not ec.get("can_simulate"):
            raise TacticsLabError(f"Engine cannot simulate: {ec.get('reason') or 'incompatible'}")

        use_league = (mode == "league" and bool(self.loaded_save_id) and bool(self.base_scenario) and bool(self.opponents_cache))
        base = self.base_scenario if (use_league and self.base_scenario) else self.get_defaults()

        sc_b, name_b = self._resolve_tactic_scenario(tactic_b, base)
        sc_a, name_a = (self._resolve_tactic_scenario(tactic_a, base) if tactic_a else (None, ""))

        count = max(2, int(matches_per_opponent or 2))
        if count % 2:
            count += 1

        if use_league:
            # Pick distinct clubs from league opponents
            picked: list[dict[str, Any]] = []
            seen_clubs: set[int] = set()
            cap = max(1, min(20, int(opponent_count or 4)))
            for opp in self.opponents_cache:
                cuid = int(opp.get("club_uid") or 0)
                if cuid and cuid in seen_clubs:
                    continue
                if cuid:
                    seen_clubs.add(cuid)
                picked.append({"key": opp["key"]})
                if len(picked) >= cap:
                    break
            if not picked:
                raise TacticsLabError("No verified league opponents available for this save.")
            payload: dict[str, Any] = {
                "save_id": self.loaded_save_id,
                "club_uid": self.loaded_club_uid,
                "scenario": sc_b,
                "opponents": picked,
                "count": count,
                "seed_mode": "fixed",
                "first_seed": 1,
                "event_details": bool(event_details),
                "workers": 1,
                "name": name or (f"AB: {name_a} vs {name_b}" if sc_a else f"League: {name_b}"),
            }
            if sc_a is not None:
                payload["reference"] = sc_a
            job = self.call("POST", "/api/save/run", payload)
            return {
                "ok": True,
                "mode": "league",
                "job_id": job.get("id"),
                "total": job.get("total"),
                "tactic_a": name_a,
                "tactic_b": name_b,
                "opponents_count": len(picked),
                "job": job,
            }

        # Fallback / Quick neutral fixture A/B test (no .fm save required)
        payload = {
            "scenario": sc_b,
            "count": max(2, min(40, count)),
            "seed_mode": "fixed",
            "first_seed": 1,
            "event_details": bool(event_details),
            "workers": 1,
            "name": name or (f"AB: {name_a} vs {name_b}" if sc_a else f"Test: {name_b}"),
        }
        if sc_a is not None:
            payload["reference"] = sc_a
        job = self.call("POST", "/api/run", payload)
        return {
            "ok": True,
            "mode": "quick",
            "job_id": job.get("id"),
            "total": job.get("total"),
            "tactic_a": name_a,
            "tactic_b": name_b,
            "job": job,
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.installed:
            return []
        res = self.call("GET", "/api/jobs")
        jobs = res.get("value") if isinstance(res, dict) and "value" in res else res
        if not isinstance(jobs, list):
            return []
        out = []
        for job in reversed(jobs[-15:]):
            out.append(_summarize_job(job))
        return out

    def cancel_job(self) -> dict[str, Any]:
        return self.call("POST", "/api/cancel", {})


def _summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    """Extract honest verified metrics & A/B winner summary from a Tactics Lab job."""
    runs = job.get("runs") or []
    groups: dict[str, dict[str, Any]] = {}
    scopes: set[str] = set()
    verified_count = 0

    for run in runs:
        label = str(run.get("variant") or "B")
        grp = label.split("_", 1)[0] if "_" in label else label
        g = groups.setdefault(grp, {
            "label": grp, "matches": 0, "verified": 0,
            "wins": 0, "draws": 0, "losses": 0, "pts": 0,
            "gf": 0, "ga": 0, "xgf": 0.0, "xga": 0.0,
        })
        if run.get("verified"):
            verified_count += 1
            g["verified"] += 1
        if run.get("validation_scope"):
            scopes.add(str(run["validation_scope"]))
        result_obj = run.get("result") if isinstance(run.get("result"), dict) else {}
        score = result_obj.get("score") if isinstance(result_obj.get("score"), list) else run.get("score")
        if isinstance(score, list) and len(score) == 2:
            # Away venue flips side 0/1 in save_opponents schedule
            is_away = label.endswith("_A")
            gf, ga = (int(score[1]), int(score[0])) if is_away else (int(score[0]), int(score[1]))
            g["matches"] += 1
            g["gf"] += gf
            g["ga"] += ga
            if gf > ga:
                g["wins"] += 1
                g["pts"] += 3
            elif gf == ga:
                g["draws"] += 1
                g["pts"] += 1
            else:
                g["losses"] += 1
        stats_obj = run.get("statistics") if isinstance(run.get("statistics"), dict) else {}
        stats = stats_obj.get("teams") if isinstance(stats_obj.get("teams"), list) else (
            run.get("stats") or run.get("team_stats")
        )
        if isinstance(stats, list) and len(stats) == 2:
            is_away = label.endswith("_A")
            mine, opp = (stats[1], stats[0]) if is_away else (stats[0], stats[1])
            g["xgf"] += float(mine.get("xg") or 0.0)
            g["xga"] += float(opp.get("xg") or 0.0)

    for g in groups.values():
        m = max(1, g["matches"])
        g["ppg"] = round(g["pts"] / m, 2)
        g["gd"] = g["gf"] - g["ga"]
        g["xgd"] = round(g["xgf"] - g["xga"], 2)
        g["xgf"] = round(g["xgf"], 2)
        g["xga"] = round(g["xga"], 2)

    snaps = job.get("tactic_snapshots") or {}
    vnames = job.get("variant_names") or {}
    for k, g in groups.items():
        snap = snaps.get(k)
        if isinstance(snap, dict):
            g["tactic_name"] = snap.get("tactic_name") or snap.get("name") or vnames.get(k) or f"Tactic {k}"
        elif k in vnames:
            g["tactic_name"] = str(vnames[k])

    winner = ""
    if "A" in groups and "B" in groups and groups["A"]["matches"] > 0 and groups["B"]["matches"] > 0:
        a, b = groups["A"], groups["B"]
        if (b["ppg"], b["gd"], b["xgd"]) > (a["ppg"], a["gd"], a["xgd"]):
            winner = f"B ({b.get('tactic_name', 'B')})"
        elif (a["ppg"], a["gd"], a["xgd"]) > (b["ppg"], b["gd"], b["xgd"]):
            winner = f"A ({a.get('tactic_name', 'A')})"
        else:
            winner = "Tie"

    return {
        "id": job.get("id"),
        "name": job.get("name") or job.get("id"),
        "status": job.get("status"),
        "stage": job.get("stage") or "",
        "mode": job.get("mode") or "single",
        "experiment_type": job.get("experiment_type") or "",
        "completed": int(job.get("completed") or 0),
        "total": int(job.get("total") or 0),
        "verified_matches": verified_count,
        "validation_scope": sorted(scopes),
        "groups": groups,
        "winner": winner,
        "error": job.get("error") or "",
    }
