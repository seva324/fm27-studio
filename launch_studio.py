"""Standalone desktop launcher for FM27 Studio (local web UI + pure-stdlib API).

Starts the loopback HTTP/SSE server bound to ``127.0.0.1`` with a per-launch
random token, then opens it in a frameless Microsoft Edge (or Chrome) app
window with an isolated user-data directory so it feels like a native desktop
application.  Falls back to the default system browser if neither Edge nor
Chrome is installed.

Usage:
    python -S launch_studio.py
    python -S launch_studio.py --port 8765 --no-browser
    python -S main.py --studio
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from fmrte.db import Snapshot
from fmrte.studio_api import StudioBackend, StudioServer


def find_app_browser() -> str | None:
    """Locate a Chromium-based browser that supports ``--app=`` desktop mode."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    folder = Path(base) / "FM27Studio" / "BrowserProfile"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def launch_browser_window(url: str, *, width: int = 1540, height: int = 920) -> subprocess.Popen | None:
    """Open ``url`` in a standalone app window and return the process handle."""
    browser = find_app_browser()
    if not browser:
        webbrowser.open(url)
        return None
    profile = _profile_dir()
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=TranslateUI",
        "--disable-background-mode",
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, creationflags=creationflags)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FM27 Studio desktop launcher")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0,
                        help="Bind port (default: 0 = automatic free port)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Only run the local server and print the URL")
    parser.add_argument("--no-auto-attach", action="store_true",
                        help="Do not auto-attach to FM27 on startup")
    parser.add_argument("--width", type=int, default=1540,
                        help="Initial app window width")
    parser.add_argument("--height", type=int, default=920,
                        help="Initial app window height")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = Snapshot()
    backend = StudioBackend(db)
    if not args.no_auto_attach:
        try:
            backend.attach_game(refresh_all=False)
        except Exception:
            pass
    server = StudioServer(backend, host=args.host, port=args.port)
    url = server.start()
    print(f"FM27 Studio running at {url}", flush=True)

    proc: subprocess.Popen | None = None
    try:
        if not args.no_browser:
            proc = launch_browser_window(url, width=args.width, height=args.height)
            if proc is not None:
                # Wait while the browser process is alive. Note that Edge/Chrome
                # may hand off to an existing BrowserProfile instance and exit
                # the launcher process in 1-6s while the Studio window stays open;
                # keep serving as long as the UI heartbeat (/api/ping) is active.
                start = time.monotonic()
                while proc.poll() is None:
                    time.sleep(0.4)
                elapsed = time.monotonic() - start
                if elapsed < 12.0 or server.is_client_active(within_seconds=35.0):
                    print("Browser window active (server stays running until window closes or Ctrl+C).", flush=True)
                    idle_grace = time.monotonic() + 25.0
                    while True:
                        time.sleep(1.0)
                        if time.monotonic() < idle_grace:
                            continue
                        if server.had_client and not server.is_client_active(within_seconds=45.0):
                            print("Studio window closed; shutting down server.", flush=True)
                            break
            else:
                print("Opened in default browser (press Ctrl+C to stop the server).", flush=True)
                while True:
                    time.sleep(0.5)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
