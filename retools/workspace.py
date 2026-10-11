"""Where per-game project data lives.

The toolkit is installed once and used from many project repos, so a game's data
(``patches/<Game>/ghidra``, ``index.db``, live traces) belongs to the workspace being
worked in — not to the toolkit checkout. The workspace is ``$VRE_WORKSPACE`` when set,
otherwise the current directory, which matches the documented "run tools from your
repo root" convention (and is the toolkit root when working inside this repo).
"""
from __future__ import annotations

import os
from pathlib import Path


def workspace_root() -> Path:
    """Return the directory that holds ``patches/<Game>/``."""
    return Path(os.environ.get("VRE_WORKSPACE") or os.getcwd()).resolve()


def game_dir(game: str) -> Path:
    """Return ``<workspace>/patches/<game>``."""
    return workspace_root() / "patches" / game
