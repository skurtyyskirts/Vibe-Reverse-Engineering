import random

import pytest

from autonomy import baseline
from autonomy.state import PortRun
from livetools.screenshot import encode_png

W, H = 48, 32


def noise_png(seed=1):
    rng = random.Random(seed)
    return encode_png(W, H, bytes(rng.randrange(256) for _ in range(W * H * 3)))


def nudged_png(seed=1, changed_rows=0):
    """A capture identical to noise_png except for the first N rows."""
    rng = random.Random(seed)
    rgb = bytearray(rng.randrange(256) for _ in range(W * H * 3))
    for y in range(changed_rows):
        for x in range(W):
            i = (y * W + x) * 3
            rgb[i] = 255 - rgb[i]
    return encode_png(W, H, bytes(rgb))


@pytest.fixture
def run(tmp_path):
    return PortRun.create("MyGame", game_dir="C:/G", exe="game.exe",
                          patches_dir=tmp_path)


@pytest.fixture
def shot(tmp_path):
    def make(name, data):
        path = tmp_path / name
        path.write_bytes(data)
        return path
    return make


def test_save_records_a_reference_frame(run, shot):
    result = baseline.save(run, "ingame-lit", shot("a.png", noise_png()),
                           note="fallback light on")
    assert result["replaced"] is False
    assert baseline.baseline_path(run, "ingame-lit").is_file()
    assert run.data["baselines"]["ingame-lit"]["note"] == "fallback light on"


def test_saving_twice_replaces_the_reference(run, shot):
    baseline.save(run, "ingame", shot("a.png", noise_png(1)))
    result = baseline.save(run, "ingame", shot("b.png", noise_png(2)))
    assert result["replaced"] is True
    assert len(baseline.listing(run)) == 1


def test_labels_with_awkward_characters_still_get_a_filename(run, shot):
    baseline.save(run, "in game / lit", shot("a.png", noise_png()))
    assert baseline.baseline_path(run, "in game / lit").is_file()


def test_saving_a_missing_capture_raises(run, tmp_path):
    with pytest.raises(FileNotFoundError):
        baseline.save(run, "ingame", tmp_path / "absent.png")


def test_an_identical_capture_is_unchanged(run, shot):
    baseline.save(run, "ingame", shot("a.png", noise_png()))
    result = baseline.check(run, "ingame", shot("b.png", noise_png()))
    assert result["same"]
    assert result["verdict"] == "unchanged"
    assert result["ratio"] == 0.0


def test_a_regressed_frame_is_flagged_and_localized(run, shot):
    baseline.save(run, "ingame", shot("a.png", nudged_png(changed_rows=0)))
    result = baseline.check(run, "ingame",
                            shot("b.png", nudged_png(changed_rows=H // 2)))
    assert not result["same"]
    assert result["verdict"] == "changed"
    assert result["hottest"]["row"] == 0


def test_renderer_noise_below_the_floor_is_not_a_regression(run, shot):
    # One pixel out of 1536 is 0.00065 — under the floor, so a frame that only
    # differs by dithering does not read as a broken scene.
    baseline.save(run, "ingame", shot("a.png", nudged_png(changed_rows=0)))
    single = bytearray(nudged_png(changed_rows=0))
    result = baseline.check(run, "ingame", shot("b.png", bytes(single)))
    assert result["same"]
    assert result["threshold"] == baseline.NOISE_FLOOR


def test_a_stricter_threshold_can_be_demanded(run, shot):
    baseline.save(run, "ingame", shot("a.png", nudged_png(changed_rows=0)))
    result = baseline.check(run, "ingame",
                            shot("b.png", nudged_png(changed_rows=1)),
                            threshold=0.0001)
    assert not result["same"]


def test_checking_an_unsaved_baseline_raises(run, shot):
    with pytest.raises(FileNotFoundError):
        baseline.check(run, "never-saved", shot("a.png", noise_png()))


def test_a_resolution_change_is_refused_rather_than_reported_as_a_regression(
        run, shot, tmp_path):
    baseline.save(run, "ingame", shot("a.png", noise_png()))
    smaller = tmp_path / "small.png"
    smaller.write_bytes(encode_png(8, 8, bytes(8 * 8 * 3)))
    with pytest.raises(ValueError, match="re-save the baseline"):
        baseline.check(run, "ingame", smaller)


def test_listing_reports_whether_the_file_survives(run, shot):
    baseline.save(run, "ingame", shot("a.png", noise_png()))
    assert baseline.listing(run)[0]["present"] is True
    baseline.baseline_path(run, "ingame").unlink()
    assert baseline.listing(run)[0]["present"] is False


def test_baselines_survive_reopening_the_run(run, shot, tmp_path):
    baseline.save(run, "ingame", shot("a.png", noise_png()))
    reopened = PortRun.open("MyGame", patches_dir=tmp_path)
    assert baseline.check(reopened, "ingame", shot("b.png", noise_png()))["same"]
