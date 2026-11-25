"""Online FM27 tactic browser & one-click .fmf installer (FM-Arena & Sortitoutsi).

Pure Python 3.12 stdlib only.  Reads public leaderboards from:
- FM-Arena Patch 27.1.1 (https://fm-arena.com/tactic-testing/aggregators/13/)
  — includes public direct ``.fmf`` attachment downloads straight into your
  ``Sports Interactive\\Football Manager 27\\tactics`` folder.
- Sortitoutsi FM27 Tactics (https://sortitoutsi.net/football-manager-2027-tactics)
  — search & browse ranked community tactics (opens tactic page in browser
  when download requires a Sortitoutsi account).
"""

from __future__ import annotations

import html
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

FM_ARENA_URL = "https://fm-arena.com/tactic-testing/aggregators/13/"
SORTITOUTSI_URL = "https://sortitoutsi.net/football-manager-2027-tactics"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CACHE_TTL_SECONDS = 600.0
_MAX_HTML_BYTES = 2 * 1024 * 1024
_MAX_FMF_BYTES = 4 * 1024 * 1024
_FMF_MAGIC = b"\x02\x01afe."

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def detect_fm27_user_dir(subdir: str) -> str:
    """Locate the user's FM27 ``tactics`` or ``schedules`` directory."""
    env_key = f"FMRTE_{subdir.upper()}_DIR"
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        return env_val
    home = os.path.expanduser("~")
    candidates: list[str] = []
    for base in (
        os.path.join(home, "OneDrive", "Documents", "Sports Interactive"),
        os.path.join(home, "Documents", "Sports Interactive"),
    ):
        for game in ("Football Manager 27", "Football Manager 2027", "Football Manager 26"):
            candidates.append(os.path.join(base, game, subdir))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0] if candidates else ""


def detect_fm27_tactics_dir() -> str:
    return detect_fm27_user_dir("tactics")


def detect_fm27_schedules_dir() -> str:
    return detect_fm27_user_dir("schedules")


def list_local_fmf(subdir: str) -> dict[str, Any]:
    """List local ``.fmf`` files inside FM27's ``tactics`` or ``schedules`` folder."""
    folder = detect_fm27_user_dir(subdir)
    files: list[dict[str, Any]] = []
    if folder and os.path.isdir(folder):
        try:
            for entry in os.scandir(folder):
                if entry.is_file() and entry.name.lower().endswith(".fmf"):
                    st = entry.stat()
                    files.append({
                        "name": os.path.splitext(entry.name)[0],
                        "filename": entry.name,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    })
        except OSError:
            pass
    files.sort(key=lambda item: (-item["mtime"], item["name"].lower()))
    return {"dir": folder, "exists": bool(folder and os.path.isdir(folder)),
            "count": len(files), "files": files}


def _http_get(url: str, *, timeout: float = 15.0, max_bytes: int = _MAX_HTML_BYTES
              ) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/octet-stream,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Remote response exceeded size limit.")
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        return data, hdrs


def _clean_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _formation_from_positions_title(title: str) -> str:
    """Turn ``In possession: GK, DCL, DCR, DMC, ...`` into a clean shape string."""
    raw = html.unescape(title or "")
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    tokens = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tokens:
        return ""
    lines = [0, 0, 0, 0, 0]
    for tok in tokens:
        if tok == "GK":
            continue
        if tok.startswith(("D", "WB")) and not tok.startswith("DM"):
            lines[0] += 1
        elif tok.startswith("DM"):
            lines[1] += 1
        elif tok.startswith("M"):
            lines[2] += 1
        elif tok.startswith("AM"):
            lines[3] += 1
        elif tok.startswith("ST"):
            lines[4] += 1
    return "-".join(str(c) for c in lines if c)


def parse_fm_arena_html(page_html: str) -> list[dict[str, Any]]:
    """Parse ranked FM27 tactics from FM-Arena's aggregator HTML table."""
    rows = re.findall(r'<tr[^>]*id="fmt-tactic-(\d+)"[^>]*>(.*?)</tr>', page_html, re.S)
    results: list[dict[str, Any]] = []
    for rank, (tid, body) in enumerate(rows, start=1):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)
        if len(tds) < 11:
            continue
        link_m = re.search(r'href="(/threads/[^"]+)"[^>]*>(.*?)</a>', tds[0], re.S)
        if not link_m:
            continue
        thread_url = "https://fm-arena.com" + html.unescape(link_m.group(1))
        name = _clean_text(link_m.group(2))
        crown = "fmtCrown" in tds[0]
        form_m = re.search(r'title="([^"]*In possession:[^"]*)"', tds[1], re.I)
        positions_desc = html.unescape(form_m.group(1)) if form_m else ""
        formation = _formation_from_positions_title(positions_desc)
        author = _clean_text(tds[2])
        pld = _clean_text(tds[3])
        pts = _clean_text(tds[4])
        gd = _clean_text(tds[5])
        gf = _clean_text(tds[6])
        ga = _clean_text(tds[7])
        xgf = _clean_text(tds[8])
        xga = _clean_text(tds[9])
        xgd = _clean_text(tds[10])
        pts_num = 0
        m_pts = re.match(r"(\d+)", pts)
        if m_pts:
            pts_num = int(m_pts.group(1))
        results.append({
            "source": "fm-arena",
            "id": tid,
            "rank": rank,
            "name": name,
            "formation": formation or "—",
            "positions": positions_desc,
            "author": author,
            "pld": pld,
            "pts": pts,
            "pts_num": pts_num,
            "gd": gd,
            "gf": gf,
            "ga": ga,
            "xgf": xgf,
            "xga": xga,
            "xgd": xgd,
            "crown": crown,
            "url": thread_url,
            "direct_download": True,
        })
    return results


def parse_sortitoutsi_html(page_html: str) -> list[dict[str, Any]]:
    """Parse FM27 community tactics from Sortitoutsi's tactics table."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S)
    results: list[dict[str, Any]] = []
    rank = 0
    for body in rows:
        link_m = re.search(
            r'<a[^>]*href="(https://sortitoutsi\.net/content/\d+/[^"]+)"[^>]*>(.*?)</a>',
            body, re.S,
        )
        if not link_m:
            continue
        rank += 1
        url = html.unescape(link_m.group(1))
        name = _clean_text(link_m.group(2))
        author_m = re.search(r'By\s*<a[^>]*>(.*?)</a>', body, re.S | re.I)
        author = _clean_text(author_m.group(1)) if author_m else "—"
        dl_m = re.search(r"([\d,]+)\s*Downloads", body, re.I)
        downloads = dl_m.group(1) if dl_m else "—"
        tds = [_clean_text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)]
        win_pct = tds[1] if len(tds) > 1 and "%" in tds[1] else "—"
        ppg = tds[2] if len(tds) > 2 and re.match(r"^\d+\.\d+$", tds[2]) else "—"
        gf = tds[3] if len(tds) > 3 else "—"
        ga = tds[4] if len(tds) > 4 else "—"
        gd = tds[5] if len(tds) > 5 else "—"
        form_m = re.search(r"\b(\d-\d-\d(?:-\d)?)\b", name)
        formation = form_m.group(1) if form_m else "—"
        results.append({
            "source": "sortitoutsi",
            "id": url.split("/content/")[-1].split("/")[0],
            "rank": rank,
            "name": name,
            "formation": formation,
            "author": author,
            "downloads": downloads,
            "win_pct": win_pct,
            "pts": f"{ppg} PPG" if ppg != "—" else win_pct,
            "ppg": ppg,
            "gf": gf,
            "ga": ga,
            "gd": gd,
            "url": url,
            "direct_download": False,
        })
    return results


def _cached_fetch(source: str) -> list[dict[str, Any]]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(source)
        if hit and (now - hit[0]) < _CACHE_TTL_SECONDS:
            return list(hit[1])
    if source == "sortitoutsi":
        raw, _ = _http_get(SORTITOUTSI_URL)
        items = parse_sortitoutsi_html(raw.decode("utf-8", "replace"))
    else:
        raw, _ = _http_get(FM_ARENA_URL)
        items = parse_fm_arena_html(raw.decode("utf-8", "replace"))
    with _cache_lock:
        _cache[source] = (now, items)
    return list(items)


def search_online_tactics(source: str = "fm-arena", query: str = "",
                          formation: str = "", limit: int = 25) -> dict[str, Any]:
    """Search FM27 tactics from FM-Arena or Sortitoutsi."""
    src = (source or "fm-arena").strip().lower()
    if src not in ("fm-arena", "sortitoutsi"):
        src = "fm-arena"
    try:
        items = _cached_fetch(src)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {
            "ok": False,
            "source": src,
            "error": f"Could not fetch tactics from {src}: {exc}",
            "tactics": [],
        }
    q = (query or "").strip().lower()
    f = (formation or "").strip().lower().replace(" ", "")
    filtered: list[dict[str, Any]] = []
    for item in items:
        if q:
            hay = f"{item.get('name', '')} {item.get('author', '')} {item.get('formation', '')} {item.get('positions', '')}".lower()
            if q not in hay:
                continue
        if f:
            item_f = f"{item.get('formation', '')} {item.get('name', '')}".lower().replace(" ", "")
            if f not in item_f:
                continue
        filtered.append(item)
    cap = max(1, min(60, int(limit or 25)))
    return {
        "ok": True,
        "source": src,
        "source_url": SORTITOUTSI_URL if src == "sortitoutsi" else FM_ARENA_URL,
        "tactics_dir": detect_fm27_tactics_dir(),
        "total": len(filtered),
        "tactics": filtered[:cap],
    }


def _safe_fmf_filename(name: str) -> str:
    base = os.path.basename(html.unescape(name or "").strip().strip('"').strip("'"))
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", base).strip(" .")
    if not base:
        base = "fm27_tactic"
    if not base.lower().endswith(".fmf"):
        base += ".fmf"
    return base


def download_fm_arena_tactic(thread_url: str, target_dir: str | None = None) -> dict[str, Any]:
    """Download a public ``.fmf`` tactic from an FM-Arena thread into FM27's tactics folder."""
    url = (thread_url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http") or parsed.netloc.lower() not in ("fm-arena.com", "www.fm-arena.com"):
        return {"ok": False, "error": "Only fm-arena.com tactic URLs support direct .fmf download."}

    try:
        if "/attachments/" in parsed.path:
            dl_url = url
        else:
            raw_html, _ = _http_get(url)
            text = raw_html.decode("utf-8", "replace")
            matches = re.findall(r'href="(/attachments/[^"]*-fmf\.\d+/?)"', text, re.I)
            if not matches:
                matches = re.findall(r'href="(/attachments/[^"]+\.fmf[^"]*)"', text, re.I)
            if not matches:
                return {"ok": False, "error": "No .fmf attachment link was found on that FM-Arena thread."}
            dl_url = "https://fm-arena.com" + html.unescape(matches[0])

        blob, hdrs = _http_get(dl_url, max_bytes=_MAX_FMF_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"Download failed: {exc}"}

    if len(blob) < 32 or blob[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return {"ok": False, "error": "Remote server returned HTML instead of an .fmf tactic file."}

    filename = ""
    cd = hdrs.get("content-disposition", "")
    m_fn = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', cd, re.I)
    if m_fn:
        filename = urllib.parse.unquote(m_fn.group(1))
    if not filename:
        slug = dl_url.rstrip("/").split("/")[-1]
        filename = re.sub(r"-fmf\.\d+$", ".fmf", slug, flags=re.I)
    filename = _safe_fmf_filename(filename)

    folder = (target_dir or "").strip() or detect_fm27_tactics_dir()
    if not folder:
        return {"ok": False, "error": "Could not determine FM27 tactics directory."}
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, filename)
    with open(dest, "wb") as handle:
        handle.write(blob)

    return {
        "ok": True,
        "filename": filename,
        "path": dest,
        "size": len(blob),
        "valid_fmf_header": blob.startswith(_FMF_MAGIC),
        "download_url": dl_url,
        "message": f"Installed {filename} into FM27 tactics folder ({len(blob):,} bytes). Load it in-game!",
    }
