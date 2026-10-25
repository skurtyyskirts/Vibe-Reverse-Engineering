"""Regression coverage for the single-shell MSVC build path."""

import json
import subprocess

import pytest

from retools import asi_patcher


def _spec(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({
        "name": "LegendPatch",
        "arch": "x86",
        "patches": [],
    }))
    return path


def test_build_runs_setup_compile_and_link_in_one_batch(tmp_path, monkeypatch):
    spec_path = _spec(tmp_path)
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        batch = tmp_path / "_build_LegendPatch.bat"
        captured["batch"] = batch.read_text(encoding="mbcs")
        (tmp_path / "LegendPatch.asi").write_bytes(b"ASI")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(asi_patcher.subprocess, "run", run)
    asi_patcher.cmd_build(spec_path, tmp_path / "vcvarsall.bat")

    assert captured["command"] == ["cmd.exe", "/c", str(tmp_path / "_build_LegendPatch.bat")]
    assert captured["kwargs"] == {"capture_output": True, "text": True}
    assert 'call "' in captured["batch"]
    assert "cl.exe" in captured["batch"]
    assert "link.exe" in captured["batch"]
    assert "|| exit /b 1" in captured["batch"]
    assert "|| exit /b 2" in captured["batch"]
    assert "|| exit /b 3" in captured["batch"]
    assert not (tmp_path / "_build_LegendPatch.bat").exists()


def test_build_reports_the_failed_batch_stage_and_removes_batch(tmp_path, monkeypatch):
    spec_path = _spec(tmp_path)

    monkeypatch.setattr(
        asi_patcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 2, "compile output", "compile error"),
    )

    with pytest.raises(SystemExit, match="Compile failed \\(exit 2\\)"):
        asi_patcher.cmd_build(spec_path, tmp_path / "vcvarsall.bat")

    assert not (tmp_path / "_build_LegendPatch.bat").exists()
