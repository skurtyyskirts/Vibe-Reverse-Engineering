"""Per-game data resolves against the current working directory (the repo being worked in)."""
from pathlib import Path

from retools.workspace import game_dir, workspace_root


def test_workspace_root_is_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert workspace_root() == tmp_path.resolve()


def test_game_dir_is_under_patches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert game_dir("SomeGame") == tmp_path.resolve() / "patches" / "SomeGame"


def test_default_index_db_follows_the_current_directory(tmp_path, monkeypatch):
    from retools.index import GameIndex

    monkeypatch.chdir(tmp_path)
    expected = tmp_path.resolve() / "patches" / "SomeGame" / "index.db"
    assert Path(GameIndex.default_db_path("SomeGame")) == expected
