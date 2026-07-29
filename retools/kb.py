"""Single source of truth for the kb.h grammar, plus a writer for it.

kb.h lines take three shapes:
    @ 0xADDR <signature>;      -- function at an address
    $ 0xADDR <type...> <name>  -- global variable at an address
    <C declaration>            -- bare typedef / struct / enum
Lines that are blank or begin with ``//`` are ignored.

The knowledge base is how discoveries compound: every entry makes the next
decompilation more readable. That only works if findings can be written back
as they are confirmed, which is what ``add_entries`` is for — a live trace
that proves what a function does should end with the function in kb.h, not in
a chat message.

Parsing skips lines it cannot read, so a malformed entry is invisible rather
than loud. ``validate`` is the lint for that: it reports every line that is not
a usable entry — both the ones ``parse_kb`` discards outright and the ones that
parse into something the backends cannot use.

Usage (CLI):
    python -m retools.kb validate patches/MyGame/kb.h
    python -m retools.kb show patches/MyGame/kb.h
    python -m retools.kb add patches/MyGame/kb.h \\
        --func 0x401000 "void __cdecl ProcessInput(int key)" \\
        --global 0x7C5548 "Object* g_mainObject" \\
        --type "struct Foo { int x; float y; };"

Usage (library):
    from retools.kb import add_entries, validate
    add_entries("patches/MyGame/kb.h", functions=[(0x401000, "void Foo(int)")])
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class KbFunction:
    address: int
    signature: str  # no leading "@ 0xADDR", no trailing ";"
    name: str


@dataclass(frozen=True)
class KbGlobal:
    address: int
    type: str  # "" when the line is "$ 0xADDR name"
    name: str


@dataclass
class Kb:
    functions: list[KbFunction] = field(default_factory=list)
    globals: list[KbGlobal] = field(default_factory=list)
    typedefs: list[str] = field(default_factory=list)


def extract_function_name(sig: str) -> str:
    """Extract the function name from a signature (no address, no ';').

    Name is the last whitespace-separated token before '(' (or the whole
    pre-paren text), with leading pointer/reference decorators stripped.
    """
    paren = sig.find("(")
    pre = sig[:paren] if paren != -1 else sig
    pre = pre.strip()
    if not pre:
        return ""
    return pre.rsplit(None, 1)[-1].lstrip("*&")


def parse_kb(text_or_path: str | Path) -> Kb:
    """Parse kb.h content or a file.

    A ``Path`` is read from disk; a ``str`` is always treated as content. Callers
    that hold a filesystem path pass a ``Path`` so a one-line kb string is never
    mistaken for a filename.
    """
    text = (text_or_path.read_text(encoding="utf-8", errors="replace")
            if isinstance(text_or_path, Path) else text_or_path)
    kb = Kb()
    for raw in text.splitlines():
        # Inline comments never reach entries: signatures/names/types feed
        # backend command strings (r2, Ghidra) where stray text is unsafe.
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue

        if line.startswith("@ "):
            parts = line[2:].split(None, 1)
            if len(parts) < 2:
                continue
            try:
                addr = int(parts[0], 16)
            except ValueError:
                continue
            sig = parts[1].rstrip(";").strip()
            kb.functions.append(
                KbFunction(address=addr, signature=sig, name=extract_function_name(sig))
            )
        elif line.startswith("$ "):
            parts = line[2:].split()
            if len(parts) < 2:
                continue
            try:
                addr = int(parts[0], 16)
            except ValueError:
                continue
            name = parts[-1]
            type_ = " ".join(parts[1:-1])
            kb.globals.append(KbGlobal(address=addr, type=type_, name=name))
        else:
            kb.typedefs.append(line)
    return kb


def read_existing_addresses(path: str | Path) -> set[int]:
    """Return the addresses of ``@`` function and ``$`` global entries in a kb.h file.

    Used by bootstrap to skip addresses that already have an entry.
    Returns an empty set if the file does not exist.
    """
    if not os.path.isfile(path):
        return set()
    kb = parse_kb(Path(path))
    return {f.address for f in kb.functions} | {g.address for g in kb.globals}


# ── Validation ─────────────────────────────────────────────────────────────

def line_problem(line: str) -> str | None:
    """Explain why a line is not a usable kb.h entry, or None if it is fine.

    Stricter than ``parse_kb``, deliberately. The parser discards most
    malformed lines, but a few parse into an entry that is wrong rather than
    absent — a function signature with no parameter list yields a "name" taken
    from its return type, which then reaches r2 and Ghidra as a command
    argument. Both cases are reported here so neither reaches the KB.

    Args:
        line: One raw line from a kb.h file.

    Returns:
        A description of the problem, or None when the line is fine (including
        blanks and comments, which are ignored by design).
    """
    stripped = line.split("//", 1)[0].strip()
    if not stripped:
        return None

    if stripped.startswith(("@", "$")) and not stripped.startswith(("@ ", "$ ")):
        return f"'{stripped[0]}' must be followed by a space, then 0xADDR"

    if stripped.startswith("@ "):
        parts = stripped[2:].split(None, 1)
        if len(parts) < 2:
            return "function entry needs '@ 0xADDR <signature>'"
        try:
            int(parts[0], 16)
        except ValueError:
            return f"'{parts[0]}' is not a hex address"
        signature = parts[1].rstrip(";").strip()
        if "(" not in signature:
            return ("function entry needs a parameter list, e.g. "
                    "'void Foo(int)' — this parses, but names the function "
                    "after its return type")
        if not extract_function_name(signature):
            return "signature has no recognizable function name"
        return None

    if stripped.startswith("$ "):
        parts = stripped[2:].split()
        if len(parts) < 2:
            return "global entry needs '$ 0xADDR <type> <name>'"
        try:
            int(parts[0], 16)
        except ValueError:
            return f"'{parts[0]}' is not a hex address"
    return None


def validate(path: str | Path) -> list[tuple[int, str, str]]:
    """Find every line in a kb.h file that is not a usable entry.

    Returns:
        List of (line_number, line_text, problem).

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [(n, line.rstrip(), problem)
            for n, line in enumerate(text.splitlines(), start=1)
            if (problem := line_problem(line))]


# ── Writing ────────────────────────────────────────────────────────────────

def format_function(address: int, signature: str) -> str:
    return f"@ 0x{address:X} {signature.rstrip(';').strip()};"


def format_global(address: int, declaration: str) -> str:
    return f"$ 0x{address:X} {declaration.strip()}"


def add_entries(path: str | Path, functions: list[tuple[int, str]] | None = None,
                globals_: list[tuple[int, str]] | None = None,
                typedefs: list[str] | None = None,
                replace: bool = True) -> dict:
    """Append or update kb.h entries, keeping one entry per address.

    Args:
        path:      kb.h file; created (with parents) if missing.
        functions: (address, signature) pairs, e.g. (0x401000, "void Foo(int)").
        globals_:  (address, declaration) pairs, e.g. (0x7C5548, "Obj* g_main").
        typedefs:  Bare C declarations (struct/enum/typedef), added if absent.
        replace:   Rewrite an existing entry for the same address. With
                   replace=False an address that already has an entry is left
                   alone and counted as skipped.

    Returns:
        dict with `added`, `replaced`, `skipped` counts and the resulting
        `path`.

    Raises:
        ValueError: If any entry would produce a line kb.h cannot parse —
            better to reject it here than to write a line that silently
            disappears on the next read.
    """
    new_lines: list[tuple[int | None, str]] = []
    for address, signature in functions or []:
        new_lines.append((address, format_function(address, signature)))
    for address, declaration in globals_ or []:
        new_lines.append((address, format_global(address, declaration)))
    for declaration in typedefs or []:
        new_lines.append((None, declaration.strip()))

    for _, line in new_lines:
        problem = line_problem(line)
        if problem:
            raise ValueError(f"refusing to write unparseable entry: {line!r} — {problem}")

    target = Path(path)
    existing = (target.read_text(encoding="utf-8", errors="replace").splitlines()
                if target.is_file() else [])
    by_address: dict[int, int] = {}
    for index, line in enumerate(existing):
        stripped = line.split("//", 1)[0].strip()
        if stripped.startswith(("@ ", "$ ")):
            try:
                by_address[int(stripped[2:].split(None, 1)[0], 16)] = index
            except (ValueError, IndexError):
                continue
    present = {line.split("//", 1)[0].strip() for line in existing}

    added = replaced = skipped = 0
    for address, line in new_lines:
        if address is None:
            if line in present:
                skipped += 1
                continue
            existing.append(line)
            present.add(line)
            added += 1
        elif address in by_address:
            if not replace or existing[by_address[address]] == line:
                skipped += 1
                continue
            existing[by_address[address]] = line
            replaced += 1
        else:
            by_address[address] = len(existing)
            existing.append(line)
            added += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(existing) + "\n", encoding="utf-8")
    return {"path": str(target), "added": added, "replaced": replaced,
            "skipped": skipped}


# ── CLI ────────────────────────────────────────────────────────────────────

def _hex(value: str) -> int:
    return int(value, 16)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m retools.kb",
        description="Inspect, validate and extend a kb.h knowledge base")
    sub = parser.add_subparsers(dest="cmd", required=True)

    show = sub.add_parser("show", help="Count the entries in a kb.h")
    show.add_argument("path")

    check = sub.add_parser("validate",
        help="Report lines that are not usable entries (dropped or malformed)")
    check.add_argument("path")

    add = sub.add_parser("add", help="Add or update entries")
    add.add_argument("path")
    add.add_argument("--func", nargs=2, action="append", default=[],
        metavar=("ADDR", "SIGNATURE"),
        help='e.g. --func 0x401000 "void __cdecl ProcessInput(int key)"')
    add.add_argument("--global", dest="globals_", nargs=2, action="append",
        default=[], metavar=("ADDR", "DECLARATION"),
        help='e.g. --global 0x7C5548 "Object* g_mainObject"')
    add.add_argument("--type", action="append", default=[], metavar="DECL",
        help='e.g. --type "struct Foo { int x; float y; };"')
    add.add_argument("--no-replace", action="store_true",
        help="Leave existing entries for the same address alone")

    args = parser.parse_args(argv)
    path = Path(args.path)

    if args.cmd == "show":
        if not path.is_file():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 1
        kb = parse_kb(path)
        print(f"{path}: {len(kb.functions)} function(s), "
              f"{len(kb.globals)} global(s), {len(kb.typedefs)} type line(s)")
        return 0

    if args.cmd == "validate":
        try:
            problems = validate(path)
        except FileNotFoundError:
            print(f"error: {path} does not exist", file=sys.stderr)
            return 1
        for number, line, problem in problems:
            print(f"{path}:{number}: {problem}\n    {line}")
        if problems:
            print(f"{len(problems)} line(s) will not become usable entries")
            return 1
        print(f"{path}: all lines parse")
        return 0

    try:
        result = add_entries(
            path,
            functions=[(_hex(a), s) for a, s in args.func],
            globals_=[(_hex(a), d) for a, d in args.globals_],
            typedefs=args.type,
            replace=not args.no_replace)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{result['path']}: {result['added']} added, "
          f"{result['replaced']} replaced, {result['skipped']} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
