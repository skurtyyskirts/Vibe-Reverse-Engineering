#!/usr/bin/env python3
"""Signature database for function identification in PE binaries.

Provides a SQLite-backed signature database with:
- Byte-pattern signatures (FLIRT-style, with relocation wildcards)
- Structural signatures (CFG shape + mnemonic hash)
- Compiler fingerprinting (Rich header, CRT imports, marker patterns)
- Multi-tier matching: byte-exact -> structural fallback
- Build pipeline from JSON manifest + CSV address maps
- CLI with build, scan, identify, fingerprint subcommands

Usage:
    python retools/sigdb.py build manifest.json -o sigs.db
    python retools/sigdb.py scan binary.exe -d sigs.db
    python retools/sigdb.py identify binary.exe 0x401000 -d sigs.db
    python retools/sigdb.py fingerprint binary.exe
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import struct
import sys
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Binary

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "signatures.db"

_HF_REPO_DEFAULT = "RTX-Remix/Vibe-Reverse-Engineering-Signature-DB"
_HF_URL_TEMPLATE = "https://huggingface.co/{repo}/resolve/main/{path}"

SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS byte_sigs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern BLOB NOT NULL,
    mask BLOB NOT NULL,
    func_size INTEGER NOT NULL DEFAULT 0,
    tail_crc INTEGER NOT NULL DEFAULT 0,
    compiler TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    prefix BLOB NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_byte_prefix ON byte_sigs (prefix);

CREATE TABLE IF NOT EXISTS structural_sigs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    block_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    call_count INTEGER NOT NULL,
    mnemonic_hash INTEGER NOT NULL,
    constants TEXT NOT NULL DEFAULT '',
    compiler TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_struct_mhash ON structural_sigs (mnemonic_hash);

CREATE TABLE IF NOT EXISTS compiler_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    compiler TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Match:
    name: str
    confidence: float
    tier: str
    compiler: str
    source: str
    category: str


def _masked_eq(code: bytes, pattern: bytes, mask: bytes) -> bool:
    if len(code) < len(pattern):
        return False
    for c, p, m in zip(code, pattern, mask):
        if m and c != p:
            return False
    return True


def _mnemonic_hash(insns) -> int:
    h = 0
    for insn in insns:
        h = (h * 31 + hash(insn.mnemonic)) & 0xFFFFFFFF
    return h


def _compute_tail_crc(code: bytes) -> int:
    if len(code) < 8:
        return 0
    return zlib.crc32(code[-8:]) & 0xFFFFFFFF


def _extract_prefix(pattern: bytes, mask: bytes, max_len: int = 8) -> bytes:
    prefix = bytearray()
    for b, m in zip(pattern[:max_len], mask[:max_len]):
        if not m:
            break
        prefix.append(b)
    return bytes(prefix)


def _download_file(url: str, dest: str) -> None:
    with urllib.request.urlopen(url) as resp:
        data = resp.read()
    with open(dest, "wb") as fout:
        fout.write(data)


def _pull_sources(repo: str, dest_dir: str) -> None:
    manifest_url = _HF_URL_TEMPLATE.format(repo=repo, path="sources/manifest.json")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(manifest_url) as resp:
            manifest = json.loads(resp.read())
        for fname in manifest.get("sources", []):
            url = _HF_URL_TEMPLATE.format(repo=repo, path=fname)
            try:
                _download_file(url, str(dest / Path(fname).name))
                print(f" Downloaded {fname}")
            except urllib.error.HTTPError as e:
                print(f" Warning: could not download {fname}: {e}", file=sys.stderr)
    except Exception as e:
        print(f" Warning: could not pull sources: {e}", file=sys.stderr)


def _parse_rich_header(pe_bytes: bytes) -> dict | None:
    RICH = b"Rich"
    DANS = b"DanS"
    rich_pos = pe_bytes.find(RICH)
    if rich_pos < 0:
        return None
    key = struct.unpack_from("<I", pe_bytes, rich_pos + 4)[0]
    dans_pos = -1
    for i in range(rich_pos - 4, -1, -4):
        if len(pe_bytes) < i + 4:
            continue
        val = struct.unpack_from("<I", pe_bytes, i)[0] ^ key
        if val == struct.unpack(">I", DANS)[0]:
            dans_pos = i
            break
        val2 = struct.unpack_from("<I", pe_bytes, i)[0]
        if val2 ^ key == int.from_bytes(DANS, "big"):
            dans_pos = i
            break
    if dans_pos < 0:
        return None
    entries: dict[int, int] = {}
    pos = dans_pos + 16
    while pos + 8 <= rich_pos:
        prod_id = struct.unpack_from("<I", pe_bytes, pos)[0] ^ key
        count = struct.unpack_from("<I", pe_bytes, pos + 4)[0] ^ key
        comp_id = prod_id >> 16
        build_id = prod_id & 0xFFFF
        entries[(comp_id, build_id)] = entries.get((comp_id, build_id), 0) + count
        pos += 8
    return entries


def parse_rich_header(b) -> list:
    try:
        pe_bytes = b.raw if hasattr(b, "raw") else bytes(b.pe.__data__)
    except Exception:
        return []
    result = _parse_rich_header(pe_bytes)
    if result is None:
        return []
    return [
        {"comp_id": comp_id, "build_id": build_id, "count": count}
        for (comp_id, build_id), count in result.items()
    ]


_MSVC_MARKER_PATTERNS = [
    b"\xcc\xcc\xcc\xcc",
    b"\x56\x8b\xf1",
    b"\x8b\xff\x55\x8b\xec",
]
_GCC_MARKER_PATTERNS = [
    b"\x55\x89\xe5",
    b"\x66\x90",
]
_MINGW_IMPORT_HINTS = [b"__mingw", b"libgcc", b"__imp_"]

_CRT_DLL_PATTERNS = [
    "msvcr",
    "vcruntime",
    "msvcp",
    "msvcrt",
    "ucrtbase",
    "api-ms-win-crt",
]

_CRT_NAME_HINTS = [
    "_malloc",
    "malloc",
    "_free",
    "free",
    "_realloc",
    "realloc",
    "_calloc",
    "calloc",
    "_new",
    "_delete",
    "operator_new",
    "operator_delete",
    "__security",
    "_init",
    "__init",
    "_exit",
    "_atexit",
    "atexit",
    "_memcpy",
    "memcpy",
    "_memset",
    "memset",
    "_memmove",
    "memmove",
    "_strcmp",
    "strcmp",
    "_strlen",
    "strlen",
    "__crt",
    "_crt",
    "_purecall",
    "__purecall",
    "_assert",
    "__assert",
]
_MATH_NAME_HINTS = [
    "sin",
    "cos",
    "tan",
    "sqrt",
    "log",
    "exp",
    "pow",
    "floor",
    "ceil",
    "fabs",
    "fmod",
    "atan",
    "asin",
    "acos",
    "_CI",
    "flt_",
]


def _categorize_name(name: str) -> str:
    lower = name.lower()
    for hint in _CRT_NAME_HINTS:
        if hint.lower() in lower:
            return "crt"
    for hint in _MATH_NAME_HINTS:
        if hint in name or hint.lower() in lower:
            return "math"
    return "unknown"


def detect_crt_import(b) -> str | None:
    try:
        for imp in b.pe.DIRECTORY_ENTRY_IMPORT:
            dll_name = imp.dll.decode(errors="replace").lower() if imp.dll else ""
            for pat in _CRT_DLL_PATTERNS:
                if pat in dll_name:
                    return "msvc"
    except AttributeError:
        pass
    return None


def _scan_import_names(b) -> list:
    names = []
    try:
        for imp in b.pe.DIRECTORY_ENTRY_IMPORT:
            names.append(imp.dll.decode(errors="replace").lower())
            for sym in imp.imports:
                if sym.name:
                    names.append(sym.name.decode(errors="replace").lower())
    except AttributeError:
        pass
    return names


def _estimate_func_size(b, va: int) -> int:
    try:
        insns = list(b.disasm(va, 0x200))
    except Exception:
        return 0
    if not insns:
        return 0

    last_ret_end = 0
    i = 0
    while i < len(insns):
        insn = insns[i]
        if insn.mnemonic in ("ret", "retn"):
            end = insn.address + insn.size - va
            last_ret_end = end
            j = i + 1
            nop_run = 0
            while j < len(insns) and insns[j].mnemonic == "nop":
                nop_run += 1
                j += 1
            if j < len(insns) and nop_run > 0:
                return last_ret_end
            i = j
        elif insn.mnemonic == "int3":
            return last_ret_end if last_ret_end else 0
        else:
            i += 1

    if last_ret_end:
        return last_ret_end
    last = insns[-1]
    return last.address + last.size - va


def extract_byte_sig(b, va: int):
    code = b.read_va(va, 32)
    if not code or len(code) < 32:
        return None

    mask = bytearray(b"\xff" * 32)
    i = 0
    while i < 28:
        byte = code[i]
        if byte in (0xE8, 0xE9):
            mask[i + 1] = 0x00
            mask[i + 2] = 0x00
            mask[i + 3] = 0x00
            mask[i + 4] = 0x00
            i += 5
        else:
            i += 1

    func_size = _estimate_func_size(b, va)

    if func_size > 0:
        tail_start = max(0, func_size - 32)
        tail_bytes = b.read_va(va + tail_start, func_size - tail_start)
        tail_crc = zlib.crc32(tail_bytes) & 0xFFFFFFFF if tail_bytes else 0
    else:
        tail_crc = 0

    return bytes(code[:32]), bytes(mask), tail_crc, func_size


def extract_structural_sig(b, va: int):
    try:
        insns = list(b.disasm(va, 0x2000))
    except Exception:
        return None
    if not insns:
        return None

    block_count = 1
    edge_count = 0
    call_count = 0
    constants = []

    prev_was_branch = False
    for insn in insns:
        mn = insn.mnemonic
        if prev_was_branch:
            block_count += 1
            prev_was_branch = False
        if mn in (
            "jmp",
            "je",
            "jne",
            "jz",
            "jnz",
            "jg",
            "jge",
            "jl",
            "jle",
            "ja",
            "jae",
            "jb",
            "jbe",
            "jcxz",
            "jecxz",
            "loop",
        ):
            edge_count += 1
            if mn != "jmp":
                edge_count += 1
            prev_was_branch = True
        elif mn == "call":
            call_count += 1
        elif mn in ("ret", "retn"):
            edge_count += 1
            prev_was_branch = True
        if hasattr(insn, "op_str") and insn.op_str:
            import re

            for m in re.finditer(r"0x[0-9a-fA-F]+", insn.op_str):
                val = int(m.group(), 16)
                if val > 0xFF:
                    constants.append(hex(val))

    mh = _mnemonic_hash(insns)
    return {
        "block_count": block_count,
        "edge_count": edge_count,
        "call_count": call_count,
        "mnemonic_hash": mh,
        "constants": ",".join(sorted(set(constants))[:20]),
    }


class SignatureDB:
    def __init__(self, path=DEFAULT_DB_PATH):
        self._path = str(path)
        self._conn = None
        self._open()

    def _open(self):
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cur.fetchone() is None:
            self._init_schema()
        else:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                self._migrate(row[0] if row else 0)

    def _migrate(self, from_version: int):
        if from_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {from_version} is newer than "
                f"supported version {SCHEMA_VERSION}"
            )
        if from_version == 1:
            try:
                self._conn.execute(
                    "ALTER TABLE byte_sigs ADD COLUMN prefix BLOB NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_byte_prefix ON byte_sigs (prefix)"
            )
            rows = self._conn.execute(
                "SELECT id, pattern, mask FROM byte_sigs"
            ).fetchall()
            self._conn.executemany(
                "UPDATE byte_sigs SET prefix = ? WHERE id = ?",
                (
                    (_extract_prefix(bytes(row[1]), bytes(row[2])), row[0])
                    for row in rows
                ),
            )
            self._conn.execute(
                "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
            )
            self._conn.commit()
        else:
            self._conn.executescript(
                "DROP TABLE IF EXISTS schema_version;"
                "DROP TABLE IF EXISTS byte_sigs;"
                "DROP TABLE IF EXISTS structural_sigs;"
                "DROP TABLE IF EXISTS compiler_fingerprints;"
            )
            self._init_schema()

    def _init_schema(self):
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def add_byte_sig(
        self,
        *,
        name,
        pattern,
        mask,
        func_size,
        tail_crc,
        compiler="",
        source="",
        category="",
    ):
        prefix = _extract_prefix(pattern, mask)
        self._conn.execute(
            "INSERT INTO byte_sigs "
            "(name, pattern, mask, func_size, tail_crc, compiler, source, category, prefix) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                pattern,
                mask,
                func_size,
                tail_crc,
                compiler,
                source,
                category,
                prefix,
            ),
        )
        self._conn.commit()

    def add_structural_sig(
        self,
        *,
        name,
        block_count,
        edge_count,
        call_count,
        mnemonic_hash,
        constants="",
        compiler="",
        source="",
        category="",
    ):
        self._conn.execute(
            "INSERT INTO structural_sigs "
            "(name, block_count, edge_count, call_count, mnemonic_hash, "
            "constants, compiler, source, category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                block_count,
                edge_count,
                call_count,
                mnemonic_hash,
                constants,
                compiler,
                source,
                category,
            ),
        )
        self._conn.commit()

    def match_bytes(self, code, func_size, preferred_compiler, func_tail_crc):
        if len(code) < 32:
            return []
        code32 = code[:32]
        prefixes = [code32[:i] for i in range(min(len(code32), 8) + 1)]
        query = (
            "SELECT name, pattern, mask, func_size, tail_crc, "
            "compiler, source, category FROM byte_sigs "
            f"WHERE prefix IN ({','.join(['?'] * len(prefixes))})"
        )
        cur = self._conn.execute(query, prefixes)
        candidates = []
        for (
            name,
            pattern,
            mask,
            db_size,
            db_tail_crc,
            compiler,
            source,
            category,
        ) in cur:
            if not _masked_eq(code32, pattern, mask):
                continue
            score = 0.7
            if db_size == func_size:
                score += 0.15
            elif (
                db_size > 0 and abs(db_size - func_size) / max(db_size, func_size) < 0.1
            ):
                score += 0.05
            if db_tail_crc != 0 and func_tail_crc != 0 and db_tail_crc == func_tail_crc:
                score += 0.10
            if preferred_compiler and compiler == preferred_compiler:
                score += 0.05
            candidates.append(
                (
                    Match(
                        name=name,
                        confidence=min(score, 1.0),
                        tier="byte",
                        compiler=compiler,
                        source=source,
                        category=category,
                    ),
                    score,
                )
            )
        candidates.sort(key=lambda c: c[1], reverse=True)
        return [m for m, _ in candidates]

    def match_structural(
        self,
        block_count,
        edge_count,
        call_count,
        mnemonic_hash,
        constants="",
        preferred_compiler="",
    ):
        cur = self._conn.execute(
            "SELECT name, block_count, edge_count, call_count, mnemonic_hash, "
            "compiler, source, category FROM structural_sigs "
            "WHERE mnemonic_hash = ?",
            (mnemonic_hash,),
        )
        candidates = []
        for name, bc, ec, cc, mh2, compiler, source, category in cur:
            score = 0.5
            if bc == block_count:
                score += 0.15
            if ec == edge_count:
                score += 0.10
            if cc == call_count:
                score += 0.10
            if preferred_compiler and compiler == preferred_compiler:
                score += 0.05
            candidates.append(
                (
                    Match(
                        name=name,
                        confidence=min(score, 1.0),
                        tier="structural",
                        compiler=compiler,
                        source=source,
                        category=category,
                    ),
                    score,
                )
            )
        candidates.sort(key=lambda c: c[1], reverse=True)
        return [m for m, _ in candidates]

    def identify(self, b, va, preferred_compiler=""):
        code = b.read_va(va, 256)
        func_size = 0
        tail_crc = _compute_tail_crc(code)
        fp = self.fingerprint(b)
        compiler = preferred_compiler or fp.get("compiler", "")
        byte_matches = self.match_bytes(code, func_size, compiler, tail_crc)
        if byte_matches:
            return byte_matches[0]
        sig = extract_structural_sig(b, va)
        if sig:
            struct_matches = self.match_structural(
                block_count=sig["block_count"],
                edge_count=sig["edge_count"],
                call_count=sig["call_count"],
                mnemonic_hash=sig["mnemonic_hash"],
                constants=sig.get("constants", ""),
                preferred_compiler=compiler,
            )
            if struct_matches:
                return struct_matches[0]
        return None

    def scan(self, b, preferred_compiler=""):
        fp = self.fingerprint(b)
        compiler = preferred_compiler or fp.get("compiler", "")
        results = {}
        for va in getattr(b, "func_table", []):
            code = b.read_va(va, 256)
            if not code:
                continue
            tail_crc = _compute_tail_crc(code)
            ms = self.match_bytes(code, 0, compiler, tail_crc)
            if ms:
                results[va] = ms[0]
                continue
            sig = extract_structural_sig(b, va)
            if sig:
                ms = self.match_structural(
                    block_count=sig["block_count"],
                    edge_count=sig["edge_count"],
                    call_count=sig["call_count"],
                    mnemonic_hash=sig["mnemonic_hash"],
                    constants=sig.get("constants", ""),
                    preferred_compiler=compiler,
                )
                if ms:
                    results[va] = ms[0]
        return results

    def fingerprint(self, b):
        evidence = []
        scores = {}
        import_names = _scan_import_names(b)

        msvc_crt = sum(
            1 for n in import_names if any(p in n for p in _CRT_DLL_PATTERNS)
        )
        if msvc_crt:
            scores["msvc"] = scores.get("msvc", 0.0) + 0.4
            evidence.append(f"MSVC CRT imports: {msvc_crt}")

        mingw_hints = sum(
            1
            for n in import_names
            for hint in _MINGW_IMPORT_HINTS
            if hint.decode() in n
        )
        if mingw_hints:
            scores["gcc"] = scores.get("gcc", 0.0) + 0.4
            evidence.append(f"MinGW import hints: {mingw_hints}")

        try:
            pe_bytes = bytes(b.pe.__data__[:0x1000])
            rich = _parse_rich_header(pe_bytes)
            if rich is not None:
                for (comp_id, _), count in rich.items():
                    if comp_id in (0x0001, 0x00C7, 0x00FF, 0x0102):
                        scores["msvc"] = scores.get("msvc", 0.0) + 0.3
                        evidence.append(
                            f"Rich header MSVC comp_id=0x{comp_id:04X} x{count}"
                        )
                        break
            elif b"Rich" not in pe_bytes:
                scores["gcc"] = scores.get("gcc", 0.0) + 0.2
                evidence.append("No Rich header (GCC/Clang indicator)")
        except Exception:
            pass

        try:
            scan_bytes = b.read_va(
                b.base, min(0x10000, b.pe.OPTIONAL_HEADER.SizeOfImage)
            )
            msvc_hits = sum(1 for pat in _MSVC_MARKER_PATTERNS if pat in scan_bytes)
            gcc_hits = sum(1 for pat in _GCC_MARKER_PATTERNS if pat in scan_bytes)
            if msvc_hits:
                scores["msvc"] = scores.get("msvc", 0.0) + 0.1 * msvc_hits
                evidence.append(f"MSVC code patterns: {msvc_hits}")
            if gcc_hits:
                scores["gcc"] = scores.get("gcc", 0.0) + 0.1 * gcc_hits
                evidence.append(f"GCC code patterns: {gcc_hits}")
        except Exception:
            pass

        if not scores:
            return {"compiler": "unknown", "confidence": 0.0, "evidence": evidence}
        best = max(scores, key=scores.get)
        return {
            "compiler": best,
            "confidence": min(scores[best], 1.0),
            "evidence": evidence,
        }


def build_from_manifest(db, manifest: dict) -> int:
    added = 0
    for source in manifest.get("sources", []):
        compiler = source.get("compiler", "")
        binary_path = source.get("binary", "")
        map_path = source.get("map", "")

        if not Path(binary_path).exists():
            print(f" WARNING: binary not found: {binary_path}")
            continue

        try:
            b = Binary(str(binary_path))
        except Exception as e:
            print(f" ERROR loading {binary_path}: {e}")
            continue

        addr_map = {}
        if map_path and Path(map_path).exists():
            try:
                with open(map_path, newline="") as cf:
                    for row in csv.DictReader(cf):
                        try:
                            addr_map[row["name"]] = int(row["address"], 16)
                        except (ValueError, KeyError):
                            pass
            except Exception as e:
                print(f" WARNING: could not read map {map_path}: {e}")

        for name, va in addr_map.items():
            try:
                result = extract_byte_sig(b, va)
                if result is None:
                    continue
                pattern, mask, tail_crc, func_size = result
                category = _categorize_name(name)
                db.add_byte_sig(
                    name=name,
                    pattern=pattern,
                    mask=mask,
                    func_size=func_size,
                    tail_crc=tail_crc,
                    compiler=compiler,
                    source=str(binary_path),
                    category=category,
                )
                added += 1
            except Exception as e:
                print(f" ERROR: {name} @ 0x{va:X}: {e}")

    return added


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Signature database for TRL reverse engineering"
    )
    sub = ap.add_subparsers(dest="command", required=True)

    bp = sub.add_parser("build", help="Build signature DB from a manifest")
    bp.add_argument("manifest", help="JSON manifest path")
    bp.add_argument(
        "-o", "--output", default=str(DEFAULT_DB_PATH), help="Output DB path"
    )
    bp.add_argument(
        "--csv", nargs="*", default=[], help="Optional CSV address override files"
    )

    sp = sub.add_parser("scan", help="Scan a binary for known functions")
    sp.add_argument("binary", help="PE binary path")
    sp.add_argument(
        "-d", "--db", default=str(DEFAULT_DB_PATH), help="Signature DB path"
    )
    sp.add_argument("--compiler", default="", help="Preferred compiler hint")

    ip = sub.add_parser("identify", help="Identify a single function")
    ip.add_argument("binary", help="PE binary path")
    ip.add_argument("va", help="Virtual address (hex)")
    ip.add_argument(
        "-d", "--db", default=str(DEFAULT_DB_PATH), help="Signature DB path"
    )
    ip.add_argument("--compiler", default="", help="Preferred compiler hint")

    fp = sub.add_parser("fingerprint", help="Fingerprint compiler of a binary")
    fp.add_argument("binary", help="PE binary path")

    pp = sub.add_parser("pull", help="Pull signature DB from HuggingFace")
    pp.add_argument(
        "--repo", default=_HF_REPO_DEFAULT, help="HuggingFace repo (owner/name)"
    )
    pp.add_argument(
        "--sources", action="store_true", help="Also pull source JSON files"
    )

    args = ap.parse_args(argv)

    if args.command == "build":
        db = SignatureDB(args.output)
        with open(args.manifest) as f:
            manifest = json.load(f)
        n = build_from_manifest(db, manifest)
        print(f"Added {n} signatures to {args.output}")
        db.close()

    elif args.command == "scan":
        try:
            DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            db = SignatureDB(args.db)
            b = Binary(args.binary)
            results = db.scan(b, args.compiler)
            for va, m in results.items():
                print(f"  0x{va:X} {m.name} ({m.tier}, {m.confidence:.0%})")
            db.close()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "identify":
        db = SignatureDB(args.db)
        b = Binary(args.binary)
        va = int(args.va, 16)
        m = db.identify(b, va, args.compiler)
        if m:
            print(f"  {m.name}")
            print(f"  confidence: {m.confidence:.0%}")
            print(f"  tier: {m.tier}")
            print(f"  compiler: {m.compiler}")
            print(f"  source: {m.source}")
            print(f"  category: {m.category}")
        else:
            print("  No match")
        db.close()

    elif args.command == "fingerprint":
        b = Binary(args.binary)
        db = SignatureDB(":memory:")
        result = db.fingerprint(b)
        print(f"  Compiler: {result['compiler']}")
        print(f"  Confidence: {result['confidence']:.0%}")
        for ev in result["evidence"]:
            print(f"    - {ev}")
        db.close()

    elif args.command == "pull":
        data_dir = DEFAULT_DB_PATH.parent
        data_dir.mkdir(parents=True, exist_ok=True)
        db_url = _HF_URL_TEMPLATE.format(repo=args.repo, path="signatures.db")
        print(f"Downloading from {args.repo}...")
        _download_file(db_url, str(data_dir / "signatures.db"))
        if args.sources:
            _pull_sources(args.repo, str(data_dir / "sources"))
        print("Done.")


if __name__ == "__main__":
    main()
