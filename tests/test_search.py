import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retools"))

from search import find_imports, ImportEntry
from common import Binary

def test_find_imports_no_imports():
    b = MagicMock(spec=Binary)
    b.pe = MagicMock()
    del b.pe.DIRECTORY_ENTRY_IMPORT

    assert find_imports(b) == []

def test_find_imports_with_named_and_ordinal():
    b = MagicMock(spec=Binary)
    b.pe = MagicMock()

    entry1 = MagicMock()
    entry1.dll = b"kernel32.dll"

    imp1 = MagicMock()
    imp1.name = b"CreateFileA"
    imp1.ordinal = 1

    imp2 = MagicMock()
    imp2.name = b"CloseHandle"
    imp2.ordinal = 2

    entry1.imports = [imp1, imp2]

    entry2 = MagicMock()
    entry2.dll = b"user32.dll"

    imp3 = MagicMock()
    imp3.name = None
    imp3.ordinal = 42

    entry2.imports = [imp3]

    b.pe.DIRECTORY_ENTRY_IMPORT = [entry1, entry2]

    results = find_imports(b)

    assert len(results) == 3
    assert results[0] == ImportEntry(dll="kernel32.dll", name="CreateFileA")
    assert results[1] == ImportEntry(dll="kernel32.dll", name="CloseHandle")
    assert results[2] == ImportEntry(dll="user32.dll", name="ordinal_42")
