"""Turn replay on or off for every tool and both agents with a switch file in the shared user cache folder.

Usage:
    python replay_ctl.py on <bundle>    # replay a saved live run: no Brave query, no site fetched
    python replay_ctl.py off
    python replay_ctl.py status

An environment variable in your shell may never reach the agents' commands; this file does, because Scout and Vera run as
the same user and read the same folder. Precedence in the tools: --replay, then REPLAY_FILE, then this switch file.
"""
import sys

from replay import status, switch_file, turn_off, turn_on, warning_text


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    sys.stdout.reconfigure(encoding="utf-8")
    if args[:1] == ["on"] and len(args) == 2:
        try:
            bundle = turn_on(args[1])
        except ValueError as err:
            print(f"Error: {err}. Replay was NOT turned on.", file=sys.stderr)
            return 2
        print(warning_text(str(bundle["saved_at"])[:10], str(switch_file().read_text(encoding="utf-8").strip())))
        return 0
    if args == ["off"]:
        was_on = turn_off()
        print("Replay switch file removed. The tools now use live data again." if was_on else "Replay was not on through the switch file.")
        s = status()
        if s["on"]:
            print(f"WARNING: replay is still on because REPLAY_FILE is set in this shell ({s['bundle']}). Unset that variable.")
        return 0
    if args == ["status"]:
        s = status()
        if not s["on"]:
            print("Replay is OFF: the tools use live data.")
            return 0
        if s["problem"]:
            print(f"REPLAY IS ON ({s['via']}) but the bundle can't be used: {s['problem']}. The tools will stop with an error, not go live. "
                  "Fix with: python tools/replay_ctl.py off")
            return 1
        print(warning_text(s["saved_on"], s["bundle"]) + f" (switched on by {s['via']})")
        return 0
    print("usage: replay_ctl.py on <bundle> | off | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
