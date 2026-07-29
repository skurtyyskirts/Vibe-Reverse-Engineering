"""Top-of-iteration recovery for an unattended porting run.

Every iteration starts by answering "is the game still there". The answer is
usually yes, and when it is not the response is mechanical: a crashed game
gets relaunched, an error dialog gets dismissed, a hung one gets killed and
relaunched. Doing that in code rather than in the agent's reasoning keeps the
loop's judgement for the parts that actually need it — reading a frame,
choosing the next Remix setting.

What is *not* mechanical stays here as a report: repeated crashes after the
same action mean the action is the problem, and that is a decision about the
port, not about process management.

Usage (CLI):
    python -m autonomy watchdog MyGame
    python -m autonomy watchdog MyGame --no-recover

Usage (library):
    from autonomy.watchdog import supervise
    result = supervise(run)
"""

from __future__ import annotations

from pathlib import Path

#: Verdicts a relaunch can fix. A hung game is killed first; a crashed one is
#: already gone or is sitting behind a dialog that gets dismissed.
RELAUNCHABLE = ("not-running", "crashed", "hung", "frozen")

#: Consecutive relaunches before the loop stops treating this as a blip. The
#: game dying this many times in a row is a finding about the port.
CRASH_LOOP_LIMIT = 3

CRASH_KEY = "watchdog:relaunch"


def supervise(run, recover: bool = True) -> dict:
    """Check the game's health and, if asked, put it back on its feet.

    Args:
        run:     The `PortRun` whose `game_dir`/`exe` identify the game.
        recover: Relaunch or unblock the game when it needs it. With
                 recover=False this only reports, which is what a diagnosis
                 step wants when the crash itself is the thing under study.

    Returns:
        dict with:
            verdict:   the health verdict before any recovery
            action:    what the watchdog did (`none`, `dismissed-dialog`,
                       `relaunched`, `crash-loop`)
            recovered: health verdict after recovery, if it ran
            healthy:   True when the game is usable now
            crash_loop: True when relaunches have hit CRASH_LOOP_LIMIT

    Raises:
        OSError: If not on Windows.
        FileNotFoundError: If the game executable is missing.
    """
    from livetools import health, procctl

    exe = run.data["exe"]
    game_dir = run.data["game_dir"]
    state = health.check(exe, game_dir=game_dir)
    result = {"verdict": state["verdict"], "reason": state["reason"],
              "action": "none", "recovered": None,
              "healthy": state["verdict"] == "ok", "crash_loop": False,
              "fatal_log_lines": state["fatal_log_lines"]}

    if result["healthy"] or not recover:
        return result

    if state["error_windows"]:
        # A modal dialog blocks every later input, so clearing it comes first
        # and may be all that was wrong.
        state = health.check(exe, game_dir=game_dir, dismiss_dialogs=True)
        result["action"] = "dismissed-dialog"
        result["recovered"] = state["verdict"]
        result["healthy"] = state["verdict"] == "ok"
        if result["healthy"]:
            run.step(action="watchdog dismissed a blocking dialog",
                     outcome="ok", key=CRASH_KEY,
                     evidence=result["reason"], conclusion="game usable again")
            return result

    if state["verdict"] not in RELAUNCHABLE:
        return result

    attempt = run.step(action=f"watchdog relaunching after {state['verdict']}",
                       outcome="fail", key=CRASH_KEY,
                       evidence="; ".join(state["fatal_log_lines"][:3])
                                or state["reason"],
                       conclusion=state["reason"])
    if attempt["attempts"] >= CRASH_LOOP_LIMIT:
        result.update(action="crash-loop", crash_loop=True)
        run.add_issue("crash-loop",
                      f"game reached {state['verdict']} "
                      f"{attempt['attempts']} times in a row",
                      evidence="; ".join(state["fatal_log_lines"][:3]))
        return result

    exe_path = Path(game_dir) / exe
    outcome = procctl.restart(exe_path)
    state = health.check(exe, game_dir=game_dir)
    result.update(action="relaunched", recovered=state["verdict"],
                  healthy=state["verdict"] == "ok",
                  restart_ok=outcome["ok"])
    if result["healthy"]:
        # The relaunch worked, so the crash was a blip; clear the budget so a
        # later unrelated crash gets its own three chances.
        run.step(action="watchdog relaunched the game", outcome="ok",
                 key=CRASH_KEY, conclusion="game back at a usable frame")
    return result
