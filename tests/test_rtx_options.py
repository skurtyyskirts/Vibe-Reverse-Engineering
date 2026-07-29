import pytest

from livetools import rtx_options


@pytest.fixture
def table(monkeypatch, tmp_path):
    data = tmp_path / "rtx_options.tsv"
    data.write_text(
        "# header comment\n"
        "rtx.uiTextures\thashes\t\t\t\tScreen-space UI textures to rasterize.\n"
        "rtx.zUp\tbool\tFalse\t\t\tZ is the world up axis.\n"
        "rtx.uniqueObjectDistance\tfloat\t300\t0\t\t"
        "Distance an object may move and stay the same object.\n",
        encoding="utf-8")
    monkeypatch.setattr(rtx_options, "DATA_FILE", data)
    monkeypatch.setattr(rtx_options, "_cache", None)
    return data


def test_lookup_returns_full_metadata(table):
    entry = rtx_options.lookup("rtx.uniqueObjectDistance")
    assert entry["type"] == "float"
    assert entry["default"] == "300"
    assert entry["min"] == "0"
    assert "same object" in entry["description"]


def test_lookup_of_an_unknown_option_is_none(table):
    assert rtx_options.lookup("rtx.nope") is None


def test_comment_lines_are_not_options(table):
    assert "# header comment" not in rtx_options.load()


def test_known_options_pass_validation(table):
    assert rtx_options.is_known("rtx.zUp")
    assert not rtx_options.is_known("rtx.uiTexture")


def test_a_missing_table_disables_validation_rather_than_blocking(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setattr(rtx_options, "DATA_FILE", tmp_path / "absent.tsv")
    monkeypatch.setattr(rtx_options, "_cache", None)
    assert rtx_options.is_known("rtx.anythingAtAll")


def test_suggest_catches_a_missing_plural(table):
    assert "rtx.uiTextures" in rtx_options.suggest("rtx.uiTexture")


def test_suggest_ignores_names_with_nothing_in_common(table):
    assert rtx_options.suggest("totally.different") == []


def test_search_matches_names_before_descriptions(table):
    hits = rtx_options.search("uiTextures")
    assert hits[0]["name"] == "rtx.uiTextures"


def test_search_finds_options_by_description(table):
    hits = rtx_options.search("world up axis")
    assert [h["name"] for h in hits] == ["rtx.zUp"]


def test_search_is_case_insensitive_and_bounded(table):
    assert rtx_options.search("SCREEN-SPACE")
    assert len(rtx_options.search("rtx.", limit=2)) == 2


def test_sync_regenerates_the_table_from_markdown(monkeypatch, tmp_path):
    source = tmp_path / "RtxOptions.md"
    source.write_text(
        "# RTX Options\n"
        "| RTX Option | Type | Default Value | Min Value | Max Value | Description |\n"
        "| :-- | :-: | :-: | :-: | :-: | :-- |\n"
        "|rtx.zUp|bool|False||||\n"
        "|rtx.sceneScale|float|1|0.1|10|Centimetres per game unit\\.<br>"
        "Wrong values break light falloff\\.|\n",
        encoding="utf-8")
    monkeypatch.setattr(rtx_options, "DATA_FILE", tmp_path / "out.tsv")
    monkeypatch.setattr(rtx_options, "_cache", None)

    assert rtx_options.sync(source) == 2
    entry = rtx_options.lookup("rtx.sceneScale")
    # <br> and escapes from the generated markdown are normalized away.
    assert entry["description"] == ("Centimetres per game unit. "
                                   "Wrong values break light falloff.")
    assert entry["min"] == "0.1" and entry["max"] == "10"


def test_sync_rejects_a_source_with_no_options(monkeypatch, tmp_path):
    source = tmp_path / "empty.md"
    source.write_text("# RTX Options\n\nnothing here\n", encoding="utf-8")
    monkeypatch.setattr(rtx_options, "DATA_FILE", tmp_path / "out.tsv")
    with pytest.raises(ValueError):
        rtx_options.sync(source)


# ── the table that actually ships ─────────────────────────────────────────

def test_shipped_table_covers_the_options_the_playbook_relies_on():
    for name in ("rtx.uiTextures", "rtx.skyBoxTextures", "rtx.zUp",
                 "rtx.sceneScale", "rtx.fallbackLightMode",
                 "rtx.geometryGenerationHashRuleString", "rtx.useVertexCapture",
                 "rtx.uniqueObjectDistance", "rtx.debugView.debugViewIdx",
                 "rtx.antiCulling.object.enable", "rtx.captureShowMenuOnHotkey"):
        assert rtx_options.lookup(name), f"{name} missing from shipped table"


def test_shipped_table_covers_every_preset_option():
    from livetools.remixctl import PRESETS

    for preset in PRESETS.values():
        for key in preset["options"]:
            assert rtx_options.lookup(key), f"preset writes unknown option {key}"


def test_shipped_table_covers_every_hash_set_option():
    from livetools.remixctl import HASH_SET_OPTIONS

    for key in HASH_SET_OPTIONS:
        assert rtx_options.lookup(key), f"unknown hash-set option {key}"


# ── value validation ───────────────────────────────────────────────────────

@pytest.fixture
def typed(monkeypatch, tmp_path):
    data = tmp_path / "rtx_options.tsv"
    data.write_text(
        "rtx.zUp\tbool\tFalse\t\t\tZ is up.\n"
        "rtx.uniqueObjectDistance\tfloat\t300\t0\t\tCorrelation distance.\n"
        "rtx.skyProbeSide\tint\t1024\t16\t4096\tSky probe resolution.\n"
        "rtx.uiTextures\thash set\t\t\t\tUI textures.\n"
        "rtx.geometryGenerationHashRuleString\tstring\tpositions\t\t\tRule.\n",
        encoding="utf-8")
    monkeypatch.setattr(rtx_options, "DATA_FILE", data)
    monkeypatch.setattr(rtx_options, "_cache", None)
    return data


@pytest.mark.parametrize("value", ["True", "False"])
def test_bools_accept_the_spellings_the_runtime_parses(typed, value):
    assert rtx_options.validate_value("rtx.zUp", value) is None


@pytest.mark.parametrize("value", ["yes", "1", "true", "TRUE", ""])
def test_bools_reject_everything_else(typed, value):
    assert rtx_options.validate_value("rtx.zUp", value)


def test_numbers_must_parse(typed):
    assert rtx_options.validate_value("rtx.uniqueObjectDistance", "far")
    assert rtx_options.validate_value("rtx.uniqueObjectDistance", "300.5") is None


def test_ints_reject_fractions(typed):
    assert rtx_options.validate_value("rtx.skyProbeSide", "512.5")
    assert rtx_options.validate_value("rtx.skyProbeSide", "512") is None


def test_declared_bounds_are_enforced(typed):
    assert "min" in rtx_options.validate_value("rtx.uniqueObjectDistance", "-1")
    assert "max" in rtx_options.validate_value("rtx.skyProbeSide", "8192")
    assert rtx_options.validate_value("rtx.skyProbeSide", "4096") is None


def test_hash_sets_accept_comma_separated_hashes(typed):
    assert rtx_options.validate_value(
        "rtx.uiTextures", "0xA1B2C3D4, 0xdeadbeef") is None


def test_hash_sets_reject_non_hex(typed):
    problem = rtx_options.validate_value("rtx.uiTextures", "0xA1B2, notahash")
    assert "notahash" in problem


def test_free_form_types_are_not_second_guessed(typed):
    assert rtx_options.validate_value(
        "rtx.geometryGenerationHashRuleString", "indices,vertexlayout") is None


def test_unknown_options_are_not_value_checked(typed):
    assert rtx_options.validate_value("rtx.notAnOption", "whatever") is None


def test_every_preset_value_passes_its_own_validation():
    from livetools.remixctl import PRESETS

    for name, preset in PRESETS.items():
        for key, value in preset["options"].items():
            problem = rtx_options.validate_value(key, value)
            assert problem is None, f"preset {name}: {key} {problem}"
