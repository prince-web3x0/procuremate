"""Progress file for long runs, and a one-line plain-English summary of it.

Usage:
    python progress.py out/search.progress.json            # print one line now
    python progress.py out/search.progress.json --wait 90   # wait up to 90 s (less if the run finishes), then print

The tools write the file after every item (a search, a site, a cross-check). Totals and time estimates appear only
when they are known: an estimate needs a known total and at least 2 items done in the current stage.
"""
import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Callable

MIN_ITEMS_FOR_ESTIMATE = 2
DEFAULT_WAIT = 60
MAX_WAIT = 60  # one call never blocks longer, so an agent can always post within about 2 minutes
DUE_SECONDS = 90  # an update is due this long after the last one (or at once when a new stage starts)
POLL_SECONDS = 1.0
OVER_ESTIMATE_FACTOR = 1.5  # the stage has taken this many times what the first estimate said


def progress_path(out_path: str | None) -> str | None:
    """Default progress file next to the --out file: out/search.json -> out/search.progress.json."""
    if not out_path:
        return None
    stem, _ = os.path.splitext(out_path)
    return stem + ".progress.json"


def _write_json(path: str, data: dict[str, Any]) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # unique per process and thread: two writers never share a temp file
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:  # Windows: another process has the file open for an instant
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


class ProgressFile:
    """Writes the small progress file. Nothing is written when path is None."""

    def __init__(self, path: str | None, label: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._path, self._label, self._clock = path, label, clock
        self._began = clock()
        self._stage: str | None = None
        self._stage_began = self._began
        self._last = self._began
        self._expected_stage_seconds: float | None = None
        self.notice: str | None = None  # set during a replay: shown in every progress line

    def update(self, stage: str, done: int, total: int | None, found: int, finished: bool = False, interrupted: bool = False,
               phase: int | None = None, total_is_maximum: bool = False) -> dict[str, Any]:
        now = self._clock()
        if stage != self._stage:
            self._stage, self._stage_began, self._expected_stage_seconds = stage, self._last, None  # a stage begins when the last item of the one before finished
        stage_seconds = now - self._stage_began
        average = round(stage_seconds / done, 1) if done > 0 else None
        remaining = None
        if total is not None and average is not None and done >= MIN_ITEMS_FOR_ESTIMATE and done < total:
            remaining = round(average * (total - done))
            if self._expected_stage_seconds is None:
                self._expected_stage_seconds = stage_seconds + remaining
        over = bool(self._expected_stage_seconds and total is not None and done < total
                    and stage_seconds > self._expected_stage_seconds * OVER_ESTIMATE_FACTOR)
        data = {
            "tool": self._label, "phase": phase, "stage": stage, "items_done": done, "items_total": total, "total_is_maximum": bool(total_is_maximum and total is not None), "suppliers_found_so_far": found,
            "seconds_elapsed": round(now - self._began, 1), "average_seconds_per_item": average,
            "estimated_seconds_remaining": remaining, "over_estimate": over,
            "finished": finished, "interrupted": interrupted,
        }
        if self.notice:
            data["replay"] = self.notice
        self._last = now
        if self._path:
            _write_json(self._path, data)
        return data


def _approx(seconds: float) -> str:
    if seconds < 60:
        return f"about {max(10, round(seconds / 10) * 10)} seconds"
    minutes = round(seconds / 60)
    return "about a minute" if minutes <= 1 else f"about {minutes} minutes"


def describe(p: dict[str, Any]) -> str:
    """One plain-English line from a progress dict. Uses only what the file says; a replay says so first."""
    line = _describe(p)
    return f"{p['replay']}: {line}" if p.get("replay") else line


def _describe(p: dict[str, Any]) -> str:
    tool = "search" if p.get("tool") == "scout_search" else "checks"
    found, elapsed = p.get("suppliers_found_so_far"), p.get("seconds_elapsed")
    took = f" in {_approx(elapsed)}" if isinstance(elapsed, (int, float)) and elapsed >= 30 else ""
    if p.get("finished"):
        return f"The {tool} finished{took}: {found} suppliers." if found is not None else f"The {tool} finished{took}."
    if p.get("interrupted"):
        return f"The {tool} was interrupted after {p.get('items_done', 0)} items; it can be resumed."
    stage, done, total = p.get("stage", "working"), p.get("items_done", 0), p.get("items_total")
    if done == 0:  # nothing finished yet: say only what is running, never "0 of N done"
        return f"{stage.capitalize()} just started ({total} in all)." if total is not None else f"{stage.capitalize()} just started."
    upto = "up to " if p.get("total_is_maximum") else ""
    count = f"{done} of {upto}{total} done" if total is not None else f"{done} done"
    parts = [f"{stage.capitalize()}: {count}"]
    if p.get("tool") == "scout_search" and found is not None:
        parts.append(f"{found} candidates found so far")
    if p.get("over_estimate"):
        parts.append("taking longer than expected")
    elif p.get("estimated_seconds_remaining") is not None:
        parts.append(f"{'at most ' if p.get('total_is_maximum') else ''}{_approx(p['estimated_seconds_remaining'])} left")
    return ", ".join(parts) + "."


def load(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def wait_for(path: str, seconds: float, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """Sleep up to `seconds`, stopping early once the run is finished or interrupted. Returns the latest progress (None if there is no file)."""
    deadline = clock() + max(0.0, seconds)
    while True:
        data = load(path)
        if data and (data.get("finished") or data.get("interrupted")):
            return data
        remaining = deadline - clock()
        if remaining <= 0:
            return data
        sleep(min(POLL_SECONDS, remaining))


def update_due(path: str, data: dict[str, Any], wall: Callable[[], float] = time.time) -> tuple[bool, float | None]:
    """Is a chat update due? Yes at once for a new stage, else DUE_SECONDS after the last due update. Records the time in a small side file."""
    side = path + ".due.json"
    now = wall()
    last = load(side) or {}
    since = now - last["at"] if "at" in last else None
    due = last.get("stage") != data.get("stage") or since is None or since >= DUE_SECONDS
    if due:
        try:
            _write_json(side, {"at": now, "stage": data.get("stage")})
        except OSError:
            pass
    return due, since


def main(argv: list[str] | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         wall: Callable[[], float] = time.time) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="progress file written by scout_search.py or check_suppliers.py")
    parser.add_argument("--wait", nargs="?", type=float, const=DEFAULT_WAIT, default=None, metavar="N",
                        help=f"sleep up to N seconds (at most {MAX_WAIT}; default {DEFAULT_WAIT}) first, stopping early when the run is finished; "
                             "the line then starts with UPDATE DUE (post it), No update due yet, FINISHED or INTERRUPTED")
    args = parser.parse_args(argv)
    if args.wait is None:
        data = load(args.file)
    else:
        data = wait_for(args.file, min(args.wait, MAX_WAIT), sleep, clock)
    if data is None:
        print("No progress file yet (the run may not have started).")
        return 1
    line = describe(data)
    if args.wait is not None:
        if data.get("finished"):
            line = f"FINISHED: {line}"
        elif data.get("interrupted"):
            line = f"INTERRUPTED: {line}"
        else:
            due, since = update_due(args.file, data, wall)
            line = f"UPDATE DUE: {line}" if due else f"No update due yet (last one {round(since)} s ago): {line}"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
