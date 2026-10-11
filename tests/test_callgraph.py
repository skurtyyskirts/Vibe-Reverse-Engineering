"""Tests for callgraph.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retools"))

from common import Binary
from callgraph import _find_callers

class TestFindCallers:
    @patch("callgraph.scan_refs")
    @patch("callgraph.find_start")
    def test_find_callers_basic(self, mock_find_start, mock_scan_refs):
        # Setup mocks
        b = MagicMock(spec=Binary)
        b.exec_ranges.return_value = [(0x1000, 0x400, 0x100)]
        b.raw = b"fake_raw_data"

        target = 0x2000

        # scan_refs returns list of (kind, va)
        mock_scan_refs.return_value = [("call", 0x1010), ("call", 0x1020)]

        # mock find_start to return function start
        def mock_fs(binary, va):
            if va == 0x1010: return 0x1000
            if va == 0x1020: return 0x1000  # Both in same caller func
            return None
        mock_find_start.side_effect = mock_fs

        # Execute
        result = _find_callers(b, target)

        # Assert
        assert mock_scan_refs.call_count == 1
        mock_scan_refs.assert_called_with(b"fake_raw_data", 0x1000, 0x400, 0x100, 0x2000, "call")

        assert result == [0x1000] # deduplicated

    @patch("callgraph.scan_refs")
    @patch("callgraph.find_start")
    def test_find_callers_multiple_sections(self, mock_find_start, mock_scan_refs):
        # Setup mocks
        b = MagicMock(spec=Binary)
        b.exec_ranges.return_value = [
            (0x1000, 0x400, 0x100),
            (0x2000, 0x500, 0x100)
        ]
        b.raw = b"fake_raw_data"

        target = 0x3000

        # scan_refs side effect
        def mock_sr(raw, va_start, raw_off, size, tgt, kind):
            if va_start == 0x1000:
                return [("call", 0x1010)]
            elif va_start == 0x2000:
                return [("call", 0x2010), ("call", 0x2030)]
            return []
        mock_scan_refs.side_effect = mock_sr

        # mock find_start to return function start or None
        def mock_fs(binary, va):
            if va == 0x1010: return 0x1000
            if va == 0x2010: return 0x2000
            if va == 0x2030: return None # No func start found, falls back to va
            return None
        mock_find_start.side_effect = mock_fs

        # Execute
        result = _find_callers(b, target)

        # Assert
        assert mock_scan_refs.call_count == 2

        assert result == [0x1000, 0x2000, 0x2030] # sorted

    @patch("callgraph.scan_refs")
    def test_find_callers_no_refs(self, mock_scan_refs):
        # Setup mocks
        b = MagicMock(spec=Binary)
        b.exec_ranges.return_value = [(0x1000, 0x400, 0x100)]
        b.raw = b"fake_raw_data"

        target = 0x2000
        mock_scan_refs.return_value = []

        # Execute
        result = _find_callers(b, target)

        # Assert
        assert result == []
