import json

import pytest

from livetools import gamectl


def test_save_creates_the_file_and_its_parents(tmp_path):
    path = tmp_path / "patches" / "MyGame" / "macros.json"
    result = gamectl.save_macro(path, "title_to_gameplay",
                                "RETURN WAIT:1500 DOWN RETURN",
                                description="title -> level 1")
    assert result["replaced"] is False
    assert json.loads(path.read_text())["title_to_gameplay"]["steps"] == \
        "RETURN WAIT:1500 DOWN RETURN"


def test_saved_macros_load_back(tmp_path):
    path = tmp_path / "macros.json"
    gamectl.save_macro(path, "a", "RETURN", description="first")
    gamectl.save_macro(path, "b", "ESCAPE", description="second")
    macros = gamectl.load_macros(path)
    assert sorted(macros) == ["a", "b"]
    assert macros["b"]["description"] == "second"


def test_resaving_keeps_the_description_when_only_timing_changes(tmp_path):
    path = tmp_path / "macros.json"
    gamectl.save_macro(path, "nav", "RETURN WAIT:500", description="the path")
    result = gamectl.save_macro(path, "nav", "RETURN WAIT:2000")
    assert result["replaced"] is True
    macros = gamectl.load_macros(path)
    assert macros["nav"]["description"] == "the path"
    assert macros["nav"]["steps"] == "RETURN WAIT:2000"


def test_an_explicit_description_overwrites_the_old_one(tmp_path):
    path = tmp_path / "macros.json"
    gamectl.save_macro(path, "nav", "RETURN", description="old")
    gamectl.save_macro(path, "nav", "RETURN", description="new")
    assert gamectl.load_macros(path)["nav"]["description"] == "new"


def test_steps_are_stripped(tmp_path):
    path = tmp_path / "macros.json"
    gamectl.save_macro(path, "nav", "  RETURN DOWN  ")
    assert gamectl.load_macros(path)["nav"]["steps"] == "RETURN DOWN"


def test_refuses_to_save_an_empty_macro(tmp_path):
    with pytest.raises(ValueError):
        gamectl.save_macro(tmp_path / "macros.json", "nav", "   ")


def test_loading_a_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        gamectl.load_macros(tmp_path / "absent.json")


def test_loading_a_non_object_raises(tmp_path):
    path = tmp_path / "macros.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(ValueError):
        gamectl.load_macros(path)


def test_remix_hotkey_spellings_resolve_to_the_same_keys():
    # rtx.conf writes "CTRL, SHFT, Q"; a chord copied from it must work.
    assert gamectl.VK_MAP["SHFT"] == gamectl.VK_MAP["SHIFT"]
    assert gamectl.VK_MAP["CTL"] == gamectl.VK_MAP["CTRL"]


def test_input_control_is_guarded_off_windows():
    import sys

    if sys.platform == "win32":
        pytest.skip("guard only applies off Windows")
    with pytest.raises(OSError):
        gamectl.find_pids("game.exe")


# ── input correctness ──────────────────────────────────────────────────────

def test_arrow_and_navigation_keys_are_marked_extended():
    # Games reading scancodes see numpad keys instead without this flag, and
    # menu navigation is arrows.
    for name in ("UP", "DOWN", "LEFT", "RIGHT", "HOME", "END",
                 "PAGEUP", "PAGEDOWN", "DELETE"):
        assert gamectl.VK_MAP[name] in gamectl.EXTENDED_VKS, name


def test_ordinary_keys_are_not_marked_extended():
    for name in ("RETURN", "ESCAPE", "SPACE", "A", "F5", "SHIFT"):
        assert gamectl.VK_MAP[name] not in gamectl.EXTENDED_VKS, name


def test_mouse_look_deltas_sum_to_the_requested_motion(monkeypatch):
    sent = []

    def fake_inject(*inputs):
        sent.extend((i.union.mi.dx, i.union.mi.dy) for i in inputs)
        return len(inputs)

    monkeypatch.setattr(gamectl, "_inject", fake_inject)
    result = gamectl.move_mouse(100, -35, steps=7, step_ms=0)

    assert result["ok"]
    assert len(sent) == 7
    assert sum(dx for dx, _ in sent) == 100
    assert sum(dy for _, dy in sent) == -35


def test_mouse_look_reports_blocked_injection(monkeypatch):
    monkeypatch.setattr(gamectl, "_inject", lambda *inputs: 0)
    assert not gamectl.move_mouse(10, 10, steps=2, step_ms=0)["ok"]


def test_mouse_look_rejects_a_zero_step_count():
    assert not gamectl.move_mouse(10, 10, steps=0)["ok"]
