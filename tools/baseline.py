"""Record and check a version baseline for this project.

The project lives in OneDrive without git, so "roll back to the last good
version" needs a manifest of what that version was. This tool writes one and
checks the working tree against it.

    python tools/baseline.py --write      record the current tree as the baseline
    python tools/baseline.py              check the tree against the baseline
    python tools/baseline.py --json       same check, machine-readable

Exit code 0 means every recorded file is present and unchanged (new files that
are not recorded are reported as additions but do not fail the check). Exit
code 1 means a recorded file changed or disappeared.

What is recorded: source, tests, data files, docs and mechanics.json. What is
not: .venv, __pycache__, shots, and anything bigger than MAX_BYTES.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(APP, "versions", "BASELINE.json")

SKIP_DIRS = {".venv", "__pycache__", "shots", "versions", ".git", "node_modules",
             "reloads",
             # Ghidra's redirected USERPROFILE (see tools/ghidra_*.bat). It is
             # a tool cache, not source: recording it would make the baseline
             # change whenever a headless run touches its own settings.
             "ghhome", "ghidra_out", "ghidra_projects"}
SKIP_SUFFIXES = {".pyc", ".log", ".png", ".jpg", ".out", ".err", ".gbf", ".map"}
MAX_BYTES = 64 * 1024 * 1024


def digest(path: str) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def walk() -> dict[str, str]:
    found: dict[str, str] = {}
    for root, dirs, files in os.walk(APP):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(root, name)
            if os.path.splitext(name)[1].lower() in SKIP_SUFFIXES:
                continue
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    continue
                rel = os.path.relpath(path, APP).replace("\\", "/")
                found[rel] = digest(path)
            except OSError:
                continue
    return found


def load() -> dict:
    with open(MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)


def write() -> int:
    files = walk()
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    payload = {
        "version": 1,
        "label": "last build that passed every check",
        "note": ("Recorded so a later agent can tell what changed. "
                 "Run tools/baseline.py to check, --write to re-record."),
        "files": files,
    }
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print(f"recorded {len(files)} files -> {os.path.relpath(MANIFEST, APP)}")
    return 0


def check(as_json: bool = False) -> int:
    if not os.path.exists(MANIFEST):
        print("no baseline yet; run with --write first")
        return 1
    data = load()
    recorded: dict[str, str] = data.get("files", {})
    current = walk()
    changed = sorted(k for k, v in recorded.items()
                     if k in current and current[k] != v)
    missing = sorted(k for k in recorded if k not in current)
    added = sorted(k for k in current if k not in recorded)
    ok = not changed and not missing
    if as_json:
        print(json.dumps({"ok": ok, "changed": changed, "missing": missing,
                          "added": added, "recorded": len(recorded),
                          "current": len(current)}, indent=1))
        return 0 if ok else 1
    print(f"baseline: {len(recorded)} files; tree: {len(current)} files")
    if changed:
        print("changed:")
        for name in changed:
            print(f"  ~ {name}")
    if missing:
        print("missing:")
        for name in missing:
            print(f"  - {name}")
    if added:
        print(f"new (not part of the baseline): {len(added)}")
        for name in added[:20]:
            print(f"  + {name}")
        if len(added) > 20:
            print(f"  … and {len(added) - 20} more")
    print("OK: tree matches the recorded baseline" if ok
          else "DRIFT: the tree is not the recorded baseline")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if "--write" in argv:
        return write()
    return check("--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
