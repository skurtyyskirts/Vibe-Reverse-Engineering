"""Offline JSONL aggregation and query tool for livetools trace data.

Pure stdlib Python -- no Frida, no pefile, no external dependencies.
Reads JSONL files produced by ``collect`` or ``trace --output``.

Can be invoked as:
    python -m livetools analyze <file.jsonl> [options]
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


# ── field path resolution ────────────────────────────────────────────

def _resolve_field(record: dict, path: str) -> Any:
    """Resolve a dot-separated field path with optional array indices.

    Examples:
        "addr"                   -> record["addr"]
        "leave.eax"              -> record["leave"]["eax"]
        "enter.reads.0.value.0"  -> record["enter"]["reads"][0]["value"][0]
    """
    parts = path.split(".")
    cur: Any = record
    for p in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


# ── filter parsing ───────────────────────────────────────────────

def _parse_filter(expr: str):
    """Parse "field==value" / "field!=value" / "field>value" etc."""
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        idx = expr.find(op)
        if idx >= 0:
            field = expr[:idx].strip()
            val_str = expr[idx + len(op):].strip()
            try:
                val: Any = int(val_str, 16) if val_str.startswith("0x") else (
                    float(val_str) if "." in val_str else int(val_str))
            except ValueError:
                val = val_str
            return field, op, val
    return None, None, None


def _match_filter(record: dict, field: str, op: str, val: Any) -> bool:
    rv = _resolve_field(record, field)
    if rv is None:
        return False
    if isinstance(rv, str):
        try:
            rv = int(rv, 16)
        except ValueError:
            pass
    try:
        if op == "==": return rv == val
        if op == "!=": return rv != val
        if op == ">":  return rv > val
        if op == "<":  return rv < val
        if op == ">=": return rv >= val
        if op == "<=": return rv <= val
    except TypeError:
        pass
    return str(rv) == str(val) if op == "==" else str(rv) != str(val)


# ── JSONL reader ───────────────────────────────────────────────

def _load_records(path: str, filter_expr: str | None = None) -> list[dict]:
    records = []
    field, op, val = (None, None, None)
    if filter_expr:
        field, op, val = _parse_filter(filter_expr)

    try:
        import orjson
        json_loads = orjson.loads
        json_error = orjson.JSONDecodeError
    except ImportError:
        json_loads = json.loads
        json_error = json.JSONDecodeError

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json_loads(line)
            except json_error:
                continue
            if field and not _match_filter(rec, field, op, val):
                continue
            records.append(rec)
    return records


# ── analysis operations ────────────────────────────────────────────

def _summary(records: list[dict]) -> str:
    lines = []
    n = len(records)
    addrs = set()
    intervals = set()
    min_ts, max_ts = float("inf"), float("-inf")
    for r in records:
        a = r.get("addr")
        if a:
            addrs.add(a)
        iv = r.get("interval")
        if iv is not None:
            intervals.add(iv)
        ts = r.get("ts")
        if ts is not None:
            if ts < min_ts:
                min_ts = ts
            if ts > max_ts:
                max_ts = ts

    span = (max_ts - min_ts) / 1000.0 if max_ts > min_ts else 0.0
    lines.append(f"Records:    {n}")
    lines.append(f"Addresses:  {len(addrs)}  {sorted(addrs)}")
    lines.append(f"Intervals:  {len(intervals)}  (max={max(intervals) if intervals else 0})")
    lines.append(f"Time span:  {span:.3f}s")

    for a in sorted(addrs):
        cnt = sum(1 for r in records if r.get("addr") == a)
        label = ""
        for r in records:
            if r.get("addr") == a and r.get("label"):
                label = f" [{r['label']}]"
                break
        lines.append(f"  {a}: {cnt} records{label}")
    return "\n".join(lines)


def _group_by(records: list[dict], field: str, top: int) -> str:
    counter: Counter = Counter()
    for r in records:
        val = _resolve_field(r, field)
        key = str(val) if val is not None else "<null>"
        counter[key] += 1

    lines = [f"Group by: {field}  ({len(counter)} unique values)", ""]
    total = len(records)
    for key, cnt in counter.most_common(top):
        pct = cnt / total * 100 if total else 0
        bar = "#" * int(pct / 2)
        lines.append(f"  {key:<30s}  {cnt:>8d}  ({pct:5.1f}%)  {bar}")
    if len(counter) > top:
        lines.append(f"  ... and {len(counter) - top} more")
    return "\n".join(lines)


def _cross_tab(records: list[dict], f1: str, f2: str, top: int) -> str:
    table: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        v1 = str(_resolve_field(r, f1) or "<null>")
        v2 = str(_resolve_field(r, f2) or "<null>")
        table[v1][v2] += 1

    all_v2 = sorted({v2 for counts in table.values() for v2 in counts})
    if len(all_v2) > 20:
        all_v2 = all_v2[:20]

    lines = [f"Cross-tab: {f1} x {f2}", ""]
    header = f"  {'':30s}" + "".join(f"  {v:>10s}" for v in all_v2)
    lines.append(header)
    lines.append("  " + "-" * (30 + 12 * len(all_v2)))

    sorted_keys = sorted(table.keys(), key=lambda k: -sum(table[k].values()))
    for k in sorted_keys[:top]:
        row = f"  {k:<30s}"
        for v2 in all_v2:
            row += f"  {table[k].get(v2, 0):>10d}"
        lines.append(row)
    return "\n".join(lines)


def _show_interval(records: list[dict], interval_id: int) -> str:
    subset = [r for r in records if r.get("interval") == interval_id]
    lines = [f"Interval {interval_id}: {len(subset)} records", ""]
    addr_counts: Counter = Counter()
    for r in subset:
        addr_counts[r.get("addr", "?")] += 1
    for a, c in addr_counts.most_common():
        label = ""
        for r in subset:
            if r.get("addr") == a and r.get("label"):
                label = f" [{r['label']}]"
                break
        lines.append(f"  {a}{label}: {c} calls")
    lines.append("")

    for i, r in enumerate(subset[:50]):
        addr = r.get("addr", "?")
        caller = r.get("caller", "?")
        leave = r.get("leave", {})
        retval = leave.get("retval", leave.get("eax", "?"))
        lines.append(f"  #{i+1}  {addr}  caller={caller}  ret={retval}")
    if len(subset) > 50:
        lines.append(f"  ... ({len(subset) - 50} more)")
    return "\n".join(lines)


def _show_intervals_range(records: list[dict], range_str: str) -> str:
    parts = range_str.split(":")
    if len(parts) != 2:
        return "Invalid range format. Use N:M"
    try:
        lo, hi = int(parts[0]), int(parts[1])
    except ValueError:
        return "Invalid range format. Use N:M"

    by_iv: dict[int, int] = Counter()
    total = 0
    for r in records:
        # Original logic: lo <= (r.get("interval", -1) or -1) <= hi
        val = r.get("interval", -1)
        filter_val = val or -1
        if lo <= filter_val <= hi:
            # Original counting logic: by_iv[r.get("interval", 0)] += 1
            count_val = r.get("interval", 0)
            by_iv[count_val] += 1
            total += 1

    lines = [f"Intervals {lo}..{hi}: {total} records", ""]
    for iv in sorted(by_iv):
        lines.append(f"  Interval {iv}: {by_iv[iv]} records")
    return "\n".join(lines)


def _compare_intervals(records: list[dict], a: int, b: int) -> str:
    ca: Counter = Counter()
    cb: Counter = Counter()
    for r in records:
        iv = r.get("interval")
        if iv == a:
            ca[r.get("addr", "?")] += 1
        if iv == b:
            cb[r.get("addr", "?")] += 1

    all_addrs = sorted(set(ca.keys()) | set(cb.keys()))
    lines = [f"Compare interval {a} vs {b}", ""]
    lines.append(f"  {'Address':<20s}  {'Int '+str(a):>10s}  {'Int '+str(b):>10s}  {'Delta':>10s}")
    lines.append(f"  {'-'*20}  {'-'*10}  {'-'*10}  {'-'*10}")
    for addr in all_addrs:
        va, vb = ca.get(addr, 0), cb.get(addr, 0)
        delta = vb - va
        sign = "+" if delta > 0 else ""
        lines.append(f"  {addr:<20s}  {va:>10d}  {vb:>10d}  {sign}{delta:>9d}")
    lines.append("")
    total_a = sum(ca.values())
    total_b = sum(cb.values())
    lines.append(f"  Total: {total_a} vs {total_b} records")
    return "\n".join(lines)


def _histogram(records: list[dict], field: str) -> str:
    values = []
    for r in records:
        v = _resolve_field(r, field)
        if v is not None:
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                pass

    if not values:
        return f"No numeric values found for field: {field}"

    lo, hi = min(values), max(values)
    n_bins = min(20, max(5, int(math.sqrt(len(values)))))
    if lo == hi:
        return f"All {len(values)} values = {lo}"

    bin_width = (hi - lo) / n_bins
    bins = [0] * n_bins
    for v in values:
        idx = min(int((v - lo) / bin_width), n_bins - 1)
        bins[idx] += 1

    max_count = max(bins) if bins else 1
    lines = [f"Histogram: {field}  ({len(values)} values, {n_bins} bins)", ""]
    for i, cnt in enumerate(bins):
        edge = lo + i * bin_width
        bar_len = int(cnt / max_count * 40) if max_count else 0
        bar = "#" * bar_len
        lines.append(f"  {edge:12.4f} | {cnt:>6d} {bar}")
    lines.append(f"  {hi:12.4f} |")
    lines.append("")
    lines.append(f"  min={lo:.6f}  max={hi:.6f}  "
                 f"mean={sum(values)/len(values):.6f}  "
                 f"median={sorted(values)[len(values)//2]:.6f}")
    return "\n".join(lines)


def _export_csv(records: list[dict], csv_path: str, filter_expr: str | None) -> str:
    if not records:
        return "No records to export."

    flat_keys = set()
    for r in records:
        flat_keys.update(_flatten(r).keys())
    keys = sorted(flat_keys)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow(_flatten(r))

    return f"Exported {len(records)} records to {csv_path} ({len(keys)} columns)"


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(_flatten(item, f"{key}.{i}"))
                else:
                    out[f"{key}.{i}"] = item
        else:
            out[key] = v
    return out


# ── entry point ───────────────────────────────────────────────

def run_analyze(args) -> None:
    fpath = args.file
    if not Path(fpath).exists():
        print(f"[error] File not found: {fpath}", file=sys.stderr)
        return

    filter_expr = getattr(args, "filter", None)
    records = _load_records(fpath, filter_expr)

    if not records:
        print("No records loaded (file empty or all filtered out).")
        return

    acted = False

    if getattr(args, "summary", False):
        print(_summary(records))
        acted = True

    if getattr(args, "group_by", None):
        if acted:
            print()
        print(_group_by(records, args.group_by, getattr(args, "top", 20)))
        acted = True

    if getattr(args, "cross_tab", None):
        if acted:
            print()
        print(_cross_tab(records, args.cross_tab[0], args.cross_tab[1],
                         getattr(args, "top", 20)))
        acted = True

    if getattr(args, "interval", None) is not None:
        if acted:
            print()
        print(_show_interval(records, args.interval))
        acted = True

    if getattr(args, "intervals", None):
        if acted:
            print()
        print(_show_intervals_range(records, args.intervals))
        acted = True

    if getattr(args, "compare_intervals", None):
        if acted:
            print()
        print(_compare_intervals(records, args.compare_intervals[0],
                                 args.compare_intervals[1]))
        acted = True

    if getattr(args, "histogram", None):
        if acted:
            print()
        print(_histogram(records, args.histogram))
        acted = True

    if getattr(args, "export_csv", None):
        if acted:
            print()
        print(_export_csv(records, args.export_csv, filter_expr))
        acted = True

    if not acted:
        print(_summary(records))
