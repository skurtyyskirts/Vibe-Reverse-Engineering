import json

import pytest

from autonomy.state import ATTEMPT_LIMIT, PHASES, PortRun, phase_name


@pytest.fixture
def run(tmp_path):
    return PortRun.create("MyGame", game_dir="C:/Games/MyGame", exe="game.exe",
                          patches_dir=tmp_path, goal="renders correctly")


def test_create_lays_out_the_workspace(run, tmp_path):
    assert (tmp_path / "MyGame" / "autonomy" / "state.json").is_file()
    assert (tmp_path / "MyGame" / "autonomy" / "screens").is_dir()
    assert (tmp_path / "MyGame" / "autonomy" / "journal.md").is_file()


def test_create_refuses_to_clobber_an_existing_run(run, tmp_path):
    with pytest.raises(FileExistsError):
        PortRun.create("MyGame", game_dir="x", exe="y", patches_dir=tmp_path)


def test_open_missing_run_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        PortRun.open("Nope", patches_dir=tmp_path)


def test_state_survives_reopen(run, tmp_path):
    run.step(action="sent RETURN", outcome="ok", key="nav:title",
             next_action="select Play")
    reopened = PortRun.open("MyGame", patches_dir=tmp_path)
    assert reopened.data["next_action"] == "select Play"
    assert reopened.data["steps"] == 1


def test_failures_accumulate_until_the_key_is_exhausted(run):
    for _ in range(ATTEMPT_LIMIT - 1):
        result = run.step(action="retry", outcome="fail", key="nav:title")
        assert not result["exhausted"]
    result = run.step(action="retry", outcome="fail", key="nav:title")
    assert result["exhausted"]
    assert run.exhausted_keys() == ["nav:title"]


def test_success_clears_the_failure_budget(run):
    run.step(action="try", outcome="fail", key="nav:title")
    run.step(action="try", outcome="fail", key="nav:title")
    run.step(action="try again", outcome="ok", key="nav:title")
    assert run.attempts("nav:title") == 0
    assert not run.exhausted("nav:title")


def test_info_outcome_leaves_the_budget_alone(run):
    run.step(action="observe", outcome="info", key="nav:title")
    assert run.attempts("nav:title") == 0


def test_unknown_outcome_rejected(run):
    with pytest.raises(ValueError):
        run.step(action="x", outcome="maybe")


def test_completing_a_phase_requires_evidence(run):
    with pytest.raises(ValueError):
        run.complete_phase(0, gate="   ")


def test_completing_a_phase_advances_to_the_next_pending_one(run):
    run.start_phase(0)
    assert run.complete_phase(0, gate="screens/001_0_boot.png") == 1
    assert run.phase_status(0) == "done"
    assert run.data["gates"]["0"]["evidence"] == "screens/001_0_boot.png"


def test_completed_phases_are_not_revisited(run):
    run.complete_phase(0, gate="a")
    run.complete_phase(1, gate="b")
    run.set_phase_status(2, "skipped", note="no shaders")
    assert run.complete_phase(3, gate="c") == 4


def test_unknown_phase_status_rejected(run):
    with pytest.raises(ValueError):
        run.set_phase_status(0, "nearly")


def test_shot_paths_are_monotonic_and_labelled(run):
    run.start_phase(2)
    first = run.shot_path("boot")
    second = run.shot_path("title screen")
    assert first.name == "001_2_boot.png"
    # Characters that are not filename-safe are replaced, not dropped.
    assert second.name == "002_2_title-screen.png"


def test_issues_open_and_resolve(run):
    run.add_issue("unstable-hud", "HUD hashes churn", evidence="screens/012.png")
    assert [i["id"] for i in run.open_issues()] == ["unstable-hud"]
    run.resolve_issue("unstable-hud", resolution="tagged rtx.uiTextures")
    assert run.open_issues() == []


def test_resolving_an_unknown_issue_raises(run):
    with pytest.raises(KeyError):
        run.resolve_issue("nope", resolution="x")


def test_reopening_an_issue_updates_it_in_place(run):
    run.add_issue("flicker", "first sighting")
    run.add_issue("flicker", "still there", evidence="screens/030.png")
    assert len(run.data["issues"]) == 1
    assert run.data["issues"][0]["evidence"] == "screens/030.png"


def test_journal_records_every_step_with_its_evidence(run):
    run.step(action="sent RETURN", outcome="ok", key="nav:title",
             evidence="screens/003.png", conclusion="reached main menu")
    journal = (run.root / "journal.md").read_text()
    assert "sent RETURN" in journal
    assert "screens/003.png" in journal
    assert "reached main menu" in journal


def test_report_covers_phases_issues_and_abandoned_approaches(run):
    run.complete_phase(0, gate="screens/001.png")
    run.add_issue("sky-missing", "sky drawn as near geometry")
    for _ in range(ATTEMPT_LIMIT):
        run.step(action="retry", outcome="fail", key="nav:title")
    run.finish("partial", "reaches gameplay, sky still wrong")

    report = run.report()
    assert "partial" in report
    assert "sky-missing" in report
    assert "nav:title" in report
    assert phase_name(0) in report


def test_status_is_json_serializable(run):
    json.dumps(run.status())


def test_every_phase_index_has_a_name():
    assert all(phase_name(i) == PHASES[i] for i in PHASES)
    assert phase_name(99) == "unknown-99"
