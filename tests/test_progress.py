"""Progress files written during long runs, the --wait option, and the one-line plain-English summary."""
import json
from pathlib import Path
from typing import Any

import check_suppliers as cs
import progress as pg
import scout_search as ss
from common import Fetcher
from conftest import FakeNet

FIELDS = {"tool", "phase", "stage", "items_done", "items_total", "total_is_maximum", "suppliers_found_so_far", "seconds_elapsed",
          "average_seconds_per_item", "estimated_seconds_remaining", "over_estimate", "finished", "interrupted"}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _file(tmp_path: Path, clock: Clock, label: str = "check_suppliers") -> tuple[pg.ProgressFile, Path]:
    path = tmp_path / "p.progress.json"
    return pg.ProgressFile(str(path), label, clock), path


def test_the_file_has_every_field_and_no_estimate_after_one_item(tmp_path: Path) -> None:
    clock = Clock()
    pf, path = _file(tmp_path, clock)
    clock.now = 10
    pf.update("checking sites", 1, 14, 1, phase=2)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == FIELDS and data["phase"] == 2
    assert data["average_seconds_per_item"] == 10.0 and data["estimated_seconds_remaining"] is None and data["finished"] is False


def test_the_estimate_appears_from_the_second_item_and_comes_from_the_average(tmp_path: Path) -> None:
    clock = Clock()
    pf, _ = _file(tmp_path, clock)
    clock.now = 20
    assert pf.update("checking sites", 2, 14, 2)["estimated_seconds_remaining"] == 120  # 10 s per item, 12 left


def test_no_total_means_no_estimate_and_no_total_in_the_line(tmp_path: Path) -> None:
    clock = Clock()
    pf, _ = _file(tmp_path, clock, "scout_search")
    clock.now = 60
    data = pf.update("searching", 6, None, 9, phase=1)
    assert data["items_total"] is None and data["estimated_seconds_remaining"] is None
    line = pg.describe(data)
    assert line == "Searching: 6 done, 9 candidates found so far."


def test_the_search_cap_is_a_maximum_and_the_line_says_so(tmp_path: Path) -> None:
    clock = Clock()
    pf, _ = _file(tmp_path, clock, "scout_search")
    clock.now = 20
    data = pf.update("searching", 2, 12, 5, phase=1, total_is_maximum=True)
    assert data["total_is_maximum"] is True
    assert pg.describe(data) == "Searching: 2 of up to 12 done, 5 candidates found so far, at most about 2 minutes left."


def test_a_new_stage_restarts_the_average_and_keeps_its_phase(tmp_path: Path) -> None:
    clock = Clock()
    pf, _ = _file(tmp_path, clock)
    clock.now = 100
    pf.update("checking sites", 14, 14, 14, phase=2)
    clock.now = 110
    data = pf.update("cross-checking the best", 1, 5, 14, phase=3)
    assert data["average_seconds_per_item"] == 10.0 and data["estimated_seconds_remaining"] is None and data["phase"] == 3


def test_going_well_past_the_first_estimate_is_flagged(tmp_path: Path) -> None:
    clock = Clock()
    pf, _ = _file(tmp_path, clock)
    clock.now = 20
    pf.update("checking sites", 2, 10, 2)  # expects 100 s for the stage
    clock.now = 200
    data = pf.update("checking sites", 3, 10, 3)
    assert data["over_estimate"] is True
    assert "taking longer than expected" in pg.describe(data) and "left" not in pg.describe(data)


def test_finished_and_interrupted_flags(tmp_path: Path) -> None:
    clock = Clock()
    pf, path = _file(tmp_path, clock)
    clock.now = 125
    assert pf.update("done", 14, 14, 14, finished=True)["finished"] is True
    assert pg.describe(json.loads(path.read_text(encoding="utf-8"))) == "The checks finished in about 2 minutes: 14 suppliers."
    cut = pf.update("checking sites", 5, 14, 5, interrupted=True)
    assert "interrupted after 5 items" in pg.describe(cut) and "resumed" in pg.describe(cut)


def test_the_summary_line_for_a_running_check() -> None:
    data = {"tool": "check_suppliers", "stage": "checking sites", "items_done": 6, "items_total": 20, "suppliers_found_so_far": 6,
            "seconds_elapsed": 90, "estimated_seconds_remaining": 240, "over_estimate": False, "finished": False, "interrupted": False}
    assert pg.describe(data) == "Checking sites: 6 of 20 done, about 4 minutes left."
    assert pg.describe({**data, "estimated_seconds_remaining": None}) == "Checking sites: 6 of 20 done."
    assert pg.describe({**data, "estimated_seconds_remaining": 40}).endswith("about 40 seconds left.")


def test_a_missing_progress_file_is_reported_not_invented(tmp_path: Path, capsys: Any) -> None:
    assert pg.main([str(tmp_path / "nope.json")]) == 1
    assert "No progress file yet" in capsys.readouterr().out


def test_the_command_prints_one_line(tmp_path: Path, capsys: Any) -> None:
    clock = Clock()
    pf, path = _file(tmp_path, clock)
    clock.now = 30
    pf.update("checking sites", 3, 6, 3)
    assert pg.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("Checking sites: 3 of 6 done")


# --- --wait -------------------------------------------------------------------------------------------

class Sleeper:
    """A fake clock that moves only when the code sleeps; `hook` runs on each sleep (to simulate the run progressing)."""

    def __init__(self, hook: Any = None) -> None:
        self.now, self.slept, self.hook = 0.0, 0.0, hook

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds
        if self.hook:
            self.hook(self.now)


def _write(path: Path, **fields: Any) -> None:
    base = {"tool": "check_suppliers", "stage": "checking sites", "items_done": 4, "items_total": 20, "suppliers_found_so_far": 4,
            "seconds_elapsed": 40, "estimated_seconds_remaining": 160, "over_estimate": False, "finished": False, "interrupted": False}
    path.write_text(json.dumps({**base, **fields}), encoding="utf-8")


def test_wait_sleeps_the_full_time_when_the_run_is_still_going(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "p.json"
    _write(path)
    s = Sleeper()
    assert pg.main([str(path), "--wait", "60"], sleep=s.sleep, clock=s.clock, wall=s.clock) == 0
    assert s.slept == 60 and capsys.readouterr().out == "UPDATE DUE: Checking sites: 4 of 20 done, about 3 minutes left.\n"


def test_wait_without_a_number_means_60_seconds_and_is_capped(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    _write(path)
    s = Sleeper()
    pg.main([str(path), "--wait"], sleep=s.sleep, clock=s.clock, wall=s.clock)
    assert s.slept == pg.DEFAULT_WAIT == 60
    long = Sleeper()
    pg.main([str(path), "--wait", "300"], sleep=long.sleep, clock=long.clock, wall=long.clock)
    assert long.slept == pg.MAX_WAIT == 60  # one call never blocks for minutes


def test_wait_stops_early_when_the_run_finishes_and_reports_the_new_state(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "p.json"
    _write(path)
    s = Sleeper(hook=lambda now: _write(path, finished=True, items_done=20) if now >= 25 else None)
    assert pg.main([str(path), "--wait", "60"], sleep=s.sleep, clock=s.clock, wall=s.clock) == 0
    assert s.slept == 25 and capsys.readouterr().out.startswith("FINISHED: The checks finished")


def test_wait_returns_at_once_if_the_run_is_already_finished(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    _write(path, finished=True)
    s = Sleeper()
    pg.main([str(path), "--wait", "90"], sleep=s.sleep, clock=s.clock)
    assert s.slept == 0


def test_wait_for_a_file_that_appears_later(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "late.json"
    s = Sleeper(hook=lambda now: _write(path) if now >= 10 else None)
    assert pg.main([str(path), "--wait", "30"], sleep=s.sleep, clock=s.clock) == 0
    assert s.slept == 30 and "Checking sites" in capsys.readouterr().out


def test_wait_with_no_file_ever_says_so(tmp_path: Path, capsys: Any) -> None:
    s = Sleeper()
    assert pg.main([str(tmp_path / "none.json"), "--wait", "5"], sleep=s.sleep, clock=s.clock) == 1
    assert "No progress file yet" in capsys.readouterr().out and s.slept == 5


# --- the tools write the files ----------------------------------------------------------------------------

def test_default_progress_path_sits_next_to_the_out_file() -> None:
    assert pg.progress_path("out/search.json") == "out/search.progress.json" and pg.progress_path(None) is None


def test_both_tools_write_the_file_and_mark_it_finished(tmp_path: Path) -> None:
    search, checks = tmp_path / "s.json", tmp_path / "c.json"
    assert ss.main(["--product", "office chairs", "--location", "Abuja", "--out", str(search)]) == 0
    first = json.loads((tmp_path / "s.progress.json").read_text(encoding="utf-8"))
    assert first["finished"] is True and first["tool"] == "scout_search" and first["phase"] == 1 and first["suppliers_found_so_far"] > 0
    assert cs.main([str(search), "--out", str(checks)]) == 0
    second = json.loads((tmp_path / "c.progress.json").read_text(encoding="utf-8"))
    assert second["finished"] is True and second["phase"] == 3 and second["suppliers_found_so_far"] >= 7
    assert pg.describe(second).startswith("The checks finished")


def _check_with_progress(tmp_path: Path, domains: list[str], top_n: int) -> list[dict[str, Any]]:
    page = "<html><body><h1>Office chairs</h1><p>Address: 1 Road, Wuse, Abuja</p></body></html>"
    routes = {f"https://{d}/": page for d in domains}
    report = {"request": {"product": "office chairs", "location": "Abuja"}, "suppliers": [{"domain": d, "records": []} for d in domains]}
    clock = Clock()
    pf = pg.ProgressFile(str(tmp_path / "x.json"), "check_suppliers", clock)
    steps: list[dict[str, Any]] = []

    def on_progress(partial: dict[str, Any]) -> None:
        clock.now += 10
        p = partial["progress"]
        steps.append(pf.update(p["stage"], p["items_done"], p["items_total"], p["suppliers_done"], phase=cs.PHASES.get(p["stage"])))

    cs.check_suppliers(report, lambda q: [], fetcher_factory=lambda: Fetcher(opener=FakeNet(routes), max_pages=2),
                       page_fetcher_factory=lambda: Fetcher(opener=FakeNet({})), get_json=lambda u: {}, on_progress=on_progress, top_n=top_n)
    return steps


def test_site_checks_write_progress_before_the_first_site_and_after_each_one(tmp_path: Path) -> None:
    steps = _check_with_progress(tmp_path, [f"s{i}.ng" for i in range(4)], top_n=0)
    sites = [s for s in steps if s["stage"] == "checking sites"]
    assert [s["items_done"] for s in sites] == [0, 1, 2, 3, 4] and all(s["items_total"] == 4 for s in sites) and all(s["phase"] == 2 for s in sites)
    assert [s["estimated_seconds_remaining"] for s in sites] == [None, None, 30, 13, None]  # from the second item on (the stage clock starts when the stage does), none once all are done


def test_cross_checks_have_their_own_stage_total_and_phase(tmp_path: Path) -> None:
    steps = _check_with_progress(tmp_path, [f"s{i}.ng" for i in range(4)], top_n=3)
    cross = [s for s in steps if s["stage"] == "cross-checking the best"]
    assert cross and all(s["items_total"] == 3 and s["phase"] == 3 for s in cross)
    assert max(s["items_done"] for s in cross) == 3 and cross[0]["items_done"] == 0


# --- cadence: the tool says when an update is due ---------------------------------------------------------

def test_a_new_stage_is_due_at_once_then_not_until_about_90_seconds_have_passed(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "p.json"
    _write(path)
    s = Sleeper()
    outputs = []
    for _ in range(3):  # three calls of --wait 60, as the agent loops
        pg.main([str(path), "--wait", "60"], sleep=s.sleep, clock=s.clock, wall=s.clock)
        outputs.append(capsys.readouterr().out.split(":")[0])
    assert outputs == ["UPDATE DUE", "No update due yet (last one 60 s ago)", "UPDATE DUE"]  # posts at 0 s and 120 s: never 3 minutes apart


def test_a_stage_change_is_due_even_right_after_an_update(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "p.json"
    _write(path)
    s = Sleeper()
    pg.main([str(path), "--wait", "1"], sleep=s.sleep, clock=s.clock, wall=s.clock)
    capsys.readouterr()
    _write(path, stage="cross-checking the best", items_done=1, items_total=5)
    pg.main([str(path), "--wait", "1"], sleep=s.sleep, clock=s.clock, wall=s.clock)
    assert capsys.readouterr().out.startswith("UPDATE DUE: Cross-checking the best: 1 of 5 done")


def test_a_stage_that_has_just_started_never_says_zero_of_n(tmp_path: Path) -> None:
    clock = Clock()
    pf, _ = _file(tmp_path, clock)
    line = pg.describe(pf.update("checking sites", 0, 20, 0))
    assert line == "Checking sites just started (20 in all)." and "0 of" not in line
    assert pg.describe(pf.update("searching", 0, None, 0)) == "Searching just started."
