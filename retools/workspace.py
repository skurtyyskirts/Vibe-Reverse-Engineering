"""Where per-game project data lives.

The toolkit is installed once and used from many project repos, so a game's data
(``patches/<Game>/ghidra``, ``index.db``, live traces) belongs to the repo being worked
in — not to the toolkit checkout. That repo is the current working directory: the same
directory every relative ``--project patches/<Game>`` argument is resolved against, so the
Ghidra daemon, the index readers and the writers all agree (and inside this repo it is
the toolkit root, as before).
"""
from __future__ import annotations

from pathlib import Path


def workspace_root() -> Path:
    """Return the directory that holds ``patches/<Game>/`` (the current working directory)."""
    return Path.cwd().resolve()


def game_dir(game: str) -> Path:
    """Return ``<workspace>/patches/<game>``."""
    return workspace_root() / "patches" / game
