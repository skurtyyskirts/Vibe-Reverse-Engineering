"""CLI for the unattended porting run state machine.

Every harness drives this the same way: shell out per iteration, read JSON.

    python -m autonomy init MyGame --game-dir "C:/Games/MyGame" --exe game.exe
    python -m autonomy status MyGame
    python -m autonomy shot-path MyGame boot
    python -m autonomy step MyGame --action "..." --key nav:title --outcome fail
    python -m autonomy phase MyGame --complete 2 --gate screens/003_2_title.png
    python -m autonomy watchdog MyGame
    python -m autonomy report MyGame --out patches/MyGame/findings.md

Exit codes: 0 succeeded, 1 the command or the game failed, 2 bad arguments,
3 the loop must change approach — `step` when an action key hit its attempt
limit, `watchdog` when the game is in a crash loop.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .state import ATTEMPT_LIMIT, PHASE_STATUSES, PortRun

EXIT_EXHAUSTED = 3


def _emit(payload: dict, as_json: bool = True) -> None:
    print(json.dumps(payload, indent=2) if as_json else payload)


def cmd_init(args: argparse.Namespace) -> int:
    run = PortRun.create(args.game, game_dir=args.game_dir, exe=args.exe,
                         patches_dir=args.patches, goal=args.goal or "")
    _emit(run.status())
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    _emit(run.status())
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    result = run.step(action=args.action, outcome=args.outcome,
                      key=args.key or "", evidence=args.evidence or "",
                      conclusion=args.conclusion or "",
                      next_action=args.next_action or "")
    result["phase"] = run.phase
    result["limit"] = ATTEMPT_LIMIT
    _emit(result)
    return EXIT_EXHAUSTED if result["exhausted"] else 0


def cmd_phase(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    if args.start is not None:
        run.start_phase(args.start)
    elif args.complete is not None:
        run.complete_phase(args.complete, gate=args.gate)
    else:
        run.set_phase_status(args.set_phase[0], args.set_phase[1],
                             note=args.note or "")
    _emit(run.status())
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    if args.add:
        _emit(run.add_issue(args.add, summary=args.summary,
                            evidence=args.evidence or ""))
    elif args.resolve:
        _emit(run.resolve_issue(args.resolve, resolution=args.resolution))
    else:
        _emit({"issues": run.data["issues"]})
    return 0


def cmd_shot_path(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    print(run.shot_path(args.label, phase=args.phase))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    run.finish(args.verdict, args.summary)
    _emit(run.status())
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    from .watchdog import supervise

    run = PortRun.open(args.game, patches_dir=args.patches)
    try:
        result = supervise(run, recover=not args.no_recover)
    except (OSError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _emit(result)
    if result["crash_loop"]:
        return EXIT_EXHAUSTED
    return 0 if result["healthy"] else 1


def cmd_report(args: argparse.Namespace) -> int:
    run = PortRun.open(args.game, patches_dir=args.patches)
    text = run.report()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(out)
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m autonomy",
        description="Resume-safe state machine for unattended RTX Remix ports")
    p.add_argument("--patches", default="patches",
                   help="Per-game workspace root (default: patches)")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Create a run workspace")
    init.add_argument("game")
    init.add_argument("--game-dir", required=True,
                      help="Directory the game runs from (holds rtx.conf)")
    init.add_argument("--exe", required=True, help="Executable name, e.g. game.exe")
    init.add_argument("--goal", help="Success criterion for the final report")
    init.set_defaults(func=cmd_init)

    st = sub.add_parser("status", help="Phase, next action, attempts, open issues")
    st.add_argument("game")
    st.set_defaults(func=cmd_status)

    step = sub.add_parser("step", help="Record one verified step of the loop")
    step.add_argument("game")
    step.add_argument("--action", required=True, help="What was done")
    step.add_argument("--outcome", choices=("ok", "fail", "info"), default="info")
    step.add_argument("--key", help="Action key sharing a failure budget, e.g. nav:title")
    step.add_argument("--evidence", help="Screenshot path or log excerpt")
    step.add_argument("--conclusion", help="What the evidence showed")
    step.add_argument("--next", dest="next_action", help="Next iteration's action")
    step.set_defaults(func=cmd_step)

    ph = sub.add_parser("phase", help="Start, complete or override a phase")
    ph.add_argument("game")
    grp = ph.add_mutually_exclusive_group(required=True)
    grp.add_argument("--start", type=int, metavar="N")
    grp.add_argument("--complete", type=int, metavar="N")
    grp.add_argument("--set", dest="set_phase", nargs=2,
                     metavar=("N", "STATUS"),
                     help=f"Force a status: {'/'.join(PHASE_STATUSES)}")
    ph.add_argument("--gate", default="",
                    help="Evidence proving the phase passed (required with --complete)")
    ph.add_argument("--note", help="Reason, for --set")
    ph.set_defaults(func=cmd_phase)

    iss = sub.add_parser("issue", help="Track unresolved problems")
    iss.add_argument("game")
    iss.add_argument("--add", metavar="ID")
    iss.add_argument("--summary", default="")
    iss.add_argument("--evidence")
    iss.add_argument("--resolve", metavar="ID")
    iss.add_argument("--resolution", default="")
    iss.set_defaults(func=cmd_issue)

    shot = sub.add_parser("shot-path", help="Reserve the next screenshot path")
    shot.add_argument("game")
    shot.add_argument("label", help="Short label, e.g. title-screen")
    shot.add_argument("--phase", type=int, help="Override the phase in the name")
    shot.set_defaults(func=cmd_shot_path)

    wd = sub.add_parser("watchdog",
        help="Check the game is alive and put it back up if it is not")
    wd.add_argument("game")
    wd.add_argument("--no-recover", action="store_true",
        help="Report health without relaunching or dismissing dialogs")
    wd.set_defaults(func=cmd_watchdog)

    fin = sub.add_parser("finish", help="Close the run with a verdict")
    fin.add_argument("game")
    fin.add_argument("--verdict", required=True,
                     help="e.g. success / partial / blocked")
    fin.add_argument("--summary", required=True)
    fin.set_defaults(func=cmd_finish)

    rep = sub.add_parser("report", help="Render the run as markdown")
    rep.add_argument("game")
    rep.add_argument("--out", help="Write to this file instead of stdout")
    rep.set_defaults(func=cmd_report)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "phase" and args.complete is not None and not args.gate:
        print("error: --complete requires --gate evidence", file=sys.stderr)
        return 2
    if args.cmd == "issue" and args.add and not args.summary:
        print("error: --add requires --summary", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except (FileNotFoundError, FileExistsError, KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
