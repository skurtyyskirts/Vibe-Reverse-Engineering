"""Resume-safe state for an unattended RTX Remix porting run.

An autonomous port is a long sequence of small verified steps spread across
many agent iterations, game restarts and (inevitably) crashes. Nothing about
that survives in an agent's context window, so it lives on disk instead:

    patches/<Game>/autonomy/
        state.json      phase progress, attempt counters, open issues, next action
        journal.md      append-only log: what was done, the evidence, the conclusion
        screens/        NNN_<phase>_<label>.png, numbered monotonically

`PortRun` is the only writer. Every mutation persists immediately and
atomically, so a run killed mid-step resumes from the last completed step
rather than from the beginning.

Two invariants make the loop terminate instead of spinning forever:

- Every step records an outcome against an *action key*. Repeated failures of
  the same key increment a counter; once it reaches `ATTEMPT_LIMIT` the key is
  exhausted and the loop must try a different approach or stop.
- A phase only advances through `complete_phase`, which requires the gate
  evidence that proves it passed.

Usage (CLI):
    python -m autonomy init MyGame --game-dir "C:/Games/MyGame" --exe game.exe
    python -m autonomy status MyGame
    python -m autonomy shot-path MyGame boot
    python -m autonomy step MyGame --action "sent RETURN at title" \\
        --key nav:title --outcome ok --evidence screens/003_2_title.png \\
        --conclusion "reached main menu" --next "select Play"
    python -m autonomy phase MyGame --complete 2 --gate "screens/003_2_title.png"
    python -m autonomy issue MyGame --add unstable-hud-hash \\
        --summary "HUD hashes churn" --evidence screens/012_5_flicker.png
    python -m autonomy report MyGame

Usage (library):
    from autonomy import PortRun
    run = PortRun.open("MyGame")
    if run.exhausted("nav:title"):
        ...
    run.step(action="...", key="nav:title", outcome="ok", evidence="...")
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

SCHEMA_VERSION = 1

#: Repeated failures of one action key past this count mean the approach is
#: wrong, not unlucky — the loop must change tactics or stop.
ATTEMPT_LIMIT = 3

PHASES: dict[int, str] = {
    0: "preflight",
    1: "static-bootstrap",
    2: "launch",
    3: "navigate",
    4: "remix-baseline",
    5: "hash-stability",
    6: "ffp-conversion",
    7: "render-correctness",
    8: "report",
}

PHASE_STATUSES = ("pending", "in_progress", "done", "skipped", "blocked")
OUTCOMES = ("ok", "fail", "info")

_PATCHES = Path("patches")


def phase_name(index: int) -> str:
    """Return the short name for a phase index, or 'unknown-<n>'."""
    return PHASES.get(index, f"unknown-{index}")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class PortRun:
    """The durable state of one game's porting run.

    Attributes:
        root: `patches/<Game>/autonomy` — the run's directory.
        data: Parsed `state.json` contents. Treat as read-only; mutate through
            methods so changes are journalled and persisted.
    """

    def __init__(self, root: str | Path, data: dict):
        self.root = Path(root)
        self.data = data

    # ── construction ──────────────────────────────────────────────────────

    @staticmethod
    def root_for(game: str, patches_dir: str | Path = _PATCHES) -> Path:
        return Path(patches_dir) / game / "autonomy"

    @classmethod
    def create(cls, game: str, game_dir: str, exe: str,
               patches_dir: str | Path = _PATCHES,
               goal: str = "") -> "PortRun":
        """Create a fresh run workspace.

        Args:
            game: Project name; also the `patches/<name>/` folder.
            game_dir: Directory the game runs from (holds rtx.conf and logs).
            exe: Executable file name, e.g. `game.exe`.
            patches_dir: Root of the per-game workspace tree.
            goal: Optional free-text success criterion for the final report.

        Returns:
            The new run, already persisted.

        Raises:
            FileExistsError: If a run already exists for this game.
        """
        root = cls.root_for(game, patches_dir)
        if (root / "state.json").exists():
            raise FileExistsError(
                f"{root / 'state.json'} exists — open it instead of recreating")
        data = {
            "schema_version": SCHEMA_VERSION,
            "game": game,
            "game_dir": game_dir,
            "exe": exe,
            "goal": goal,
            "created": _now(),
            "updated": _now(),
            "phase": 0,
            "phases": {str(i): "pending" for i in PHASES},
            "gates": {},
            "next_action": "Phase 0 preflight: verify_install.py, remix status",
            "attempts": {},
            "issues": [],
            "shot_seq": 0,
            "steps": 0,
            "finished": None,
        }
        run = cls(root, data)
        (root / "screens").mkdir(parents=True, exist_ok=True)
        run._save()
        run._append_journal(f"## Run created — {game}",
                            [f"game_dir: `{game_dir}`", f"exe: `{exe}`"]
                            + ([f"goal: {goal}"] if goal else []))
        return run

    @classmethod
    def open(cls, game: str, patches_dir: str | Path = _PATCHES) -> "PortRun":
        """Load an existing run.

        Raises:
            FileNotFoundError: If no run exists for this game.
        """
        root = cls.root_for(game, patches_dir)
        path = root / "state.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"No run at {path} — create it with `python -m autonomy init`")
        return cls(root, json.loads(path.read_text(encoding="utf-8")))

    # ── persistence ───────────────────────────────────────────────────────

    def _save(self) -> None:
        """Persist state.json atomically so a killed run never loses it."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = _now()
        target = self.root / "state.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)

    def _append_journal(self, heading: str, lines: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"- {ln}" for ln in lines if ln)
        entry = f"\n### {_now()} — {heading}\n{body}\n" if body \
            else f"\n### {_now()} — {heading}\n"
        with (self.root / "journal.md").open("a", encoding="utf-8") as fh:
            fh.write(entry)

    # ── phases ────────────────────────────────────────────────────────────

    @property
    def phase(self) -> int:
        return int(self.data["phase"])

    def phase_status(self, index: int) -> str:
        return self.data["phases"].get(str(index), "pending")

    def start_phase(self, index: int) -> None:
        """Mark a phase in progress and make it the current phase."""
        self.data["phase"] = index
        self.data["phases"][str(index)] = "in_progress"
        self._save()
        self._append_journal(f"Phase {index} ({phase_name(index)}) started", [])

    def complete_phase(self, index: int, gate: str) -> int:
        """Record a phase as passed and move to the next pending phase.

        Args:
            index: Phase that passed.
            gate: Evidence that it passed — a screenshot path, log excerpt or
                analyzer output. Required: a phase without evidence is a claim,
                not a result.

        Returns:
            The new current phase index.

        Raises:
            ValueError: If `gate` is empty.
        """
        if not gate.strip():
            raise ValueError(f"Phase {index} needs gate evidence to complete")
        self.data["phases"][str(index)] = "done"
        self.data["gates"][str(index)] = {"evidence": gate, "at": _now()}
        nxt = next((i for i in sorted(PHASES)
                    if self.data["phases"].get(str(i)) in ("pending", "in_progress")),
                   index)
        self.data["phase"] = nxt
        self._save()
        self._append_journal(
            f"Phase {index} ({phase_name(index)}) PASSED",
            [f"gate evidence: {gate}", f"now at phase {nxt} ({phase_name(nxt)})"])
        return nxt

    def set_phase_status(self, index: int, status: str, note: str = "") -> None:
        """Mark a phase skipped or blocked (use `complete_phase` for passes).

        Raises:
            ValueError: On an unknown status.
        """
        if status not in PHASE_STATUSES:
            raise ValueError(f"Unknown phase status {status!r}")
        self.data["phases"][str(index)] = status
        self._save()
        self._append_journal(
            f"Phase {index} ({phase_name(index)}) → {status}", [note])

    # ── steps and attempt budgeting ───────────────────────────────────────

    def attempts(self, key: str) -> int:
        """Consecutive failures recorded against an action key."""
        return int(self.data["attempts"].get(key, 0))

    def exhausted(self, key: str, limit: int = ATTEMPT_LIMIT) -> bool:
        """True when this approach has failed enough times to abandon it."""
        return self.attempts(key) >= limit

    def exhausted_keys(self, limit: int = ATTEMPT_LIMIT) -> list[str]:
        return [k for k, n in self.data["attempts"].items() if int(n) >= limit]

    def step(self, action: str, outcome: str = "info", key: str = "",
             evidence: str = "", conclusion: str = "",
             next_action: str = "") -> dict:
        """Record one completed step of the loop.

        A step is a single verifiable unit — "sent RETURN and screenshotted the
        result", never "set up the game". Recording it journals the action,
        updates the attempt counter for `key`, and sets what to do next.

        Args:
            action: What was done, imperatively and specifically.
            outcome: `ok` clears the key's failure counter, `fail` increments
                it, `info` leaves it alone.
            key: Action key this step belongs to, e.g. `nav:title`. Steps
                sharing a key share a failure budget.
            evidence: Path or excerpt backing the conclusion.
            conclusion: What the evidence showed.
            next_action: What the next iteration should do.

        Returns:
            dict with the key's `attempts` count and whether it is `exhausted`.

        Raises:
            ValueError: On an unknown outcome.
        """
        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown outcome {outcome!r}, expected {OUTCOMES}")
        if key:
            if outcome == "fail":
                self.data["attempts"][key] = self.attempts(key) + 1
            elif outcome == "ok":
                self.data["attempts"].pop(key, None)
        self.data["steps"] = int(self.data.get("steps", 0)) + 1
        if next_action:
            self.data["next_action"] = next_action
        self._save()

        marker = {"ok": "OK", "fail": "FAIL", "info": "--"}[outcome]
        detail = [f"phase {self.phase} ({phase_name(self.phase)})"]
        if key:
            detail.append(f"key: `{key}` (failures: {self.attempts(key)})")
        if evidence:
            detail.append(f"evidence: {evidence}")
        if conclusion:
            detail.append(f"conclusion: {conclusion}")
        if next_action:
            detail.append(f"next: {next_action}")
        self._append_journal(f"[{marker}] {action}", detail)

        return {"key": key, "attempts": self.attempts(key) if key else 0,
                "exhausted": self.exhausted(key) if key else False}

    # ── issues ────────────────────────────────────────────────────────────

    def add_issue(self, issue_id: str, summary: str, evidence: str = "") -> dict:
        """Record an open problem, or refresh an existing one's evidence."""
        for issue in self.data["issues"]:
            if issue["id"] == issue_id:
                issue.update(summary=summary, status="open")
                if evidence:
                    issue["evidence"] = evidence
                self._save()
                return issue
        issue = {"id": issue_id, "summary": summary, "evidence": evidence,
                 "status": "open", "opened": _now()}
        self.data["issues"].append(issue)
        self._save()
        self._append_journal(f"Issue opened: {issue_id}",
                             [summary, f"evidence: {evidence}" if evidence else ""])
        return issue

    def resolve_issue(self, issue_id: str, resolution: str) -> dict:
        """Close an open issue.

        Raises:
            KeyError: If no issue has that id.
        """
        for issue in self.data["issues"]:
            if issue["id"] == issue_id:
                issue.update(status="resolved", resolution=resolution,
                             closed=_now())
                self._save()
                self._append_journal(f"Issue resolved: {issue_id}", [resolution])
                return issue
        raise KeyError(f"No issue {issue_id!r} in this run")

    def open_issues(self) -> list[dict]:
        return [i for i in self.data["issues"] if i["status"] == "open"]

    # ── screenshots ───────────────────────────────────────────────────────

    def shot_path(self, label: str, phase: int | None = None) -> Path:
        """Reserve the next numbered screenshot path.

        Numbering is owned here so screenshots stay monotonic and journal
        references never collide across iterations.

        Returns:
            `screens/NNN_<phase>_<label>.png` (directory created).
        """
        seq = int(self.data.get("shot_seq", 0)) + 1
        self.data["shot_seq"] = seq
        self._save()
        ph = self.phase if phase is None else phase
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
        shots = self.root / "screens"
        shots.mkdir(parents=True, exist_ok=True)
        return shots / f"{seq:03d}_{ph}_{safe}.png"

    # ── reporting ─────────────────────────────────────────────────────────

    def finish(self, verdict: str, summary: str) -> None:
        """Close the run out with a verdict the user can act on."""
        self.data["finished"] = {"verdict": verdict, "summary": summary,
                                 "at": _now()}
        self._save()
        self._append_journal(f"Run finished — {verdict}", [summary])

    def status(self) -> dict:
        """Everything an iteration needs to decide its next step."""
        return {
            "game": self.data["game"],
            "game_dir": self.data["game_dir"],
            "exe": self.data["exe"],
            "phase": self.phase,
            "phase_name": phase_name(self.phase),
            "phases": {k: v for k, v in sorted(self.data["phases"].items(),
                                               key=lambda kv: int(kv[0]))},
            "next_action": self.data["next_action"],
            "steps": self.data.get("steps", 0),
            "attempts": self.data["attempts"],
            "exhausted_keys": self.exhausted_keys(),
            "open_issues": self.open_issues(),
            "finished": self.data.get("finished"),
            "root": str(self.root),
        }

    def report(self) -> str:
        """Render the run as a markdown report."""
        d = self.data
        lines = [f"# RTX Remix port — {d['game']}", "",
                 f"- Game dir: `{d['game_dir']}`",
                 f"- Executable: `{d['exe']}`",
                 f"- Started: {d['created']}  ·  Last update: {d['updated']}",
                 f"- Steps recorded: {d.get('steps', 0)}"]
        if d.get("goal"):
            lines.append(f"- Goal: {d['goal']}")
        fin = d.get("finished")
        if fin:
            lines += ["", f"**Verdict: {fin['verdict']}** ({fin['at']})",
                      "", fin["summary"]]

        lines += ["", "## Phases", "", "| # | Phase | Status | Gate evidence |",
                  "|---|-------|--------|---------------|"]
        for i in sorted(PHASES):
            gate = d["gates"].get(str(i), {}).get("evidence", "")
            lines.append(f"| {i} | {phase_name(i)} | "
                         f"{d['phases'].get(str(i), 'pending')} | {gate} |")

        open_issues = self.open_issues()
        resolved = [i for i in d["issues"] if i["status"] != "open"]
        lines += ["", "## Open issues", ""]
        lines += ([f"- **{i['id']}** — {i['summary']} "
                   f"({i.get('evidence') or 'no evidence'})" for i in open_issues]
                  or ["_none_"])
        if resolved:
            lines += ["", "## Resolved issues", ""]
            lines += [f"- **{i['id']}** — {i['summary']} → "
                      f"{i.get('resolution', '')}" for i in resolved]

        exhausted = self.exhausted_keys()
        if exhausted:
            lines += ["", "## Abandoned approaches", "",
                      "Action keys that hit the attempt limit — these were "
                      "tried and did not work:", ""]
            lines += [f"- `{k}` ({d['attempts'][k]} failures)" for k in exhausted]

        lines += ["", "## Evidence", "",
                  f"- Journal: `{self.root / 'journal.md'}`",
                  f"- Screenshots: `{self.root / 'screens'}` "
                  f"({d.get('shot_seq', 0)} captured)", ""]
        return "\n".join(lines)
