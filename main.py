"""Entry point for FM27 Studio."""

from __future__ import annotations

import sys

from launch_studio import main as studio_main


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cleaned = [arg for arg in raw if arg != "--studio"]
    return studio_main(cleaned)


if __name__ == "__main__":
    raise SystemExit(main())
