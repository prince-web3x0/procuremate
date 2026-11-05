"""Replay a saved real run instead of calling Brave or fetching sites (for demos and for saving search credits).

A replay bundle holds a real run's search results, check results and stage timings. The bundle contains real businesses'
public contact details: it lives in the user cache folder (outside the repo) and must never be committed.
The tools read it with --replay <file> or the REPLAY_FILE environment variable, and replay the stage timings
compressed (default 5x, REPLAY_SPEED or --replay-speed) so progress updates still happen. The output says so plainly.
"""
import copy
import os
import time
from pathlib import Path
from typing import Any, Callable

from budget import shared_dir
from common import now_iso
from progress import ProgressFile, _write_json, load

KIND = "procuremate-replay"
DEFAULT_SPEED = 5.0
REPLAY_ENV = "REPLAY_FILE"
SWITCH_NAME = "replay_active.txt"
SPEED_ENV = "REPLAY_SPEED"


def default_dir() -> Path:
    """Where bundles are saved by default: the user cache folder, outside the repo and any synced folder."""
    return shared_dir("replays")


def switch_file() -> Path:
    """The switch file in the shared user cache folder. The agents' commands may not receive your shell's environment
    variables, but they run as the same user, so they all find this file."""
    return default_dir() / SWITCH_NAME


def active_bundle() -> str | None:
    """The bundle path written in the switch file, or None when replay is off."""
    try:
        text = switch_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def replay_path(arg: str | None) -> str | None:
    """Replay is on when --replay is given, REPLAY_FILE is set, or the switch file exists (checked in that order).

    If the switch file points at a missing or unreadable bundle the tools stop with an error; they never fall back to live calls.
    """
    return arg or os.environ.get(REPLAY_ENV) or active_bundle() or None


def turn_on(bundle: str) -> dict[str, Any]:
    """Validate the bundle, then write its absolute path into the switch file."""
    absolute = str(Path(bundle).expanduser().resolve())
    data = load_bundle(absolute)
    target = switch_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(absolute + "\n", encoding="utf-8")
    return data


def turn_off() -> bool:
    """Remove the switch file. True if replay was on through the file."""
    try:
        switch_file().unlink()
        return True
    except OSError:
        return False


def status() -> dict[str, Any]:
    """Whether replay is on, by which route, and what it points at."""
    via_file = active_bundle()
    env = os.environ.get(REPLAY_ENV) or None
    path = env or via_file
    if not path:
        return {"on": False, "bundle": None, "via": None, "saved_on": None, "problem": None}
    try:
        saved_on = str(load_bundle(path)["saved_at"])[:10]
        problem = None
    except ValueError as err:
        saved_on, problem = None, str(err)
    return {"on": True, "bundle": path, "via": "REPLAY_FILE in this shell" if env else "switch file", "saved_on": saved_on, "problem": problem}


def warning_text(saved_on: str | None, bundle: str | None) -> str:
    return (f"REPLAY IS ON: results come from a live run saved on {saved_on or 'an unknown date'} ({bundle}). "
            "No Brave query is made and no site is fetched. What you see is old data. Turn it off with: python tools/replay_ctl.py off")


def replay_speed(arg: float | None = None) -> float:
    if arg and arg > 0:
        return arg
    try:
        value = float(os.environ.get(SPEED_ENV, ""))
    except ValueError:
        return DEFAULT_SPEED
    return value if value > 0 else DEFAULT_SPEED


def save_bundle(search: dict[str, Any], checks: dict[str, Any], out: str | None = None) -> str:
    """Save a live run as a replay bundle. Refuses demo runs and unfinished runs."""
    for name, report in (("search", search), ("checks", checks)):
        if report.get("mode") != "live":
            raise ValueError(f"the {name} file is not from a live run (mode {report.get('mode')!r}); only live runs are worth replaying")
        if report.get("status") != "complete":
            raise ValueError(f"the {name} file is not complete (status {report.get('status')!r})")
    s_sum, c_sum = search.get("run_summary") or {}, checks.get("run_summary") or {}
    stages = c_sum.get("stage_seconds") or {}
    bundle = {
        "kind": KIND, "saved_at": now_iso(), "request": search.get("request"),
        "search": search, "checks": checks,
        "timings": {
            "search": {"seconds": (s_sum.get("stage_seconds") or {}).get("total", 0.0), "queries": len(search.get("queries", [])),
                       "query_cap": s_sum.get("query_cap"), "suppliers_found": len(search.get("suppliers", []))},
            "checks": {"site_seconds": round(stages.get("site_checks", 0) + stages.get("domain_age", 0) + stages.get("retry_pass", 0), 2),
                       "cross_seconds": stages.get("cross_checks", 0.0), "suppliers": len(checks.get("suppliers", [])),
                       "cross_checks": sum(1 for s in checks.get("suppliers", []) if s.get("cross_check"))},
        },
    }
    target = Path(out) if out else default_dir() / f"replay-{bundle['saved_at'][:10]}.json"
    _write_json(str(target), bundle)
    return str(target)


def load_bundle(path: str) -> dict[str, Any]:
    bundle = load(path)
    if not bundle or bundle.get("kind") != KIND or not bundle.get("search") or not bundle.get("checks"):
        raise ValueError(f"{path} is not a replay bundle (make one with save_replay.py)")
    return bundle


def notice(bundle: dict[str, Any]) -> str:
    return f"REPLAY of a live run saved on {str(bundle['saved_at'])[:10]}"


def _stamp(report: dict[str, Any], bundle: dict[str, Any], speed: float) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["mode"] = "replay"
    out.setdefault("run_summary", {})["brave_budget"] = {
        "queries_used": 0, "live_queries": 0, "by_tool": {}, "cache_hits": 0, "estimated_cost_usd": 0.0,
        "note": "REPLAY: no Brave query was made and no site was fetched."}
    out["data_source"] = notice(bundle) + ": saved results and stage timings, no Brave call and no site fetched"
    out["replay"] = {"notice": notice(bundle), "saved_at": bundle["saved_at"], "speed": speed}
    return out


def _play(progress: ProgressFile, stage: str, items: int, seconds: float, total: int | None, found_end: int, speed: float,
          sleep: Callable[[float], None], phase: int, maximum: bool = False) -> None:
    """Replay one stage: `items` steps spread over the saved seconds, compressed by `speed`."""
    for i in range(1, items + 1):
        sleep(max(0.0, seconds / max(items, 1) / speed))
        progress.update(stage, i, total, round(found_end * i / items), phase=phase, total_is_maximum=maximum)


def replay_search(bundle: dict[str, Any], progress: ProgressFile, speed: float, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    progress.notice = notice(bundle)
    t = bundle["timings"]["search"]
    _play(progress, "searching", t["queries"], t["seconds"], t.get("query_cap"), t["suppliers_found"], speed, sleep, 1, maximum=True)
    progress.update("searching", t["queries"], t["queries"], t["suppliers_found"], finished=True, phase=1)
    return _stamp(bundle["search"], bundle, speed)


def replay_checks(bundle: dict[str, Any], progress: ProgressFile, speed: float, sleep: Callable[[float], None] = time.sleep,
                  domains: list[str] | None = None) -> dict[str, Any]:
    progress.notice = notice(bundle)
    t = bundle["timings"]["checks"]
    _play(progress, "checking sites", t["suppliers"], t["site_seconds"], t["suppliers"], t["suppliers"], speed, sleep, 2)
    _play(progress, "cross-checking the best", t["cross_checks"], t["cross_seconds"], t["cross_checks"], t["suppliers"], speed, sleep, 3)
    progress.update("cross-checking the best", t["suppliers"], t["suppliers"], t["suppliers"], finished=True, phase=3)
    report = _stamp(bundle["checks"], bundle, speed)
    if domains:
        wanted = set(domains)
        report["suppliers"] = [s for s in report["suppliers"] if s["domain"] in wanted]
        report["replay"]["not_in_replay"] = sorted(wanted - {s["domain"] for s in report["suppliers"]})
    return report


def replay_site_check(bundle: dict[str, Any], domain: str) -> dict[str, Any]:
    for s in bundle["checks"].get("suppliers", []):
        if s["domain"] == domain and s.get("site_check"):
            return _stamp({"mode": "live", **s["site_check"]}, bundle, 0.0)
    raise ValueError(f"{domain} has no saved site check in this replay")


def replay_cross_check(bundle: dict[str, Any], domain: str) -> dict[str, Any]:
    for s in bundle["checks"].get("suppliers", []):
        if s["domain"] == domain and s.get("cross_check"):
            return {**_stamp({"mode": "live", **s["cross_check"]}, bundle, 0.0)}
    raise ValueError(f"{domain} has no saved cross-check in this replay")
