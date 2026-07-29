"""The exit-code contract the unattended loop branches on.

0 succeeded · 1 the command failed · 2 bad invocation · 3 it ran and the answer
was no. A handler that prints an error and returns None exits 0, which reads to
the loop as success — the reason these are tested rather than assumed.
"""

import pytest

from livetools.__main__ import EXIT_FAILED, EXIT_NEGATIVE, main
from livetools.screenshot import encode_png

W, H = 32, 24


@pytest.fixture
def game_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def images(tmp_path):
    black = tmp_path / "black.png"
    black.write_bytes(encode_png(W, H, bytes(W * H * 3)))
    busy = tmp_path / "busy.png"
    busy.write_bytes(encode_png(W, H, bytes(
        (x * 37 + y * 91) % 256 for y in range(H) for x in range(W)
        for _ in range(3))))
    return {"black": str(black), "busy": str(busy)}


# ── bad invocation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["remix"], ["remix", "conf"], ["remix", "preset"],
    ["remix", "capture"], ["remix", "options"],
    ["screenshot"], ["proc"],
])
def test_a_subcommand_with_no_action_is_a_bad_invocation(argv):
    assert main(argv) == 2


# ── failure ───────────────────────────────────────────────────────────────

def test_unknown_preset_fails(game_dir):
    assert main(["remix", "preset", "apply", "no-such-preset",
                 "-d", game_dir]) == EXIT_FAILED


def test_unknown_option_name_fails(game_dir):
    assert main(["remix", "conf", "set", "rtx.uiTexture", "0x1",
                 "-d", game_dir]) == EXIT_FAILED


def test_bad_option_value_fails(game_dir):
    assert main(["remix", "conf", "set", "rtx.zUp", "yes",
                 "-d", game_dir]) == EXIT_FAILED


def test_a_hash_written_to_a_non_hash_option_fails(game_dir):
    assert main(["remix", "conf", "add-hash", "rtx.zUp", "0xA1B2C3D4",
                 "-d", game_dir]) == EXIT_FAILED


def test_showing_an_unknown_option_fails():
    assert main(["remix", "options", "show", "rtx.notAnOption"]) == EXIT_FAILED


def test_reading_a_missing_image_fails(tmp_path):
    assert main(["screenshot", "stats", str(tmp_path / "absent.png")]) == EXIT_FAILED


def test_a_malformed_tile_grid_fails(images):
    assert main(["screenshot", "diff", images["busy"], images["busy"],
                 "--tiles", "four-by-three"]) == EXIT_FAILED


# ── ran, and the answer was no ────────────────────────────────────────────

def test_an_unusable_frame_is_a_negative_answer(images):
    assert main(["screenshot", "stats", images["black"]]) == EXIT_NEGATIVE


def test_identical_captures_are_a_negative_answer(images):
    assert main(["screenshot", "diff", images["busy"], images["busy"]]) == EXIT_NEGATIVE


# ── success ───────────────────────────────────────────────────────────────

def test_a_usable_frame_succeeds(images):
    assert main(["screenshot", "stats", images["busy"]]) == 0


def test_a_changed_capture_succeeds(images):
    assert main(["screenshot", "diff", images["black"], images["busy"]]) == 0


def test_valid_option_writes_succeed(game_dir):
    assert main(["remix", "conf", "set", "rtx.zUp", "True", "-d", game_dir]) == 0
    assert main(["remix", "conf", "add-hash", "rtx.uiTextures",
                 "0xA1B2C3D4E5F60718", "-d", game_dir]) == 0
    assert main(["remix", "conf", "get", "-d", game_dir]) == 0


def test_preset_apply_and_status_succeed(game_dir):
    assert main(["remix", "preset", "apply", "automation", "-d", game_dir]) == 0
    assert main(["remix", "preset", "list"]) == 0
    assert main(["remix", "status", "-d", game_dir]) == 0


def test_option_search_and_debugviews_succeed():
    assert main(["remix", "options", "search", "sky"]) == 0
    assert main(["remix", "debugviews"]) == 0


def test_forcing_past_validation_succeeds(game_dir):
    assert main(["remix", "conf", "set", "rtx.notAnOption", "1",
                 "-d", game_dir, "--force"]) == 0
