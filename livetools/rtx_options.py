"""Offline reference for every dxvk-remix rtx.conf option.

The Remix runtime silently ignores an option it does not recognise: a typo in
rtx.conf costs a full game restart to discover, and looks exactly like "the
setting did not help". Validating a key before writing it turns that into an
immediate answer.

The same table is how an unattended run finds settings it was not told about.
Remix exposes ~1000 options; a hand-curated playbook covers the well-known
ones, and searching descriptions covers the rest without a web request.

The data ships as `data/rtx_options.tsv`, generated from dxvk-remix's own
auto-generated `RtxOptions.md`. Refresh it with `remix options sync` when
upgrading the runtime — options do get added and renamed between releases.

Usage (CLI):
    python -m livetools remix options search terrain
    python -m livetools remix options show rtx.uniqueObjectDistance
    python -m livetools remix options sync

Usage (library):
    from livetools import rtx_options
    rtx_options.lookup("rtx.zUp")
    rtx_options.search("decal", limit=10)
    rtx_options.is_known("rtx.uiTextures")
"""

from __future__ import annotations

from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "rtx_options.tsv"

RTX_OPTIONS_URL = ("https://raw.githubusercontent.com/NVIDIAGameWorks/"
                   "dxvk-remix/main/RtxOptions.md")

_FIELDS = ("name", "type", "default", "min", "max", "description")
_cache: dict[str, dict] | None = None


def load() -> dict[str, dict]:
    """Load the option table, keyed by option name.

    Returns:
        {name: {name, type, default, min, max, description}}. Empty if the
        data file is missing — callers treat that as "cannot validate", never
        as "the option is invalid".
    """
    global _cache
    if _cache is not None:
        return _cache
    options: dict[str, dict] = {}
    if DATA_FILE.is_file():
        for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < len(_FIELDS):
                parts += [""] * (len(_FIELDS) - len(parts))
            entry = dict(zip(_FIELDS, parts))
            options[entry["name"]] = entry
    _cache = options
    return options


def lookup(name: str) -> dict | None:
    """Return one option's metadata, or None if it is not in the table."""
    return load().get(name)


def is_known(name: str) -> bool:
    """True if the option exists upstream.

    Returns True when the table is unavailable, so a missing data file
    degrades to "no validation" rather than blocking every write.
    """
    table = load()
    return not table or name in table


def suggest(name: str, limit: int = 5) -> list[str]:
    """Closest known option names to a misspelled one.

    Ranked by shared prefix length then by name, which is what actually
    catches the common failures: a wrong suffix (`rtx.uiTexture`) or a wrong
    section (`rtx.antiCulling.enable`).
    """
    def shared_prefix(candidate: str) -> int:
        n = 0
        for a, b in zip(name.lower(), candidate.lower()):
            if a != b:
                break
            n += 1
        return n

    ranked = sorted(load(), key=lambda c: (-shared_prefix(c), c))
    return [c for c in ranked[:limit] if shared_prefix(c) > 4]


def validate_value(name: str, value: str) -> str | None:
    """Check a value against the option's declared type and range.

    A wrong-typed value is as invisible as a misspelled key: the runtime
    parses what it can, ignores what it cannot, and the setting silently does
    nothing. Booleans and numbers are checkable, so they get checked.

    Args:
        name:  Option name.
        value: The value about to be written.

    Returns:
        An explanation of what is wrong, or None if the value is acceptable
        (including when the option is unknown or its type is not checkable).
    """
    entry = lookup(name)
    if not entry:
        return None
    kind, text = entry["type"], value.strip()

    if kind == "bool":
        if text not in ("True", "False"):
            return f"expects True or False, got {value!r}"
        return None

    if kind == "hash set":
        for item in (h.strip() for h in text.split(",") if h.strip()):
            if not _is_hash(item):
                return (f"expects comma-separated 0x hashes, got {item!r} — "
                        "hashes come from `remix capture assets`")
        return None

    if kind in ("int", "float"):
        try:
            number = float(text)
        except ValueError:
            return f"expects a {kind}, got {value!r}"
        if kind == "int" and number != int(number):
            return f"expects an int, got {value!r}"
        for bound, compare in (("min", float.__lt__), ("max", float.__gt__)):
            limit = entry[bound]
            if limit and compare(number, float(limit)):
                return f"is out of range ({bound} {limit}), got {value!r}"
    return None


def _is_hash(text: str) -> bool:
    body = text[2:] if text[:2].lower() == "0x" else text
    return bool(body) and all(c in "0123456789abcdefABCDEF" for c in body)


def search(term: str, limit: int = 20) -> list[dict]:
    """Find options whose name or description mentions a term.

    Name matches rank above description matches, so searching "decal" leads
    with `rtx.decalTextures` rather than an unrelated option that happens to
    mention decals.

    Args:
        term:  Case-insensitive substring.
        limit: Maximum results.

    Returns:
        Matching option entries.
    """
    needle = term.lower()
    by_name, by_desc = [], []
    for entry in load().values():
        if needle in entry["name"].lower():
            by_name.append(entry)
        elif needle in entry["description"].lower():
            by_desc.append(entry)
    by_name.sort(key=lambda e: e["name"])
    by_desc.sort(key=lambda e: e["name"])
    return (by_name + by_desc)[:limit]


def sync(source: str | Path = RTX_OPTIONS_URL) -> int:
    """Regenerate the option table from dxvk-remix's RtxOptions.md.

    Args:
        source: URL or local path to RtxOptions.md.

    Returns:
        Number of options written.

    Raises:
        OSError: If the source cannot be read.
        ValueError: If it contains no option rows.
    """
    global _cache
    import re

    text = (Path(source).read_text(encoding="utf-8")
            if Path(str(source)).is_file() else _fetch(str(source)))

    rows = []
    for line in text.splitlines():
        if not line.startswith("|rtx."):
            continue
        parts = line.split("|")[1:]
        if len(parts) < 6:
            continue
        name, typ, default, low, high = (p.strip() for p in parts[:5])
        desc = "|".join(parts[5:]).rstrip("|")
        desc = re.sub(r"\s+", " ",
                      desc.replace("<br>", " ").replace("\\", "")).strip()
        rows.append((name, typ, default, low, high, desc))
    if not rows:
        raise ValueError(f"No option rows found in {source}")

    rows.sort()
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# dxvk-remix RTX option reference: "
                 "name\ttype\tdefault\tmin\tmax\tdescription\n")
        fh.write("# Generated from NVIDIAGameWorks/dxvk-remix RtxOptions.md.\n")
        fh.write("# Regenerate: python -m livetools remix options sync\n")
        for row in rows:
            fh.write("\t".join(row) + "\n")
    _cache = None
    return len(rows)


def _fetch(url: str) -> str:
    from urllib.request import urlopen

    with urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")
