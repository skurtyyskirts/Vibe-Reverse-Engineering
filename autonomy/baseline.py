"""Reference frames for a viewpoint, so regressions are caught not argued.

A port is a long series of Remix settings changes, each verified by looking at
one screenshot. That answers "does this look right now" but not "is this still
the scene I already got working" — and settings interact, so a fix for the sky
routinely breaks the lighting three iterations later. Without a reference frame
the regression is only found at the end, with a dozen changes to bisect.

A baseline is a frame captured at a known viewpoint and labelled. Later
captures of the same viewpoint are compared against it, which answers two
different questions with the same mechanism:

- **Did my change do anything?** Compare against the baseline taken just before
  it. A ratio at the noise floor means the setting did not take — a
  misspelled option, a missing restart, a debug view that never engaged.
- **Did my change break something that worked?** Compare against the last
  known-good baseline for that viewpoint.

Baselines belong to the run, not to the game directory, so they survive a
reinstall and travel with the journal.

Usage (CLI):
    python -m autonomy baseline MyGame --save ingame-lit --image screens/031_7_lit.png
    python -m autonomy baseline MyGame --check ingame-lit --image screens/044_7_after.png
    python -m autonomy baseline MyGame --list

Usage (library):
    from autonomy import baseline
    baseline.save(run, "ingame-lit", "screens/031_7_lit.png")
    baseline.check(run, "ingame-lit", "screens/044_7_after.png")
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: Below this changed-pixel ratio, two captures of a static viewpoint are the
#: same picture. Real renderers are never bit-identical frame to frame —
#: dithering, temporal accumulation and upscaler jitter all move pixels — so
#: an exact-zero test would report every comparison as a change.
NOISE_FLOOR = 0.002


def baseline_dir(run) -> Path:
    return run.root / "baselines"


def baseline_path(run, label: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
    return baseline_dir(run) / f"{safe}.png"


def save(run, label: str, image: str | Path, note: str = "") -> dict:
    """Record a capture as the reference frame for a viewpoint.

    Args:
        run:   The `PortRun` this baseline belongs to.
        label: Viewpoint name, e.g. `ingame-lit`. Re-saving replaces it.
        image: Capture to copy in.
        note:  What state the game was in — the settings that produced it.

    Returns:
        dict with `label`, `path` and `replaced`.

    Raises:
        FileNotFoundError: If the image does not exist.
    """
    source = Path(image)
    if not source.is_file():
        raise FileNotFoundError(f"No capture at {source}")
    target = baseline_path(run, label)
    replaced = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)

    baselines = run.data.setdefault("baselines", {})
    baselines[label] = {"path": str(target), "note": note,
                        "source": str(source)}
    run._save()
    run._append_journal(
        f"Baseline {'replaced' if replaced else 'saved'}: {label}",
        [f"reference: {target}", f"from: {source}", note])
    return {"label": label, "path": str(target), "replaced": replaced}


def check(run, label: str, image: str | Path,
          threshold: float = NOISE_FLOOR, tiles: tuple[int, int] = (4, 3)) -> dict:
    """Compare a capture against a saved baseline.

    Args:
        run:       The `PortRun` holding the baseline.
        label:     Viewpoint name.
        image:     Capture to compare.
        threshold: Ratio at or above which the frames count as different.
        tiles:     Grid for localizing the change (cols, rows).

    Returns:
        dict with:
            same:     True when the capture matches the baseline
            ratio:    changed-pixel ratio
            hottest:  the grid cell that changed most, or None
            verdict:  `unchanged` or `changed`
            baseline: the reference path

    Raises:
        FileNotFoundError: If the baseline or the capture is missing.
        ValueError: If the two captures are different sizes — a resolution
            change makes the comparison meaningless rather than merely large.
    """
    from livetools import screenshot as ss

    reference = baseline_path(run, label)
    if not reference.is_file():
        raise FileNotFoundError(
            f"No baseline {label!r} in {baseline_dir(run)} — save one first")
    current = Path(image)
    if not current.is_file():
        raise FileNotFoundError(f"No capture at {current}")

    ref_w, ref_h, ref_rgb = ss.decode_png(reference.read_bytes())
    cur_w, cur_h, cur_rgb = ss.decode_png(current.read_bytes())
    if (ref_w, ref_h) != (cur_w, cur_h):
        raise ValueError(
            f"Baseline {label!r} is {ref_w}x{ref_h} but the capture is "
            f"{cur_w}x{cur_h} — re-save the baseline at this resolution")

    delta = ss.diff_rgb(ref_w, ref_h, ref_rgb, cur_rgb)
    grid = ss.tiled_diff(ref_w, ref_h, ref_rgb, cur_rgb,
                         cols=tiles[0], rows=tiles[1])
    same = delta["ratio"] < threshold
    return {"label": label, "same": same,
            "verdict": "unchanged" if same else "changed",
            "ratio": delta["ratio"], "threshold": threshold,
            "bbox": delta["bbox"], "hottest": grid["hottest"],
            "baseline": str(reference), "capture": str(current)}


def listing(run) -> list[dict]:
    """Every saved baseline, with whether its file is still present."""
    return [{"label": label, **info,
             "present": Path(info["path"]).is_file()}
            for label, info in sorted(run.data.get("baselines", {}).items())]
