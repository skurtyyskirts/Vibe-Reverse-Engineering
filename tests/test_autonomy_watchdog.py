import sys
import types

import pytest

from autonomy.state import PortRun
from autonomy.watchdog import CRASH_LOOP_LIMIT, supervise


class FakeHealth:
    """Serves a scripted sequence of health verdicts."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = []

    def check(self, exe, game_dir=None, frozen_check=0.0, dismiss_dialogs=False):
        self.calls.append({"exe": exe, "dismiss_dialogs": dismiss_dialogs})
        verdict = self.verdicts.pop(0) if self.verdicts else "ok"
        return {"verdict": verdict, "reason": f"scripted {verdict}",
                "error_windows": ([{"title": "Error", "class_name": "#32770"}]
                                  if verdict == "crashed" else []),
                "fatal_log_lines": (["err: device lost"]
                                    if verdict == "runtime-error" else [])}


class FakeProc:
    def __init__(self):
        self.restarts = []

    def restart(self, exe_path, **kwargs):
        self.restarts.append(str(exe_path))
        return {"ok": True}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Install fake livetools modules and hand back a run plus the fakes."""
    def install(verdicts):
        health, proc = FakeHealth(verdicts), FakeProc()
        package = types.ModuleType("livetools")
        package.health, package.procctl = health, proc
        monkeypatch.setitem(sys.modules, "livetools", package)
        monkeypatch.setitem(sys.modules, "livetools.health", health)
        monkeypatch.setitem(sys.modules, "livetools.procctl", proc)
        run = PortRun.create("MyGame", game_dir=str(tmp_path / "game"),
                             exe="game.exe", patches_dir=tmp_path)
        return run, health, proc
    return install


def test_healthy_game_needs_no_action(wired):
    run, health, proc = wired(["ok"])
    result = supervise(run)
    assert result == {**result, "healthy": True, "action": "none"}
    assert proc.restarts == []


def test_dialog_is_dismissed_before_anything_more_drastic(wired):
    run, health, proc = wired(["crashed", "ok"])
    result = supervise(run)
    assert result["action"] == "dismissed-dialog"
    assert result["healthy"]
    assert health.calls[1]["dismiss_dialogs"] is True
    assert proc.restarts == []


def test_hung_game_is_relaunched(wired):
    run, health, proc = wired(["hung", "ok"])
    result = supervise(run)
    assert result["action"] == "relaunched"
    assert result["healthy"]
    assert proc.restarts and proc.restarts[0].endswith("game.exe")


def test_successful_relaunch_clears_the_crash_budget(wired):
    run, _, _ = wired(["not-running", "ok"])
    supervise(run)
    assert run.attempts("watchdog:relaunch") == 0


def test_repeated_crashes_become_a_crash_loop_instead_of_more_relaunches(wired):
    run, _, proc = wired(["not-running"] * 12)
    for _ in range(CRASH_LOOP_LIMIT - 1):
        assert supervise(run)["action"] == "relaunched"
    result = supervise(run)
    assert result["crash_loop"]
    assert result["action"] == "crash-loop"
    assert len(proc.restarts) == CRASH_LOOP_LIMIT - 1
    assert [i["id"] for i in run.open_issues()] == ["crash-loop"]


def test_no_recover_reports_without_touching_the_game(wired):
    run, _, proc = wired(["hung"])
    result = supervise(run, recover=False)
    assert result["verdict"] == "hung"
    assert result["action"] == "none"
    assert proc.restarts == []


def test_a_verdict_a_relaunch_cannot_fix_is_left_alone(wired):
    # A black frame means the capture path or renderer is wrong; restarting
    # the game just reproduces it.
    run, _, proc = wired(["not-rendering"])
    result = supervise(run)
    assert result["action"] == "none"
    assert not result["healthy"]
    assert proc.restarts == []


def test_every_recovery_is_journalled(wired):
    run, _, _ = wired(["hung", "ok"])
    supervise(run)
    journal = (run.root / "journal.md").read_text()
    assert "watchdog relaunching after hung" in journal
    assert "watchdog relaunched the game" in journal


def test_a_game_that_relaunches_fine_but_keeps_dying_still_trips(wired):
    # The attempt budget clears on a successful relaunch, so on its own it can
    # never catch a game that recovers every time and dies again minutes later.
    run, _, proc = wired(["not-running", "ok"] * 6)
    for _ in range(CRASH_LOOP_LIMIT - 1):
        assert supervise(run)["action"] == "relaunched"
        assert run.attempts("watchdog:relaunch") == 0
    result = supervise(run)
    assert result["crash_loop"]
    assert [i["id"] for i in run.open_issues()] == ["crash-loop"]


def test_crash_counting_is_per_phase(wired):
    run, _, _ = wired(["not-running", "ok"] * 6)
    supervise(run)
    assert run.crashes_this_phase() == 1
    run.complete_phase(0, gate="screens/001.png")
    assert run.crashes_this_phase() == 0


def test_a_runtime_error_is_filed_rather_than_relaunched(wired):
    run, _, proc = wired(["runtime-error"])
    result = supervise(run)
    assert result["action"] == "reported"
    assert proc.restarts == []
    assert run.open_issues()[0]["id"].startswith("runtime-error:")
