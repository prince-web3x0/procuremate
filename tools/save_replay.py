"""Save a finished live run (search file + check file) as a replay bundle for demos.

Usage:
    python save_replay.py --search out/search.json --checks out/checks.json [--out path]

The default path is in the user cache folder, outside the repo. The bundle contains real businesses' public
contact details: never commit it, never attach it to a room.
"""
import argparse
import json
import sys

from replay import save_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--search", required=True)
    parser.add_argument("--checks", required=True)
    parser.add_argument("--out", help="where to save (default: the user cache folder, replays/replay-<date>.json)")
    args = parser.parse_args(argv)
    try:
        with open(args.search, encoding="utf-8") as a, open(args.checks, encoding="utf-8") as b:
            path = save_bundle(json.load(a), json.load(b), args.out)
    except (OSError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    print(f"Saved replay bundle: {path}\nIt holds real businesses' public contact details. Do not commit it or share it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
