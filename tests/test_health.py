import pytest

from livetools.health import FATAL_LOG_MARKERS, fatal_log_lines, verdict_for

HEALTHY = {"crash_reporters": [], "error_windows": [], "pid": 42, "hwnd": 7,
           "responding": True,
           "frame": {"usable": True, "verdict": "content", "reason": "ok"},
           "frozen": False}


def probes(**overrides):
    return {**HEALTHY, **overrides}


def test_healthy_game_is_ok():
    assert verdict_for(probes())[0] == "ok"


def test_crash_reporter_outranks_everything_else():
    # WerFault outlives the game, so every other probe reads "not running".
    verdict, reason = verdict_for(probes(crash_reporters=[{"exe": "WerFault.exe"}],
                                         pid=None, hwnd=None))
    assert verdict == "crashed"
    assert "WerFault" in reason


def test_error_dialog_is_a_crash_not_a_hang():
    verdict, reason = verdict_for(probes(
        error_windows=[{"title": "game.exe has stopped working",
                        "class_name": "#32770"}]))
    assert verdict == "crashed"
    assert "has stopped working" in reason


def test_dialog_without_a_title_falls_back_to_its_class():
    _, reason = verdict_for(probes(
        error_windows=[{"title": "", "class_name": "#32770"}]))
    assert "#32770" in reason


def test_no_process_is_not_running():
    assert verdict_for(probes(pid=None, hwnd=None))[0] == "not-running"


def test_process_without_a_window_is_still_starting():
    assert verdict_for(probes(hwnd=None))[0] == "no-window"


def test_unresponsive_window_is_hung():
    assert verdict_for(probes(responding=False))[0] == "hung"


def test_black_frame_is_reported_even_though_the_window_answers():
    verdict, reason = verdict_for(probes(
        frame={"usable": False, "verdict": "black", "reason": "all black"}))
    assert verdict == "not-rendering"
    assert "black" in reason


def test_identical_frames_are_frozen():
    assert verdict_for(probes(frozen=True))[0] == "frozen"


def test_missing_frame_probe_does_not_block_an_ok_verdict():
    assert verdict_for(probes(frame=None, frozen=None))[0] == "ok"


@pytest.mark.parametrize("marker", FATAL_LOG_MARKERS)
def test_every_fatal_marker_is_detected(tmp_path, marker):
    (tmp_path / "game_d3d9.log").write_text(
        f"info: routine line\nerr: something {marker} happened\n")
    hits = fatal_log_lines(tmp_path)
    assert len(hits) == 1
    assert marker in hits[0].lower()


def test_routine_log_lines_are_not_fatal(tmp_path):
    (tmp_path / "game_d3d9.log").write_text(
        "info: DXVK: Game: game.exe\nwarn: unsupported texture format\n")
    assert fatal_log_lines(tmp_path) == []


def test_fatal_lines_are_deduplicated(tmp_path):
    (tmp_path / "game_d3d9.log").write_text("err: device lost\n" * 5)
    assert len(fatal_log_lines(tmp_path)) == 1


def test_no_logs_means_no_fatals(tmp_path):
    assert fatal_log_lines(tmp_path) == []
