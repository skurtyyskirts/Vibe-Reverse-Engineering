#!/usr/bin/env python3
"""Dump types and symbols from an MSVC PDB (MSF 7.00) with no external tools.

Reads the TPI stream (struct/union/class/enum layouts) and the DBI module and
global symbol streams (procedures with sizes, data symbols, publics). Struct
dumps are C with ``// +0xOFF`` member offsets, explicit padding and the total
size, so they can go straight into a kb.h.

Usage:
    python -m retools.pdbtypes <pdb> struct <Name> [-r DEPTH] [--methods] [--nested]
    python -m retools.pdbtypes <pdb> enum <Name>
    python -m retools.pdbtypes <pdb> find <regex> [--members] [--kind struct|union|enum]
    python -m retools.pdbtypes <pdb> procs <regex> [--neighbours N] [--publics]
    python -m retools.pdbtypes <pdb> globals <regex>

<Name> is an exact type name; a unique ``Scope::Name`` suffix match is accepted.
``-r DEPTH`` also dumps every struct reachable from members (through pointers,
arrays and modifiers) up to DEPTH levels, each one once. Addresses print as
RVA and as VA under ``--base`` (default 0x400000). Regexes are Python ``re``,
case-insensitive.
"""

from __future__ import annotations

import argparse
import bisect
import re
import struct
import sys
from dataclasses import dataclass

# --- MSF container -----------------------------------------------------------

_MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\0\0\0"


class Msf:
    """Block-mapped stream container that every PDB is built on."""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        if not self.data.startswith(_MSF_MAGIC):
            raise ValueError(f"{path}: not an MSF 7.00 PDB")
        bs, _fpm, _nblocks, dir_bytes, _unk, map_block = struct.unpack_from("<6I", self.data, 32)
        self.block_size = bs
        n_dir_blocks = -(-dir_bytes // bs)
        dir_block_ids = struct.unpack_from(f"<{n_dir_blocks}I", self.data, map_block * bs)
        directory = b"".join(self._block(b) for b in dir_block_ids)[:dir_bytes]
        n_streams = struct.unpack_from("<I", directory, 0)[0]
        sizes = struct.unpack_from(f"<{n_streams}I", directory, 4)
        pos = 4 + 4 * n_streams
        self.streams: list[tuple[int, tuple[int, ...]]] = []
        for size in sizes:
            if size == 0xFFFFFFFF:
                self.streams.append((0, ()))
                continue
            n = -(-size // bs)
            self.streams.append((size, struct.unpack_from(f"<{n}I", directory, pos)))
            pos += 4 * n

    def _block(self, idx: int) -> bytes:
        return self.data[idx * self.block_size:(idx + 1) * self.block_size]

    def stream(self, idx: int) -> bytes:
        if idx >= len(self.streams):
            return b""
        size, blocks = self.streams[idx]
        return b"".join(self._block(b) for b in blocks)[:size]


# --- primitive readers -------------------------------------------------------

_NUMERIC = {0x8000: "<b", 0x8001: "<h", 0x8002: "<H", 0x8003: "<i", 0x8004: "<I",
            0x8005: "<f", 0x8006: "<d", 0x8009: "<q", 0x800A: "<Q"}


def read_numeric(buf: bytes, off: int) -> tuple[int | float, int]:
    """Read a CodeView numeric leaf (LF_CHAR..LF_UQUADWORD or an inline u16)."""
    leaf = struct.unpack_from("<H", buf, off)[0]
    if leaf < 0x8000:
        return leaf, off + 2
    fmt = _NUMERIC.get(leaf)
    if fmt is None:
        raise ValueError(f"unsupported numeric leaf 0x{leaf:04X}")
    return struct.unpack_from(fmt, buf, off + 2)[0], off + 2 + struct.calcsize(fmt)


def read_cstr(buf: bytes, off: int) -> tuple[str, int]:
    end = buf.index(b"\0", off)
    return buf[off:end].decode("latin-1"), end + 1


# --- type table --------------------------------------------------------------

LF_MODIFIER, LF_POINTER, LF_PROCEDURE, LF_MFUNCTION = 0x1001, 0x1002, 0x1008, 0x1009
LF_ARGLIST, LF_FIELDLIST, LF_BITFIELD, LF_METHODLIST = 0x1201, 0x1203, 0x1205, 0x1206
LF_ARRAY, LF_CLASS, LF_STRUCTURE, LF_UNION, LF_ENUM = 0x1503, 0x1504, 0x1505, 0x1506, 0x1507
LF_INTERFACE = 0x1519
LF_BCLASS, LF_VBCLASS, LF_IVBCLASS, LF_INDEX, LF_VFUNCTAB = 0x1400, 0x1401, 0x1402, 0x1404, 0x1409
LF_ENUMERATE, LF_MEMBER, LF_STMEMBER, LF_METHOD = 0x1502, 0x150D, 0x150E, 0x150F
LF_NESTTYPE, LF_ONEMETHOD, LF_VFUNCOFF = 0x1510, 0x1511, 0x140C
_UDT_KINDS = (LF_CLASS, LF_STRUCTURE, LF_INTERFACE, LF_UNION, LF_ENUM)
_KEYWORD = {LF_CLASS: "struct", LF_STRUCTURE: "struct", LF_INTERFACE: "struct",
            LF_UNION: "union", LF_ENUM: "enum"}
_PROP_FWDREF, _PROP_UNIQUE = 0x80, 0x200

_BASE = {0x00: ("<notype>", 0), 0x03: ("void", 0), 0x08: ("HRESULT", 4),
         0x10: ("signed char", 1), 0x11: ("short", 2), 0x12: ("long", 4), 0x13: ("__int64", 8),
         0x20: ("unsigned char", 1), 0x21: ("unsigned short", 2), 0x22: ("unsigned long", 4),
         0x23: ("unsigned __int64", 8), 0x30: ("bool", 1), 0x31: ("short", 2), 0x32: ("int", 4),
         0x40: ("float", 4), 0x41: ("double", 8), 0x42: ("long double", 10),
         0x68: ("char", 1), 0x69: ("unsigned char", 1), 0x70: ("char", 1), 0x71: ("wchar_t", 2),
         0x72: ("short", 2), 0x73: ("unsigned short", 2), 0x74: ("int", 4), 0x75: ("unsigned int", 4),
         0x76: ("__int64", 8), 0x77: ("unsigned __int64", 8), 0x7A: ("char16_t", 2),
         0x7B: ("char32_t", 4)}
_CALLCONV = {0: "__cdecl", 4: "__fastcall", 7: "__stdcall", 0xB: "__thiscall", 0x18: "__vectorcall"}


@dataclass
class Udt:
    ti: int
    kind: int
    name: str
    size: int
    fields: int
    utype: int = 0  # enum underlying type


@dataclass
class Field:
    kind: int
    name: str = ""
    type: int = 0
    offset: int = 0
    value: int = 0
    attr: int = 0


class TypeTable:
    """TPI records indexed by type index, plus name lookup and C formatting."""

    def __init__(self, data: bytes):
        hdr_size, self.begin, self.end = struct.unpack_from("<III", data, 4)
        self.recs: list[tuple[int, bytes]] = []
        pos = hdr_size
        for _ in range(self.end - self.begin):
            reclen, kind = struct.unpack_from("<HH", data, pos)
            self.recs.append((kind, data[pos + 4:pos + 2 + reclen]))
            pos += 2 + reclen
        self.defs: dict[str, list[int]] = {}
        for i, (kind, _) in enumerate(self.recs):
            if kind in _UDT_KINDS:
                u = self.udt(self.begin + i)
                if not self._prop(self.begin + i) & _PROP_FWDREF:
                    self.defs.setdefault(u.name, []).append(u.ti)

    def rec(self, ti: int) -> tuple[int, bytes]:
        return self.recs[ti - self.begin]

    def _prop(self, ti: int) -> int:
        return struct.unpack_from("<H", self.rec(ti)[1], 2)[0]

    def udt(self, ti: int) -> Udt:
        kind, p = self.rec(ti)
        if kind == LF_ENUM:
            _cnt, _prop, utype, fl = struct.unpack_from("<HHII", p, 0)
            name, _ = read_cstr(p, 12)
            return Udt(ti, kind, name, 0, fl, utype)
        if kind == LF_UNION:
            _cnt, _prop, fl = struct.unpack_from("<HHI", p, 0)
            size, off = read_numeric(p, 8)
        else:
            _cnt, _prop, fl, _derived, _vshape = struct.unpack_from("<HHIII", p, 0)
            size, off = read_numeric(p, 16)
        name, _ = read_cstr(p, off)
        return Udt(ti, kind, name, int(size), fl)

    def resolve(self, ti: int) -> int:
        """Map a forward-referenced UDT to its definition (by name), else return ti."""
        if ti < self.begin or self.rec(ti)[0] not in _UDT_KINDS or not self._prop(ti) & _PROP_FWDREF:
            return ti
        cands = self.defs.get(self.udt(ti).name, [])
        return cands[0] if cands else ti

    def lookup(self, name: str) -> list[int]:
        if name in self.defs:
            return self.defs[name][:1]
        return [tis[0] for n, tis in self.defs.items() if n.endswith("::" + name)]

    # ---- field lists ----

    def fields(self, fl_ti: int) -> list[Field]:
        out: list[Field] = []
        while fl_ti:
            kind, p = self.rec(fl_ti)
            if kind != LF_FIELDLIST:
                break
            fl_ti = 0
            pos = 0
            while pos < len(p):
                sub = struct.unpack_from("<H", p, pos)[0]
                pos += 2
                f = Field(sub)
                if sub == LF_MEMBER:
                    f.attr, f.type = struct.unpack_from("<HI", p, pos)
                    f.offset, pos = read_numeric(p, pos + 6)
                    f.name, pos = read_cstr(p, pos)
                elif sub == LF_STMEMBER:
                    f.attr, f.type = struct.unpack_from("<HI", p, pos)
                    f.name, pos = read_cstr(p, pos + 6)
                elif sub == LF_ENUMERATE:
                    f.attr = struct.unpack_from("<H", p, pos)[0]
                    f.value, pos = read_numeric(p, pos + 2)
                    f.name, pos = read_cstr(p, pos)
                elif sub == LF_BCLASS:
                    f.attr, f.type = struct.unpack_from("<HI", p, pos)
                    f.offset, pos = read_numeric(p, pos + 6)
                elif sub in (LF_VBCLASS, LF_IVBCLASS):
                    f.attr, f.type, _vbp = struct.unpack_from("<HII", p, pos)
                    _, pos = read_numeric(p, pos + 10)
                    _, pos = read_numeric(p, pos)
                elif sub == LF_INDEX:
                    fl_ti = struct.unpack_from("<I", p, pos + 2)[0]
                    pos += 6
                elif sub == LF_VFUNCTAB:
                    f.type = struct.unpack_from("<I", p, pos + 2)[0]
                    pos += 6
                elif sub == LF_VFUNCOFF:
                    pos += 10
                elif sub == LF_METHOD:
                    _cnt, f.type = struct.unpack_from("<HI", p, pos)
                    f.name, pos = read_cstr(p, pos + 6)
                elif sub == LF_NESTTYPE:
                    f.type = struct.unpack_from("<I", p, pos + 2)[0]
                    f.name, pos = read_cstr(p, pos + 6)
                elif sub == LF_ONEMETHOD:
                    f.attr, f.type = struct.unpack_from("<HI", p, pos)
                    pos += 6
                    if (f.attr >> 2) & 7 in (4, 6):  # introducing virtual: vbaseoff follows
                        pos += 4
                    f.name, pos = read_cstr(p, pos)
                else:
                    raise ValueError(f"unsupported field leaf 0x{sub:04X} in fieldlist 0x{fl_ti:X}")
                out.append(f)
                while pos < len(p) and p[pos] >= 0xF0:
                    pos += 1
        return out

    def methods(self, mlist_ti: int) -> list[int]:
        _kind, p = self.rec(mlist_ti)
        out, pos = [], 0
        while pos < len(p):
            attr, _pad, ti = struct.unpack_from("<HHI", p, pos)
            pos += 8 + (4 if (attr >> 2) & 7 in (4, 6) else 0)
            out.append(ti)
        return out

    # ---- naming / sizing ----

    def size(self, ti: int) -> int:
        if ti < self.begin:
            if (ti >> 8) & 0xF:
                return 8 if (ti >> 8) & 0xF == 6 else 4
            return _BASE.get(ti & 0xFF, ("?", 0))[1]
        kind, p = self.rec(ti)
        if kind == LF_MODIFIER or kind == LF_BITFIELD:
            return self.size(struct.unpack_from("<I", p, 0)[0])
        if kind == LF_POINTER:
            return (struct.unpack_from("<I", p, 4)[0] >> 13) & 0x3F or 4
        if kind == LF_ARRAY:
            return int(read_numeric(p, 8)[0])
        if kind in _UDT_KINDS:
            u = self.udt(self.resolve(ti))
            return self.size(u.utype) if kind == LF_ENUM else u.size
        return 0

    def _args(self, arglist: int) -> str:
        _kind, p = self.rec(arglist)
        n = struct.unpack_from("<I", p, 0)[0]
        args = [self.decl(t, "") for t in struct.unpack_from(f"<{n}I", p, 4)]
        return ", ".join(args) if args else "void"

    def decl(self, ti: int, name: str) -> str:
        """C declarator for type ti naming `name` (may be empty)."""
        sep = (" " + name) if name else ""
        if ti < self.begin:
            base = _BASE.get(ti & 0xFF, (f"<base 0x{ti:X}>", 0))[0]
            if (ti >> 8) & 0xF:
                return f"{base} *{name}"
            return base + sep
        kind, p = self.rec(ti)
        if kind == LF_MODIFIER:
            target, attr = struct.unpack_from("<IH", p, 0)
            quals = " ".join(q for bit, q in ((1, "const"), (2, "volatile")) if attr & bit)
            if target >= self.begin and self.rec(target)[0] == LF_POINTER:
                return self.decl(target, f"{quals} {name}".strip())
            return f"{quals} {self.decl(target, name)}" if quals else self.decl(target, name)
        if kind == LF_POINTER:
            target, attr = struct.unpack_from("<II", p, 0)
            mode = (attr >> 5) & 7
            star = {1: "&", 4: "&&"}.get(mode, "*")
            if mode in (2, 3):
                star = self.name(struct.unpack_from("<I", p, 8)[0]) + "::*"
            if attr & 0x400:
                star += "const "
            inner = star + name
            if target >= self.begin and self.rec(target)[0] in (LF_ARRAY, LF_PROCEDURE, LF_MFUNCTION):
                inner = f"({inner})"
            return self.decl(target, inner)
        if kind == LF_ARRAY:
            elem = struct.unpack_from("<I", p, 0)[0]
            total = int(read_numeric(p, 8)[0])
            esz = self.size(elem)
            return self.decl(elem, f"{name}[{total // esz if esz else '?'}]")
        if kind == LF_PROCEDURE:
            rv, cc, _fa, _n, arglist = struct.unpack_from("<IBBHI", p, 0)
            if name.startswith("(") and cc in _CALLCONV:
                name = f"({_CALLCONV[cc]} {name[1:]}"
            return self.decl(rv, f"{name}({self._args(arglist)})")
        if kind == LF_MFUNCTION:
            rv, cls, _this, cc, _fa, _n, arglist = struct.unpack_from("<IIIBBHI", p, 0)
            return self.decl(rv, f"{name}({self._args(arglist)})")
        if kind == LF_BITFIELD:
            base, length = struct.unpack_from("<IB", p, 0)
            return f"{self.decl(base, name)} : {length}"
        if kind in _UDT_KINDS:
            u = self.udt(ti)
            return f"{_KEYWORD[kind]} {u.name}{sep}"
        return f"<leaf 0x{kind:04X}>{sep}"

    def name(self, ti: int) -> str:
        return self.decl(ti, "")

    def bitfield(self, ti: int) -> tuple[int, int] | None:
        if ti >= self.begin and self.rec(ti)[0] == LF_BITFIELD:
            _base, length, position = struct.unpack_from("<IBB", self.rec(ti)[1], 0)
            return length, position
        return None

    def udt_targets(self, ti: int) -> list[int]:
        """UDT definitions a member of type ti refers to (through ptr/array/modifier)."""
        while ti >= self.begin:
            kind, p = self.rec(ti)
            if kind in (LF_MODIFIER, LF_POINTER, LF_ARRAY, LF_BITFIELD):
                ti = struct.unpack_from("<I", p, 0)[0]
            elif kind in _UDT_KINDS:
                return [self.resolve(ti)]
            else:
                return []
        return []

    # ---- formatting ----

    def format_enum(self, ti: int) -> str:
        u = self.udt(self.resolve(ti))
        lines = [f"enum {u.name} {{  // underlying {self.name(u.utype)}"]
        for f in self.fields(u.fields):
            if f.kind == LF_ENUMERATE:
                lines.append(f"    {f.name} = {f.value},  // 0x{f.value & 0xFFFFFFFF:X}")
        lines.append("};")
        return "\n".join(lines)

    def format_udt(self, ti: int, methods: bool = False, nested: bool = False) -> str:
        u = self.udt(self.resolve(ti))
        if u.kind == LF_ENUM:
            return self.format_enum(ti)
        kw = _KEYWORD[u.kind]
        lines = [f"{kw} {u.name} {{  // sizeof 0x{u.size:X} ({u.size})"]
        if self._prop(u.ti) & _PROP_FWDREF:
            return f"{kw} {u.name};  // forward declaration only, no definition in this PDB"
        end = 0
        group_off = -1
        for f in self.fields(u.fields):
            if f.kind == LF_VFUNCTAB:
                lines.append("    void **__vfptr;  // +0x0000")
                end = max(end, 4)
            elif f.kind == LF_BCLASS:
                bname = self.udt(self.resolve(f.type)).name
                member = "__base_" + re.sub(r"\W", "_", bname)
                lines.append(f"    struct {bname} {member};  // +0x{f.offset:04X} base class")
                end = max(end, f.offset + self.size(f.type))
            elif f.kind == LF_MEMBER:
                bf = self.bitfield(f.type)
                note = ""
                if bf:
                    note = f" bit {bf[1]}"
                    end = max(end, f.offset + self.size(f.type))
                elif u.kind == LF_UNION:
                    end = max(end, self.size(f.type))
                elif f.offset < end:
                    note = " (overlaps: union member)" if f.offset == group_off else " (overlaps)"
                else:
                    if f.offset > end:
                        lines.append(f"    char _pad_{end:04X}[0x{f.offset - end:X}];  // +0x{end:04X}")
                    group_off = f.offset
                    end = f.offset + self.size(f.type)
                lines.append(f"    {self.decl(f.type, f.name)};  // +0x{f.offset:04X}{note}")
            elif f.kind == LF_STMEMBER:
                lines.append(f"    // static {self.decl(f.type, f.name)};")
            elif nested and f.kind == LF_NESTTYPE:
                lines.append(f"    // nested type {f.name} = {self.name(f.type)}")
            elif methods and f.kind == LF_ONEMETHOD:
                lines.append(f"    // method {self.decl(f.type, f.name)}")
            elif methods and f.kind == LF_METHOD:
                for mti in self.methods(f.type):
                    lines.append(f"    // method {self.decl(mti, f.name)}")
        if u.kind != LF_UNION and u.size > end:
            lines.append(f"    char _pad_{end:04X}[0x{u.size - end:X}];  // +0x{end:04X}")
        lines.append("};")
        return "\n".join(lines)

    def members(self, ti: int) -> list[Field]:
        return [f for f in self.fields(self.udt(self.resolve(ti)).fields)
                if f.kind in (LF_MEMBER, LF_STMEMBER)]

    def dump_tree(self, ti: int, depth: int, methods: bool = False, nested: bool = False) -> str:
        """Format ti and every UDT reachable from its members up to `depth` levels."""
        seen: set[str] = set()
        order: list[int] = []
        frontier = [self.resolve(ti)]
        for level in range(depth + 1):
            nxt = []
            for t in frontier:
                nm = self.udt(t).name
                if nm in seen:
                    continue
                seen.add(nm)
                order.append(t)
                if level < depth and self.udt(t).kind != LF_ENUM:
                    for f in self.fields(self.udt(t).fields):
                        if f.kind in (LF_MEMBER, LF_BCLASS):
                            nxt.extend(self.udt_targets(f.type))
            frontier = nxt
        return "\n\n".join(self.format_udt(t, methods, nested) for t in order)


# --- DBI / symbols -----------------------------------------------------------

S_PUB32, S_GDATA32, S_LDATA32, S_GPROC32, S_LPROC32 = 0x110E, 0x110D, 0x110C, 0x1110, 0x110F
S_GPROC32_ID, S_LPROC32_ID, S_UDT = 0x1147, 0x1146, 0x1108


@dataclass
class Symbol:
    kind: int
    name: str
    seg: int
    off: int
    size: int = 0
    type: int = 0
    module: str = ""


def iter_symbols(buf: bytes, start: int = 0):
    pos = start
    while pos + 4 <= len(buf):
        reclen, kind = struct.unpack_from("<HH", buf, pos)
        if reclen < 2:
            break
        yield kind, buf[pos + 4:pos + 2 + reclen]
        pos += 2 + reclen


class Pdb:
    """TPI types plus DBI module/global symbols and section headers."""

    def __init__(self, path: str):
        self.msf = Msf(path)
        self.types = TypeTable(self.msf.stream(2))
        self._dbi = self.msf.stream(3)
        self._sym_stream = struct.unpack_from("<H", self._dbi, 20)[0]
        (self._mod_size, self._sc_size, self._smap_size, self._src_size, self._tsm_size,
         _mfc, self._dbg_size, self._ec_size) = struct.unpack_from("<iiiiiIii", self._dbi, 24)
        self.sections = self._read_sections()
        self._procs: list[Symbol] | None = None
        self._data: list[Symbol] | None = None

    def _read_sections(self) -> list[tuple[str, int]]:
        dbg_off = 64 + self._mod_size + self._sc_size + self._smap_size + self._src_size \
            + self._tsm_size + self._ec_size
        if self._dbg_size < 12:
            return []
        sec_stream = struct.unpack_from("<H", self._dbi, dbg_off + 10)[0]
        if sec_stream == 0xFFFF:
            return []
        raw = self.msf.stream(sec_stream)
        return [(raw[i:i + 8].rstrip(b"\0").decode("latin-1"), struct.unpack_from("<I", raw, i + 12)[0])
                for i in range(0, len(raw) - 39, 40)]

    def rva(self, seg: int, off: int) -> int | None:
        if 0 < seg <= len(self.sections):
            return self.sections[seg - 1][1] + off
        return None

    def _module_infos(self) -> list[tuple[str, int, int]]:
        """(module name, symbol stream, symbol byte size) in module-index order."""
        out = []
        pos, end = 64, 64 + self._mod_size
        while pos < end:
            stream, sym_bytes = struct.unpack_from("<HI", self._dbi, pos + 34)
            name, p2 = read_cstr(self._dbi, pos + 64)
            _obj, p2 = read_cstr(self._dbi, p2)
            pos = (p2 + 3) & ~3
            out.append((name, stream, sym_bytes))
        return out

    def modules(self):
        """Yield (module name, symbol stream bytes) for modules that have symbols."""
        for name, stream, sym_bytes in self._module_infos():
            if stream != 0xFFFF:
                yield name, self.msf.stream(stream)[:sym_bytes]

    def contributions(self) -> list[tuple[int, int, int, int]]:
        """Section contributions as sorted (seg, off, size, module index)."""
        base = 64 + self._mod_size
        ver = struct.unpack_from("<I", self._dbi, base)[0]
        step = 32 if ver == 0xEFFE0000 + 20140516 else 28
        out = []
        for pos in range(base + 4, base + self._sc_size - step + 1, step):
            seg, off, size, _chars, mod = struct.unpack_from("<HxxiiIH", self._dbi, pos)
            out.append((seg, off, size, mod))
        return sorted(out)

    def _owner_lookup(self):
        """Return f(seg, off) -> name of the module whose section contribution holds that address."""
        names = [n for n, _, _ in self._module_infos()]
        contribs = self.contributions()
        keys = [(c[0], c[1]) for c in contribs]

        def owner(seg: int, off: int) -> str:
            i = bisect.bisect_right(keys, (seg, off)) - 1
            if i >= 0:
                cseg, coff, size, mod = contribs[i]
                if cseg == seg and coff <= off < coff + size and mod < len(names):
                    return names[mod]
            return ""
        return owner

    def _scan(self):
        procs, data = [], []
        for mod, buf in self.modules():
            for kind, p in iter_symbols(buf, 4):
                if kind in (S_GPROC32, S_LPROC32, S_GPROC32_ID, S_LPROC32_ID):
                    size, = struct.unpack_from("<I", p, 12)
                    typ, off, seg = struct.unpack_from("<IIH", p, 24)
                    name, _ = read_cstr(p, 35)
                    procs.append(Symbol(kind, name, seg, off, size, typ if kind in (S_GPROC32, S_LPROC32) else 0, mod))
                elif kind in (S_GDATA32, S_LDATA32):
                    typ, off, seg = struct.unpack_from("<IIH", p, 0)
                    name, _ = read_cstr(p, 10)
                    data.append(Symbol(kind, name, seg, off, 0, typ, mod))
        for kind, p in iter_symbols(self.msf.stream(self._sym_stream)):
            if kind in (S_GDATA32, S_LDATA32):
                typ, off, seg = struct.unpack_from("<IIH", p, 0)
                name, _ = read_cstr(p, 10)
                data.append(Symbol(kind, name, seg, off, 0, typ))
            elif kind == S_PUB32:
                flags, off, seg = struct.unpack_from("<IIH", p, 0)
                name, _ = read_cstr(p, 10)
                if flags & 2:
                    procs.append(Symbol(kind, name, seg, off))
        owner = self._owner_lookup()
        dedup: dict[tuple[str, int, int], Symbol] = {}
        for s in data:
            s.module = s.module or owner(s.seg, s.off)
            dedup.setdefault((s.name, s.seg, s.off), s)
        self._data = sorted(dedup.values(), key=lambda s: (s.seg, s.off))
        covered = {(s.seg, s.off) for s in procs if s.kind != S_PUB32}
        self._all_procs = sorted(procs, key=lambda s: (s.seg, s.off))
        self._procs = [s for s in self._all_procs if s.kind != S_PUB32 or (s.seg, s.off) not in covered]

    def procs(self, publics: bool = False) -> list[Symbol]:
        if self._procs is None:
            self._scan()
        return [s for s in self._procs if publics or s.kind != S_PUB32]

    def data(self) -> list[Symbol]:
        if self._data is None:
            self._scan()
        return self._data


# --- CLI ---------------------------------------------------------------------

def _pick(types: TypeTable, name: str) -> int:
    tis = types.lookup(name)
    if not tis:
        sys.exit(f"no type named {name!r} (try: find {name})")
    if len(tis) > 1:
        names = ", ".join(types.udt(t).name for t in tis)
        sys.exit(f"{name!r} is ambiguous: {names}")
    return tis[0]


def _short_module(path: str) -> str:
    return path.replace("/", "\\").rsplit("\\", 1)[-1]


def _addr(pdb: Pdb, s: Symbol, base: int) -> str:
    rva = pdb.rva(s.seg, s.off)
    return f"{rva:08X}  {base + rva:08X}" if rva is not None else f"{s.seg:04X}:{s.off:08X}".ljust(18)


def _cmd_find(pdb: Pdb, args) -> None:
    rx = re.compile(args.pattern, re.I)
    t = pdb.types
    kinds = {"struct": (LF_CLASS, LF_STRUCTURE, LF_INTERFACE), "union": (LF_UNION,), "enum": (LF_ENUM,)}
    allowed = kinds.get(args.kind, _UDT_KINDS)
    for name in sorted(t.defs):
        ti = t.defs[name][0]
        u = t.udt(ti)
        if u.kind not in allowed:
            continue
        if args.members:
            if u.kind == LF_ENUM:
                hits = [f for f in t.fields(u.fields) if f.kind == LF_ENUMERATE and rx.search(f.name)]
                for f in hits:
                    print(f"enum {name}: {f.name} = {f.value}")
                continue
            for f in t.members(ti):
                if rx.search(f.name):
                    where = "static" if f.kind == LF_STMEMBER else f"+0x{f.offset:04X}"
                    print(f"{name}  {where}  {t.decl(f.type, f.name)}")
        elif rx.search(name):
            size = f"0x{t.size(ti):X}" if u.kind != LF_ENUM else "enum"
            print(f"{_KEYWORD[u.kind]:6} {size:>8}  {name}")


def _cmd_procs(pdb: Pdb, args) -> None:
    rx = re.compile(args.pattern, re.I)
    procs = pdb.procs(args.publics)
    shown: set[int] = set()
    for i, s in enumerate(procs):
        if not rx.search(s.name):
            continue
        lo, hi = max(0, i - args.neighbours), min(len(procs), i + args.neighbours + 1)
        if args.neighbours and shown and lo > max(shown) + 1:
            print("    ...")
        for j in range(lo, hi):
            if j in shown:
                continue
            shown.add(j)
            p = procs[j]
            tag = {S_PUB32: "pub", S_LPROC32: "local", S_LPROC32_ID: "local"}.get(p.kind, "")
            mark = "*" if j == i or rx.search(p.name) else " "
            sig = pdb.types.decl(p.type, "") if p.type else ""
            module = _short_module(p.module)
            print(f"{mark} {_addr(pdb, p, args.base)}  size 0x{p.size:05X}  {p.name}"
                  f"{'  [' + tag + ']' if tag else ''}{'  {' + module + '}' if module else ''}"
                  f"{'  : ' + sig if sig and args.sig else ''}")


def _cmd_globals(pdb: Pdb, args) -> None:
    rx = re.compile(args.pattern, re.I)
    for s in pdb.data():
        if rx.search(s.name):
            scope = "local " if s.kind == S_LDATA32 else "global"
            print(f"{_addr(pdb, s, args.base)}  {scope}  {pdb.types.decl(s.type, s.name)}"
                  f"  (size 0x{pdb.types.size(s.type):X})  {{{_short_module(s.module)}}}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdb")
    ap.add_argument("--base", type=lambda v: int(v, 0), default=0x400000, help="image base for VAs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("struct", help="dump a struct/union/class as C")
    s.add_argument("name")
    s.add_argument("-r", "--recurse", type=int, default=0, help="also dump referenced UDTs to this depth")
    s.add_argument("--methods", action="store_true", help="list member functions as comments")
    s.add_argument("--nested", action="store_true", help="list nested type declarations as comments")
    e = sub.add_parser("enum", help="dump an enum with values")
    e.add_argument("name")
    f = sub.add_parser("find", help="list type names (or members with --members) matching a regex")
    f.add_argument("pattern")
    f.add_argument("--members", action="store_true", help="match member/enumerator names instead")
    f.add_argument("--kind", choices=("struct", "union", "enum"))
    p = sub.add_parser("procs", help="procedures with RVA and size")
    p.add_argument("pattern")
    p.add_argument("-n", "--neighbours", type=int, default=0, help="also show N procs either side")
    p.add_argument("--publics", action="store_true", help="include public symbols with no proc record")
    p.add_argument("--sig", action="store_true", help="print the procedure type")
    g = sub.add_parser("globals", help="data symbols with type")
    g.add_argument("pattern")
    args = ap.parse_args(argv)

    pdb = Pdb(args.pdb)
    if args.cmd == "struct":
        print(pdb.types.dump_tree(_pick(pdb.types, args.name), args.recurse, args.methods, args.nested))
    elif args.cmd == "enum":
        print(pdb.types.format_enum(_pick(pdb.types, args.name)))
    elif args.cmd == "find":
        _cmd_find(pdb, args)
    elif args.cmd == "procs":
        _cmd_procs(pdb, args)
    elif args.cmd == "globals":
        _cmd_globals(pdb, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
