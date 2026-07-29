import pytest

from livetools import remixctl


@pytest.fixture
def conf(tmp_path):
    return tmp_path / "rtx.conf"


def test_parse_conf_ignores_comments_and_blank_lines():
    text = "# a comment\n\nrtx.zUp = True\n  rtx.sceneScale = 0.5  \nnot an assignment\n"
    options = remixctl.parse_conf(text)
    assert options == {"rtx.zUp": "True", "rtx.sceneScale": "0.5"}


def test_parse_conf_last_assignment_wins():
    options = remixctl.parse_conf("rtx.zUp = False\nrtx.zUp = True\n")
    assert options["rtx.zUp"] == "True"


def test_set_option_creates_file(conf):
    remixctl.set_option(conf, "rtx.fallbackLightMode", "2", backup=False)
    assert remixctl.load_conf(conf) == {"rtx.fallbackLightMode": "2"}


def test_set_option_replaces_in_place_preserving_other_lines(conf):
    conf.write_text("# keep me\nrtx.zUp = False\nrtx.sceneScale = 1\n")
    remixctl.set_option(conf, "rtx.zUp", "True", backup=False)
    lines = conf.read_text().splitlines()
    assert lines == ["# keep me", "rtx.zUp = True", "rtx.sceneScale = 1"]


def test_set_option_removes_duplicate_assignments(conf):
    conf.write_text("rtx.zUp = False\nrtx.zUp = Maybe\n")
    remixctl.set_option(conf, "rtx.zUp", "True", backup=False)
    assert conf.read_text().count("rtx.zUp") == 1


def test_set_option_is_idempotent(conf):
    remixctl.set_option(conf, "rtx.zUp", "True", backup=False)
    remixctl.set_option(conf, "rtx.zUp", "True", backup=False)
    assert conf.read_text().count("rtx.zUp") == 1


def test_set_option_writes_backup(conf, tmp_path):
    conf.write_text("rtx.zUp = False\n")
    bak = remixctl.set_option(conf, "rtx.zUp", "True", backup=True)
    assert bak is not None and bak.exists()
    assert "rtx.zUp = False" in bak.read_text()


def test_set_option_no_backup_for_new_file(conf):
    assert remixctl.set_option(conf, "rtx.zUp", "True", backup=True) is None


def test_backups_in_same_second_stay_distinct(conf):
    conf.write_text("rtx.zUp = False\n")
    bak1 = remixctl.set_option(conf, "rtx.zUp", "True", backup=True)
    bak2 = remixctl.set_option(conf, "rtx.zUp", "False", backup=True)
    assert bak1 != bak2
    assert "rtx.zUp = False" in bak1.read_text()
    assert "rtx.zUp = True" in bak2.read_text()


def test_unset_option(conf):
    conf.write_text("# note\nrtx.zUp = True\nrtx.sceneScale = 1\n")
    assert remixctl.unset_option(conf, "rtx.zUp", backup=False) is True
    assert remixctl.load_conf(conf) == {"rtx.sceneScale": "1"}
    assert "# note" in conf.read_text()
    assert remixctl.unset_option(conf, "rtx.zUp", backup=False) is False


def test_add_hash_appends_and_dedupes_case_insensitively(conf):
    remixctl.add_hash(conf, "rtx.uiTextures", "0xAAAA", backup=False)
    remixctl.add_hash(conf, "rtx.uiTextures", "0xBBBB", backup=False)
    result = remixctl.add_hash(conf, "rtx.uiTextures", "0xaaaa", backup=False)
    assert result == ["0xAAAA", "0xBBBB"]
    assert remixctl.load_conf(conf)["rtx.uiTextures"] == "0xAAAA, 0xBBBB"


def test_remove_hash(conf):
    conf.write_text("rtx.uiTextures = 0xAAAA, 0xBBBB\n")
    assert remixctl.remove_hash(conf, "rtx.uiTextures", "0xaaaa",
                                backup=False) == ["0xBBBB"]


def test_remove_last_hash_unsets_option(conf):
    conf.write_text("rtx.uiTextures = 0xAAAA\n")
    assert remixctl.remove_hash(conf, "rtx.uiTextures", "0xAAAA",
                                backup=False) == []
    assert "rtx.uiTextures" not in remixctl.load_conf(conf)


def test_apply_preset_writes_all_options(conf):
    applied = remixctl.apply_preset(conf, "hash-stable-anim", backup=False)
    options = remixctl.load_conf(conf)
    for key, value in applied.items():
        assert options[key] == value


def test_apply_unknown_preset_raises(conf):
    with pytest.raises(KeyError):
        remixctl.apply_preset(conf, "no-such-preset", backup=False)


def test_presets_reference_known_debug_views():
    idx = int(remixctl.PRESETS["debug-geometry-hash"]
              ["options"]["rtx.debugView.debugViewIdx"])
    assert idx == remixctl.DEBUG_VIEWS["geometry-hash"]


def test_detect_runtime_empty_dir(tmp_path):
    info = remixctl.detect_runtime(tmp_path)
    assert info["is_remix"] is False
    assert info["rtx_conf"] is None
    assert info["logs"] == []


def test_detect_runtime_with_markers(tmp_path):
    (tmp_path / "d3d9.dll").write_bytes(b"\0" * 128)
    (tmp_path / ".trex").mkdir()
    (tmp_path / "rtx.conf").write_text("rtx.zUp = True\n")
    (tmp_path / "game_d3d9.log").write_text("info: boot\n")
    info = remixctl.detect_runtime(tmp_path)
    assert info["is_remix"] is True
    assert any(".trex" in m for m in info["remix_markers"])
    assert info["rtx_conf"] is not None
    assert [lg["name"] for lg in info["logs"]] == ["game_d3d9.log"]


def test_detect_runtime_size_hint_alone_is_not_remix(tmp_path):
    (tmp_path / "d3d9.dll").write_bytes(b"\0" * remixctl._REMIX_D3D9_MIN_BYTES)
    info = remixctl.detect_runtime(tmp_path)
    assert any("large d3d9.dll" in m for m in info["remix_markers"])
    assert info["is_remix"] is False


def test_detect_runtime_size_hint_plus_rtx_conf_is_remix(tmp_path):
    (tmp_path / "d3d9.dll").write_bytes(b"\0" * remixctl._REMIX_D3D9_MIN_BYTES)
    (tmp_path / "rtx.conf").write_text("rtx.zUp = True\n")
    assert remixctl.detect_runtime(tmp_path)["is_remix"] is True


def test_read_logs_tail_and_errors_only(tmp_path):
    lines = [f"info: line {i}" for i in range(10)] + ["err: boom", "warn: odd"]
    (tmp_path / "game_d3d9.log").write_text("\n".join(lines) + "\n")
    tail = remixctl.read_logs(tmp_path, tail=3)
    assert tail["game_d3d9.log"] == ["info: line 9", "err: boom", "warn: odd"]
    errors = remixctl.read_logs(tmp_path, tail=10, errors_only=True)
    assert errors["game_d3d9.log"] == ["err: boom", "warn: odd"]


# ── backups ────────────────────────────────────────────────────────────────

def test_backups_are_collected_in_one_folder_not_scattered(tmp_path):
    conf = tmp_path / "rtx.conf"
    remixctl.set_option(conf, "rtx.zUp", "False", backup=False)
    remixctl.set_option(conf, "rtx.zUp", "True")
    remixctl.set_option(conf, "rtx.sceneScale", "2")

    backups = tmp_path / remixctl.BACKUP_SUBDIR
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        remixctl.BACKUP_SUBDIR, "rtx.conf"]
    assert len(list(backups.iterdir())) == 2


def test_backup_dir_can_be_pointed_at_the_project_workspace(tmp_path):
    conf = tmp_path / "game" / "rtx.conf"
    conf.parent.mkdir()
    project = tmp_path / "patches" / "MyGame" / "backups"
    remixctl.set_option(conf, "rtx.zUp", "False", backup=False)
    remixctl.set_option(conf, "rtx.zUp", "True", backup_dir=project)
    assert len(list(project.iterdir())) == 1
    assert not (conf.parent / remixctl.BACKUP_SUBDIR).exists()


def test_no_backup_is_written_for_a_file_that_does_not_exist_yet(tmp_path):
    conf = tmp_path / "rtx.conf"
    assert remixctl.set_option(conf, "rtx.zUp", "True") is None
    assert not (tmp_path / remixctl.BACKUP_SUBDIR).exists()


# ── USD captures ───────────────────────────────────────────────────────────

def _make_capture(game_dir, stage="capture_2026_07_29.usd", textures=(),
                  meshes=()):
    root = game_dir / "rtx-remix" / "captures"
    root.mkdir(parents=True, exist_ok=True)
    (root / stage).write_text("#usda 1.0\n")
    for folder, names in (("textures", textures), ("meshes", meshes)):
        if names:
            (root / folder).mkdir(exist_ok=True)
        for name in names:
            (root / folder / name).write_bytes(b"")
    return root


def test_no_captures_reports_empty(tmp_path):
    assert remixctl.list_captures(tmp_path) == []
    assert remixctl.newest_capture(tmp_path) is None
    assert remixctl.capture_assets(tmp_path)["assets"] == {}


def test_captures_are_listed_newest_last(tmp_path):
    root = _make_capture(tmp_path, stage="capture_a.usd")
    (root / "capture_b.usd").write_text("#usda 1.0\n")
    import os
    os.utime(root / "capture_a.usd", (1, 1))
    names = [c["name"] for c in remixctl.list_captures(tmp_path)]
    assert names == ["capture_a.usd", "capture_b.usd"]
    assert remixctl.newest_capture(tmp_path)["name"] == "capture_b.usd"


def test_asset_hashes_are_read_from_exported_filenames(tmp_path):
    _make_capture(tmp_path,
                  textures=["A1B2C3D4E5F60718.dds", "deadbeefcafe1234.dds"],
                  meshes=["0123456789ABCDEF.usd"])
    found = remixctl.capture_assets(tmp_path)
    assert [e["hash"] for e in found["assets"]["texture"]] == [
        "0xA1B2C3D4E5F60718", "0xDEADBEEFCAFE1234"]
    assert found["counts"] == {"texture": 2, "mesh": 1}


def test_generated_names_are_not_mistaken_for_hashes(tmp_path):
    # Sky probes and thumbnails share the folder but are not asset identities.
    _make_capture(tmp_path, textures=["stage_T_SkyProbe.dds", "ab.dds",
                                      "A1B2C3D4E5F60718.dds"])
    hashes = [e["hash"] for e in remixctl.capture_assets(tmp_path)["assets"]["texture"]]
    assert hashes == ["0xA1B2C3D4E5F60718"]


def test_hashes_from_a_capture_are_accepted_by_add_hash(tmp_path):
    _make_capture(tmp_path, textures=["A1B2C3D4E5F60718.dds"])
    digest = remixctl.capture_assets(tmp_path)["assets"]["texture"][0]["hash"]
    conf = tmp_path / "rtx.conf"
    assert remixctl.add_hash(conf, "rtx.uiTextures", digest, backup=False) == [digest]
    assert remixctl.load_conf(conf)["rtx.uiTextures"] == digest


def test_capture_ready_preset_disables_the_menu_prompt():
    options = remixctl.PRESETS["capture-ready"]["options"]
    assert options["rtx.captureShowMenuOnHotkey"] == "False"
