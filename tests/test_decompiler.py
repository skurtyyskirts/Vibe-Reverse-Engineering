import sys
from unittest.mock import MagicMock
if 'r2pipe' not in sys.modules:
    sys.modules['r2pipe'] = MagicMock()
"""Tests for decompiler.py backend routing logic."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retools"))


class TestScriptModeImports:
    def test_load_types_import_resolves_in_script_mode(self, tmp_path):
        """`python retools/decompiler.py` puts only retools/ on sys.path[0];
        _load_types must still be able to import the kb parser."""
        import subprocess
        import textwrap

        retools = Path(__file__).resolve().parent.parent / "retools"
        driver = tmp_path / "driver.py"
        driver.write_text(textwrap.dedent(f"""
            import sys
            sys.path.insert(0, r"{retools}")   # simulate script-dir-only sys.path
            import decompiler

            class FakeR2:
                def cmd(self, c): pass

            decompiler._load_types(FakeR2(), "@ 0x401000 void Foo(void);")
            print("OK")
        """))
        proc = subprocess.run(
            [sys.executable, str(driver)],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        assert "OK" in proc.stdout, proc.stderr


class TestBackendRouting:
    def test_auto_no_project_uses_r2(self):
        """--backend auto without --project should use r2ghidra."""
        from decompiler import decompile
        with patch("decompiler._find_r2_bin", return_value=None):
            with pytest.raises(FileNotFoundError, match="radare2 not found"):
                decompile("fake.exe", 0x401000, backend="auto")

    def test_auto_with_project_no_ghidra_falls_through(self, tmp_path):
        """--backend auto with --project but no Ghidra project falls through to r2."""
        mock_backend = MagicMock()
        mock_backend.is_analyzed.return_value = False

        with patch.dict(sys.modules, {"retools.pyghidra_backend": mock_backend}):
            from importlib import reload
            import decompiler
            reload(decompiler)
            with patch("decompiler._find_r2_bin", return_value=None):
                with pytest.raises(FileNotFoundError, match="radare2 not found"):
                    decompiler.decompile("fake.exe", 0x401000, backend="auto", project_dir=str(tmp_path))

    def test_ghidra_backend_without_project_errors(self):
        """--backend ghidra without --project returns error."""
        from decompiler import decompile
        result = decompile("fake.exe", 0x401000, backend="ghidra")
        assert "[error]" in result
        assert "--project required" in result

    def test_ghidra_backend_routes_to_pyghidra(self, tmp_path):
        """--backend ghidra with --project routes to pyghidra_backend."""
        mock_backend = MagicMock()
        mock_backend.decompile.return_value = "void func() {}"
        mock_backend.is_analyzed.return_value = True

        with patch.dict(sys.modules, {"retools.pyghidra_backend": mock_backend}):
            from importlib import reload
            import decompiler
            reload(decompiler)
            result = decompiler.decompile("fake.exe", 0x401000, backend="ghidra", project_dir=str(tmp_path))

        mock_backend.decompile.assert_called_once()
        assert result == "void func() {}"


class FakeR2:
    def __init__(self):
        self.cmds = []

    def cmd(self, c):
        self.cmds.append(c)
        return ""


class TestLoadTypesOrdering:
    def test_typedefs_emitted_before_references(self, tmp_path):
        """td must be sent to r2 before any tl/afs that could reference the type."""
        kb_path = tmp_path / "kb.h"
        kb_path.write_text(
            "struct Foo { int x; };\n"
            "$ 0x7C5548 Foo* g_mainObject\n"
            "@ 0x401000 void __cdecl ProcessInput(int key);\n"
        )

        from decompiler import _load_types
        r2 = FakeR2()
        _load_types(r2, str(kb_path))

        td_idx = next(i for i, c in enumerate(r2.cmds) if c.startswith("td "))
        tl_idx = next(i for i, c in enumerate(r2.cmds) if c.startswith("tl "))
        afs_idx = next(i for i, c in enumerate(r2.cmds) if c.startswith("afs "))

        assert td_idx < tl_idx
        assert td_idx < afs_idx

class TestDecompilerR2:
    def setup_method(self):
        self.mock_find_r2 = patch("decompiler._find_r2_bin", return_value="/fake/r2").start()
        self.mock_ensure_path = patch("decompiler._ensure_r2_in_path").start()
        self.mock_r2pipe_open = patch("r2pipe.open").start()
        self.mock_sleigh_home = patch("decompiler._find_sleigh_home", return_value=None).start()
        self.mock_load_types = patch("decompiler._load_types").start()

        self.mock_r2 = MagicMock()
        self.mock_r2pipe_open.return_value = self.mock_r2

    def teardown_method(self):
        patch.stopall()

    def test_auto_backend_pdg_success(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "int main() {}"
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        result = decompile("fake.exe", 0x401000)
        assert result == "int main() {}"
        self.mock_r2.cmd.assert_any_call("pdg @ 0x401000")

    def test_auto_backend_pdg_install_fallback(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "Please install r2ghidra"
            if arg.startswith("pdc @"): return "int main_pdc() {}"
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        result = decompile("fake.exe", 0x401000)
        assert result == "int main_pdc() {}"
        self.mock_r2.cmd.assert_any_call("pdg @ 0x401000")
        self.mock_r2.cmd.assert_any_call("pdc @ 0x401000")

    def test_auto_backend_both_fail(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "install"
            if arg.startswith("pdc @"): return "  "
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        result = decompile("fake.exe", 0x401000)
        assert "[error]" in result
        assert "No decompiler backend produced output" in result

    def test_explicit_backend(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdd @"): return "JS decompiler out"
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        result = decompile("fake.exe", 0x401000, backend="pdd")
        assert result == "JS decompiler out"
        self.mock_r2.cmd.assert_any_call("pdd @ 0x401000")

    def test_unknown_backend(self):
        from decompiler import decompile
        result = decompile("fake.exe", 0x401000, backend="invalid_backend")
        assert "[error]" in result
        assert "Unknown backend" in result

    def test_explicit_backend_no_output(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdc @"): return "   "
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        result = decompile("fake.exe", 0x401000, backend="pdc")
        assert "[error]" in result
        assert "pdc produced no output" in result

    def test_full_analysis(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "out"
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        decompile("fake.exe", 0x401000, full_analysis=True)
        self.mock_r2.cmd.assert_any_call("aaa")

    def test_no_full_analysis(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "out"
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        decompile("fake.exe", 0x401000, full_analysis=False)
        self.mock_r2.cmd.assert_any_call("af @ 0x401000")

    def test_sleigh_home_set(self):
        from decompiler import decompile
        patch.stopall() # Stop default mock

        self.mock_find_r2 = patch("decompiler._find_r2_bin", return_value="/fake/r2").start()
        self.mock_ensure_path = patch("decompiler._ensure_r2_in_path").start()
        self.mock_r2pipe_open = patch("r2pipe.open").start()
        self.mock_load_types = patch("decompiler._load_types").start()
        self.mock_sleigh_home = patch("decompiler._find_sleigh_home", return_value="/custom/sleigh/path").start()

        mock_r2_local = MagicMock()
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "out"
            return ""
        mock_r2_local.cmd.side_effect = mock_cmd
        self.mock_r2pipe_open.return_value = mock_r2_local

        decompile("fake.exe", 0x401000)
        mock_r2_local.cmd.assert_any_call("e r2ghidra.sleighhome=/custom/sleigh/path")

    def test_types_loaded(self):
        from decompiler import decompile
        def mock_cmd(arg):
            if arg.startswith("pdg @"): return "out"
            return ""
        self.mock_r2.cmd.side_effect = mock_cmd

        decompile("fake.exe", 0x401000, types="fake_types.h")
        self.mock_load_types.assert_called_once_with(self.mock_r2, "fake_types.h")
