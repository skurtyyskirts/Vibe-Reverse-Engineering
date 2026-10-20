"""Offline report for a `livetools sample` recording: self and inclusive time per function, per thread.

Symbols, per module, first match wins:
  1. a PDB next to the module (same stem) whose GUID matches the module's CodeView record;
  2. an index.db `funcs` table given with --names MODULE=PATH (e.g. the game exe's retools index);
  3. the module's export table (nearest export below, within 64 KB);
  otherwise the address is reported as `module!+0xRVA` (bucketed to 256 bytes).

Inclusive time walks the recorded stack words and keeps those that sit right after a CALL instruction in
the module's on-disk image (E8 rel32 or FF /2), so stale data on the stack rarely counts as a caller.
"""
from __future__ import annotations

import bisect
import gzip
import json
import mmap
import re
import sqlite3
import struct
from collections import Counter, defaultdict
from pathlib import Path

import pefile

MSF_MAGIC = b"Microsoft C/C++ MSF 7.00\r\n\x1aDS\x00\x00\x00"
S_PUB32, S_LPROC32, S_GPROC32 = 0x110E, 0x110F, 0x1110
S_LPROC32_ID, S_GPROC32_ID = 0x1146, 0x1147
PROC_TYPES = (S_LPROC32, S_GPROC32, S_LPROC32_ID, S_GPROC32_ID)


def msf_streams(blob: bytes) -> list[bytes]:
    """Every stream of an MSF 7.0 (PDB) file, reassembled from its pages."""
    if not blob.startswith(MSF_MAGIC):
        raise ValueError("not an MSF 7.0 file")
    block, _, _, dir_bytes, _, map_addr = struct.unpack_from("<6I", blob, 0x20)
    n_dir_blocks = (dir_bytes + block - 1) // block
    dir_blocks = struct.unpack_from("<%dI" % n_dir_blocks, blob, map_addr * block)
    directory = b"".join(blob[b * block:(b + 1) * block] for b in dir_blocks)[:dir_bytes]
    n = struct.unpack_from("<I", directory, 0)[0]
    sizes = struct.unpack_from("<%dI" % n, directory, 4)
    pos = 4 + 4 * n
    streams = []
    for size in sizes:
        if size == 0xFFFFFFFF:
            streams.append(b"")
            continue
        nb = (size + block - 1) // block
        blocks = struct.unpack_from("<%dI" % nb, directory, pos)
        pos += 4 * nb
        streams.append(b"".join(blob[b * block:(b + 1) * block] for b in blocks)[:size])
    return streams


def pdb_guid_age(streams: list[bytes]) -> tuple[bytes, int]:
    info = streams[1]
    return info[12:28], struct.unpack_from("<I", info, 8)[0]


def _cstr(buf: bytes, pos: int, limit: int) -> str | None:
    end = buf.find(b"\0", pos, limit)
    if end < 0 or end == pos:
        return None
    raw = buf[pos:end]
    return raw.decode("latin1") if all(32 <= c < 127 for c in raw) else None


def pdb_symbols(streams: list[bytes], section_rvas: list[int]) -> list[tuple[int, int | None, str]]:
    """(rva, size or None, name) for every procedure and public symbol record in the PDB's streams."""
    out = []
    nsec = len(section_rvas)
    for idx, s in enumerate(streams):
        if idx < 3 or idx == 4:   # header, PDB info, TPI, IPI: no symbol records
            continue
        for m in re.finditer(rb"[\x0e-\x10]\x11|\x46\x11|\x47\x11", s):
            pos = m.start()
            if pos < 2:
                continue
            kind = struct.unpack_from("<H", s, pos)[0]
            reclen = struct.unpack_from("<H", s, pos - 2)[0]
            end = pos + reclen
            if end > len(s):
                continue
            if kind == S_PUB32 and reclen >= 12:
                off, seg = struct.unpack_from("<IH", s, pos + 6)
                name = _cstr(s, pos + 12, end)
                if name and 1 <= seg <= nsec:
                    out.append((section_rvas[seg - 1] + off, None, name))
            elif kind in PROC_TYPES and reclen >= 37:
                size = struct.unpack_from("<I", s, pos + 14)[0]
                off, seg = struct.unpack_from("<IH", s, pos + 30)
                name = _cstr(s, pos + 37, end)
                if name and 1 <= seg <= nsec and size < (1 << 24):
                    out.append((section_rvas[seg - 1] + off, size, name))
    return out


class ModuleSymbols:
    """Address -> function name for one module image."""

    def __init__(self, mod: dict, names_db: str | None):
        self.name = mod["name"]
        self.base = mod["base"]
        self.size = mod["size"]
        self.source = "none"
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.names: list[str] = []
        self.pe = None
        self.image = None
        path = Path(mod["path"])
        if path.exists():
            self.pe = pefile.PE(str(path), fast_load=True)
            self.pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DEBUG"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
            fh = open(path, "rb")
            self.image = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        syms = (self._from_pdb(path) or self._from_index(names_db) or self._from_exports())
        if syms:
            syms.sort(key=lambda x: (x[0], x[1] is None))
            for i, (rva, size, name) in enumerate(syms):
                if self.starts and self.starts[-1] == rva:
                    continue
                nxt = syms[i + 1][0] if i + 1 < len(syms) else rva + 0x10000
                self.starts.append(rva)
                self.ends.append(rva + size if size else min(nxt, rva + 0x10000))
                self.names.append(name)

    def _from_pdb(self, path: Path):
        pdb = path.with_suffix(".pdb")
        if not (self.pe and pdb.exists() and hasattr(self.pe, "DIRECTORY_ENTRY_DEBUG")):
            return None
        cv = next((self.image[d.struct.PointerToRawData:d.struct.PointerToRawData + 24]
                   for d in self.pe.DIRECTORY_ENTRY_DEBUG if d.struct.Type == 2), b"")
        if cv[:4] != b"RSDS":
            return None
        guid = cv[4:20]   # CV_INFO_PDB70: 'RSDS', GUID, age
        streams = msf_streams(pdb.read_bytes())
        if pdb_guid_age(streams)[0] != guid:
            self.source = "pdb-mismatch"
            return None
        self.source = "pdb"
        return pdb_symbols(streams, [s.VirtualAddress for s in self.pe.sections])

    def _from_index(self, db: str | None):
        if not (db and self.pe):
            return None
        image_base = self.pe.OPTIONAL_HEADER.ImageBase
        con = sqlite3.connect(db)
        rows = con.execute("select address, end_ea, name from funcs").fetchall()
        con.close()
        self.source = "index.db"
        return [(a - image_base, (e - a) if e and e > a else None, n or f"sub_{a:X}") for a, e, n in rows]

    def _from_exports(self):
        if not (self.pe and hasattr(self.pe, "DIRECTORY_ENTRY_EXPORT")):
            return None
        self.source = "exports"
        return [(e.address, None, (e.name or f"ord{e.ordinal}".encode()).decode("latin1"))
                for e in self.pe.DIRECTORY_ENTRY_EXPORT.symbols if e.address]

    def lookup(self, addr: int) -> str:
        rva = addr - self.base
        i = bisect.bisect_right(self.starts, rva) - 1
        if i >= 0 and rva < self.ends[i]:
            return f"{self.name}!{self.names[i]}"
        return f"{self.name}!+0x{rva & ~0xFF:X}"

    def follows_call(self, addr: int) -> bool:
        if not (self.pe and self.image):
            return False
        rva = addr - self.base
        try:
            off = self.pe.get_offset_from_rva(rva)
        except pefile.PEFormatError:
            return False
        if off is None or off < 7 or off > len(self.image):   # uninitialized-data sections have no file bytes
            return False
        b = self.image[off - 7:off]
        return (b[2] == 0xE8 or (b[1] == 0xFF and (b[2] & 0x38) == 0x10) or
                (b[3] == 0xFF and (b[4] & 0x38) == 0x10) or (b[4] == 0xFF and (b[5] & 0x38) == 0x10) or
                (b[5] == 0xFF and (b[6] & 0x38) == 0x10) or
                (b[0] == 0xFF and (b[1] & 0x38) == 0x10))


class Symbolizer:
    def __init__(self, mods: list[dict], names: dict[str, str]):
        self.mods = mods
        self.bases = [m["base"] for m in mods]
        self.cache: dict[int, ModuleSymbols] = {}
        self.names = {k.lower(): v for k, v in names.items()}

    def module(self, addr: int) -> ModuleSymbols | None:
        i = bisect.bisect_right(self.bases, addr) - 1
        if i < 0 or addr >= self.mods[i]["base"] + self.mods[i]["size"]:
            return None
        if i not in self.cache:
            m = self.mods[i]
            self.cache[i] = ModuleSymbols(m, self.names.get(m["name"].lower()))
        return self.cache[i]

    def name(self, addr: int) -> str:
        m = self.module(addr)
        return m.lookup(addr) if m else f"?!0x{addr:X}"


def load(path: Path) -> tuple[dict, list[dict]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        header = json.loads(f.readline())
        return header, [json.loads(line) for line in f]


def report(path: Path, names: dict[str, str], top: int = 25, max_frames: int = 24) -> str:
    header, samples = load(path)
    sym = Symbolizer(header["modules"], names)
    by_tid: dict[int, list[dict]] = defaultdict(list)
    for s in samples:
        by_tid[s["tid"]].append(s)
    lines = [f"profile {path.name}: {header['target']} pid {header['pid']} "
             f"({'x86' if header['wow64'] else 'x64'}), started {header['started']}, {len(samples)} samples"]
    for th in header["threads"]:
        rows = by_tid.get(th["tid"], [])
        if not rows:
            continue
        n = len(rows)
        self_c: Counter = Counter()
        incl_c: Counter = Counter()
        mod_c: Counter = Counter()
        paths: Counter = Counter()
        for s in rows:
            leaf = sym.name(s["ip"])
            self_c[leaf] += 1
            mod_c[leaf.split("!")[0]] += 1
            chain = [leaf]
            for w in s["st"][:256]:
                m = sym.module(w)
                if m and m.follows_call(w):
                    nm = m.lookup(w)
                    if nm != chain[-1]:
                        chain.append(nm)
                    if len(chain) >= max_frames:
                        break
            for nm in set(chain):
                incl_c[nm] += 1
            paths[" <- ".join(chain[:4])] += 1
        span = (rows[-1]["t"] - rows[0]["t"]) / 1000 if n > 1 else 0
        lines.append("")
        lines.append(f"=== thread {th['tid']}  cpu {th['cpu']}  start {sym.name(th['start']) if th['start'] else '?'}"
                     f"  samples {n} over {span:.1f} s ===")
        lines.append("  modules (self): " + ", ".join(f"{k} {v * 100 / n:.1f}%" for k, v in mod_c.most_common(8)))
        lines.append("  -- self --")
        lines += [f"  {v * 100 / n:6.2f}%  {k}" for k, v in self_c.most_common(top)]
        lines.append("  -- inclusive --")
        lines += [f"  {v * 100 / n:6.2f}%  {k}" for k, v in incl_c.most_common(top)]
        lines.append("  -- hottest paths (leaf <- callers) --")
        lines += [f"  {v * 100 / n:6.2f}%  {k}" for k, v in paths.most_common(12)]
    srcs = sorted({f"{m.name}={m.source}" for m in sym.cache.values()})
    lines.append("")
    lines.append("symbol sources: " + ", ".join(srcs))
    return "\n".join(lines)
