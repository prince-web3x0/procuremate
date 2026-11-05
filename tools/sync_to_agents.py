"""Copy tools/ into the working folders of your Switch agents. Never copies secrets or caches.

Switch Console runs each agent in its own working directory, and the agent calls `python tools/...` from
there. Tell this script where those folders are, either way:

    python tools/sync_to_agents.py --dest /path/to/scout /path/to/vera [--dry-run]
    PROCUREMATE_AGENT_DIRS="/path/to/scout:/path/to/vera" python tools/sync_to_agents.py

(PROCUREMATE_AGENT_DIRS is separated by ';' on Windows and ':' elsewhere.) Each destination gets a `tools`
subfolder. A destination that does not exist is skipped with a message.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
AGENT_DIRS_ENV = "PROCUREMATE_AGENT_DIRS"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.env", "secrets*", ".env*")


def dirs_from_env() -> tuple[Path, ...]:
    raw = os.environ.get(AGENT_DIRS_ENV, "")
    return tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())


def planned_files(source: Path) -> list[Path]:
    return sorted(
        p.relative_to(source)
        for p in source.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )


def sync(source: Path, destinations: tuple[Path, ...], dry_run: bool = False) -> list[Path]:
    """Copy source into <dest>/tools for each destination that exists; return target dirs used."""
    if not source.is_dir():
        raise FileNotFoundError(f"source folder missing: {source}")
    used: list[Path] = []
    for dest in destinations:
        if not dest.is_dir():
            print(f"skip (agent folder not found): {dest}")
            continue
        target = dest / "tools"
        if not dry_run:
            shutil.copytree(source, target, ignore=IGNORE, dirs_exist_ok=True)
        print(f"{'would copy' if dry_run else 'copied'} {len(planned_files(source))} files -> {target}")
        used.append(target)
    return used


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dest", nargs="*", type=Path, help=f"agent working folders (or set {AGENT_DIRS_ENV})")
    args = parser.parse_args(argv)
    dests = tuple(args.dest) if args.dest else dirs_from_env()
    if not dests:
        print(f"No destination given. Use --dest <folder> [<folder>...] or set {AGENT_DIRS_ENV}.", file=sys.stderr)
        return 2
    return 0 if sync(SOURCE, dests, args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
