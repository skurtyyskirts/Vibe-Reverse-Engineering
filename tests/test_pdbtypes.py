"""Tests for retools/pdbtypes.py -- TPI parsing and C formatting on a synthetic type stream."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "retools"))

from pdbtypes import TypeTable  # noqa: E402

T_CHAR, T_INT4, T_UINT4 = 0x70, 0x74, 0x75


def _rec(kind: int, payload: bytes) -> bytes:
    return struct.pack("<HH", len(payload) + 2, kind) + payload


def _member(type_index: int, offset: int, name: str) -> bytes:
    body = struct.pack("<HHIH", 0x150D, 3, type_index, offset) + name.encode() + b"\0"
    pad = (-len(body)) % 4
    return body + bytes(0xF0 | (pad - i) for i in range(pad))


def _tpi(records: list[bytes]) -> bytes:
    body = b"".join(records)
    header = struct.pack("<IIIII", 20040203, 56, 0x1000, 0x1000 + len(records), len(body))
    return header + bytes(56 - len(header)) + body


def _table() -> TypeTable:
    records = [
        # 0x1000 field list: int x @0, char c[8] @4
        _rec(0x1203, _member(T_INT4, 0, "x") + _member(0x1001, 4, "c")),
        # 0x1001 char[8]
        _rec(0x1503, struct.pack("<IIH", T_CHAR, T_UINT4, 8) + b"\0"),
        # 0x1002 struct Foo (definition, 12 bytes)
        _rec(0x1505, struct.pack("<HHIIIH", 2, 0, 0x1000, 0, 0, 12) + b"Foo\0"),
        # 0x1003 struct Foo (forward reference)
        _rec(0x1505, struct.pack("<HHIIIH", 0, 0x80, 0, 0, 0, 0) + b"Foo\0"),
        # 0x1004 Foo* (32-bit near pointer, size 4)
        _rec(0x1002, struct.pack("<II", 0x1003, 0x0A | (4 << 13))),
        # 0x1005 field list: A = 0, B = 0x8000 (LF_USHORT numeric leaf)
        _rec(0x1203, struct.pack("<HHH", 0x1502, 3, 0) + b"A\0" + b"\xf1"
             + struct.pack("<HHHH", 0x1502, 3, 0x8002, 0x8000) + b"B\0"),
        # 0x1006 enum E : int
        _rec(0x1507, struct.pack("<HHII", 2, 0, T_INT4, 0x1005) + b"E\0"),
    ]
    return TypeTable(_tpi(records))


def test_struct_members_and_offsets():
    text = _table().format_udt(0x1002)
    assert "struct Foo {  // sizeof 0xC (12)" in text
    assert "int x;  // +0x0000" in text
    assert "char c[8];  // +0x0004" in text


def test_forward_reference_resolves_to_definition():
    types = _table()
    assert types.resolve(0x1003) == 0x1002
    assert types.size(0x1003) == 12
    assert types.lookup("Foo") == [0x1002]


def test_pointer_declarator():
    assert _table().decl(0x1004, "p") == "struct Foo *p"


def test_enum_values_with_numeric_leaf():
    text = _table().format_enum(0x1006)
    assert "A = 0," in text
    assert "B = 32768," in text
