"""Brave query counter and cap, query cache, cross-check reuse, and replay bundles."""
import json
from pathlib import Path
from typing import Any

import pytest

import budget as bg
import check_suppliers as cs
import cross_check as cc
import replay as rp
import save_replay as sr
import scout_search as ss
from common import Fetcher
from conftest import FakeNet, fixture_text
from progress import ProgressFile


def _budget(tmp_path: Path, cap: int = 3, price: float = 5.0) -> bg.Budget:
    return bg.Budget(tmp_path / "counter.json", cap=cap, price_per_1000=price)


# --- counter and cap --------------------------------------------------------------------------------

def test_the_counter_counts_per_tool_and_estimates_the_cost(tmp_path: Path) -> None:
    b = _budget(tmp_path, cap=50)
    b.start_task()
    for tool in ("scout_search", "scout_search", "check_suppliers", "vera_check_suppliers"):
        b.charge(tool)
    s = b.summary()
    assert s["queries_used"] == 4 and s["by_tool"] == {"scout_search": 2, "check_suppliers": 1, "vera_check_suppliers": 1}
    assert s["estimated_cost_usd"] == 0.02 and s["price_per_1000_usd"] == 5.0 and "Estimate" in s["note"]
    assert "estimated cost about $0.020 at $5/1,000 (an estimate)" in b.line()


def test_the_price_is_configurable(tmp_path: Path) -> None:
    b = _budget(tmp_path, price=3.0)
    b.start_task()
    b.charge("x")
    assert b.summary()["estimated_cost_usd"] == 0.003


def test_the_cap_stops_further_queries_and_says_so_plainly(tmp_path: Path) -> None:
    b = _budget(tmp_path, cap=2)
    b.start_task()
    b.charge("scout_search")
    b.charge("check_suppliers")
    with pytest.raises(bg.BudgetReached, match="search budget reached"):
        b.charge("vera_check_suppliers")
    s = b.summary()
    assert s["queries_used"] == 2 and s["budget_reached"] is True and "SEARCH BUDGET REACHED" in b.line()


def test_two_processes_share_one_counter_file(tmp_path: Path) -> None:
    scout, vera = _budget(tmp_path, cap=2), _budget(tmp_path, cap=2)  # separate objects, same file: like two agents
    scout.start_task()
    scout.charge("scout_search")
    vera.charge("vera_check_suppliers")
    with pytest.raises(bg.BudgetReached):
        scout.charge("scout_search")


def test_a_new_task_resets_the_counter(tmp_path: Path) -> None:
    b = _budget(tmp_path, cap=1)
    b.start_task()
    b.charge("x")
    b.start_task()
    b.charge("x")  # allowed again
    assert b.summary()["queries_used"] == 1


def test_the_cap_comes_from_the_environment_default_20(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bg.max_queries() == 20
    monkeypatch.setenv("MAX_BRAVE_QUERIES", "7")
    assert bg.max_queries() == 7
    monkeypatch.setenv("MAX_BRAVE_QUERIES", "lots")
    assert bg.max_queries() == 20


# --- query cache -------------------------------------------------------------------------------------

def test_a_repeated_query_is_answered_from_the_cache_at_no_cost(tmp_path: Path) -> None:
    b, calls = _budget(tmp_path, cap=5), []
    b.start_task()
    cache = bg.QueryCache(tmp_path / "q")
    search = bg.guarded(lambda q: calls.append(q) or [{"title": "t", "url": "https://a.ng/", "description": q}], b, cache, "scout_search")
    first = search("office chairs Abuja")
    again = search("  Office   Chairs ABUJA ")  # same text after normalising
    assert first == again and len(calls) == 1
    assert b.summary()["queries_used"] == 1 and b.summary()["cache_hits"] == 1


def test_the_query_cache_expires_after_24_hours(tmp_path: Path) -> None:
    now = [1000.0]
    cache = bg.QueryCache(tmp_path / "q", clock=lambda: now[0])
    cache.put("q", [{"x": 1}])
    assert cache.get("q") == [{"x": 1}]
    now[0] += 24 * 3600 + 1
    assert cache.get("q") is None


def test_at_the_cap_a_cached_query_still_answers_and_a_new_one_raises_the_given_error(tmp_path: Path) -> None:
    b = _budget(tmp_path, cap=1)
    b.start_task()
    cache = bg.QueryCache(tmp_path / "q")
    search = bg.guarded(lambda q: [{"title": q}], b, cache, "t", ss.SearchError)
    search("one")
    assert search("one") == [{"title": "one"}]
    with pytest.raises(ss.SearchError, match="search budget reached"):
        search("two")


def test_a_failed_search_is_not_cached(tmp_path: Path) -> None:
    b = _budget(tmp_path, cap=5)
    b.start_task()
    cache = bg.QueryCache(tmp_path / "q")

    def boom(q: str) -> list[dict[str, str]]:
        raise ss.SearchError("Brave API returned HTTP 500")

    with pytest.raises(ss.SearchError):
        bg.guarded(boom, b, cache, "t", ss.SearchError)("q")
    assert cache.get("q") is None


# --- Vera reuses Scout's cross-check ----------------------------------------------------------------

ROUTES = {
    "https://s.ng/": fixture_text("supplier_home.html"), "https://s.ng/pages/contact": fixture_text("contact.html"),
    "https://s.ng/pages/about-us": fixture_text("footer_registered.html"), "https://s.ng/pages/delivery-info": fixture_text("delivery.html"),
}


def _supplier_report() -> dict[str, Any]:
    return {"request": {"product": "office chairs", "location": "Abuja"}, "suppliers": [{"domain": "s.ng", "records": []}]}


def _run_check(queries: list[str], cache: bg.CrossCheckCache) -> dict[str, Any]:
    page = ROUTES
    return cs.check_suppliers(_supplier_report(), lambda q: queries.append(q) or [], cross_cache=cache,
                              fetcher_factory=lambda: Fetcher(opener=FakeNet(page), max_pages=2),
                              page_fetcher_factory=lambda: Fetcher(opener=FakeNet({})), get_json=lambda u: {})


def test_a_cross_check_done_by_scout_is_reused_by_vera_without_brave_queries(tmp_path: Path) -> None:
    cache = bg.CrossCheckCache(tmp_path / "cross")
    scout_queries: list[str] = []
    first = _run_check(scout_queries, cache)
    assert scout_queries and first["suppliers"][0]["cross_check_state"] == "run"
    vera_queries: list[str] = []
    second = _run_check(vera_queries, cache)
    assert vera_queries == [] and second["suppliers"][0]["cross_check_state"].startswith("run (reused")
    assert second["suppliers"][0]["cross_check"]["checks"] == first["suppliers"][0]["cross_check"]["checks"]


def test_a_cross_check_cut_short_by_the_budget_is_not_cached(tmp_path: Path) -> None:
    cache = bg.CrossCheckCache(tmp_path / "cross")

    def stopped(q: str) -> list[dict[str, str]]:
        raise ss.SearchError("search budget reached: 20 Brave queries used for this task")

    page = ROUTES
    cs.check_suppliers(_supplier_report(), stopped, cross_cache=cache, fetcher_factory=lambda: Fetcher(opener=FakeNet(page), max_pages=2),
                       page_fetcher_factory=lambda: Fetcher(opener=FakeNet({})), get_json=lambda u: {})
    assert cache.get("s.ng") is None


# --- replay -------------------------------------------------------------------------------------------

def _live_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    search = {"mode": "live", "status": "complete", "request": {"product": "office chairs", "location": "Abuja"},
              "queries": [{"query": "a"}, {"query": "b"}, {"query": "c"}, {"query": "d"}], "suppliers": [{"domain": "a.ng"}, {"domain": "b.ng"}],
              "run_summary": {"stage_seconds": {"total": 40.0}, "query_cap": 12}}
    checks = {"mode": "live", "status": "complete", "request": search["request"],
              "run_summary": {"stage_seconds": {"site_checks": 30.0, "domain_age": 10.0, "retry_pass": 0.0, "cross_checks": 20.0, "total": 60.0}},
              "suppliers": [{"domain": "a.ng", "cross_check": {"domain": "a.ng", "checks": {"x": 1}}}, {"domain": "b.ng", "cross_check": None}]}
    return search, checks


def test_a_live_run_is_saved_as_a_bundle_outside_the_repo_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCUREMATE_CACHE_DIR", str(tmp_path / "cache" / "pages"))
    search, checks = _live_pair()
    path = rp.save_bundle(search, checks)
    assert Path(path).parent == tmp_path / "cache" / "replays" and Path(path).name.startswith("replay-")
    bundle = rp.load_bundle(path)
    assert bundle["timings"]["search"] == {"seconds": 40.0, "queries": 4, "query_cap": 12, "suppliers_found": 2}
    assert bundle["timings"]["checks"] == {"site_seconds": 40.0, "cross_seconds": 20.0, "suppliers": 2, "cross_checks": 1}


def test_demo_and_unfinished_runs_are_refused(tmp_path: Path) -> None:
    search, checks = _live_pair()
    with pytest.raises(ValueError, match="not from a live run"):
        rp.save_bundle({**search, "mode": "demo"}, checks, str(tmp_path / "b.json"))
    with pytest.raises(ValueError, match="not complete"):
        rp.save_bundle(search, {**checks, "status": "partial"}, str(tmp_path / "b.json"))


def test_a_file_that_is_not_a_bundle_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a replay bundle"):
        rp.load_bundle(str(tmp_path / "x.json"))
    with pytest.raises(ValueError):
        rp.load_bundle(str(tmp_path / "missing.json"))


def _bundle(tmp_path: Path) -> str:
    search, checks = _live_pair()
    return rp.save_bundle(search, checks, str(tmp_path / "bundle.json"))


def test_replaying_the_search_compresses_the_timings_and_says_it_is_a_replay(tmp_path: Path) -> None:
    bundle = rp.load_bundle(_bundle(tmp_path))
    sleeps: list[float] = []
    progress = ProgressFile(str(tmp_path / "p.json"), "scout_search")
    report = rp.replay_search(bundle, progress, 5.0, sleep=sleeps.append)
    assert sleeps == [2.0] * 4  # 40 s over 4 queries, at 5x
    assert report["mode"] == "replay" and report["replay"]["notice"].startswith("REPLAY of a live run saved on 20")
    assert "no Brave call" in report["data_source"] and report["suppliers"] == [{"domain": "a.ng"}, {"domain": "b.ng"}]
    final = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert final["finished"] is True and final["phase"] == 1


def test_replaying_the_checks_filters_to_the_requested_domains(tmp_path: Path) -> None:
    bundle = rp.load_bundle(_bundle(tmp_path))
    report = rp.replay_checks(bundle, ProgressFile(None, "check_suppliers"), 5.0, sleep=lambda s: None, domains=["b.ng", "zzz.ng"])
    assert [s["domain"] for s in report["suppliers"]] == ["b.ng"] and report["replay"]["not_in_replay"] == ["zzz.ng"]


def test_replay_speed_defaults_to_5_and_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert rp.replay_speed() == 5.0
    monkeypatch.setenv("REPLAY_SPEED", "10")
    assert rp.replay_speed() == 10.0 and rp.replay_speed(2.0) == 2.0


def test_the_tools_replay_instead_of_searching_or_fetching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    bundle_path = _bundle(tmp_path)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    monkeypatch.setenv("REPLAY_FILE", bundle_path)  # no key, no network, no fixtures needed
    out_s, out_c = tmp_path / "s.json", tmp_path / "c.json"
    assert ss.main(["--product", "office chairs", "--location", "Abuja", "--out", str(out_s)]) == 0
    assert "REPLAY of a live run saved on" in capsys.readouterr().err
    assert json.loads(out_s.read_text(encoding="utf-8"))["mode"] == "replay"
    assert cs.main([str(out_s), "--out", str(out_c)]) == 0
    checks = json.loads(out_c.read_text(encoding="utf-8"))
    assert checks["mode"] == "replay" and [s["domain"] for s in checks["suppliers"]] == ["a.ng", "b.ng"]
    assert cc.main(["a.ng", "--replay", bundle_path, "--out", str(tmp_path / "x.json")]) == 0
    assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8"))["replay"]["notice"].startswith("REPLAY")
    assert cc.main(["nope.ng", "--replay", bundle_path]) == 2


def test_save_replay_command(tmp_path: Path, capsys: Any) -> None:
    search, checks = _live_pair()
    (tmp_path / "s.json").write_text(json.dumps(search), encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps(checks), encoding="utf-8")
    out = tmp_path / "r.json"
    assert sr.main(["--search", str(tmp_path / "s.json"), "--checks", str(tmp_path / "c.json"), "--out", str(out)]) == 0
    assert out.exists() and "Do not commit it" in capsys.readouterr().out
    assert sr.main(["--search", str(tmp_path / "missing.json"), "--checks", str(tmp_path / "c.json")]) == 2


def test_replay_bundles_are_ignored_by_git() -> None:
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "replays/" in ignore and "*.replay.json" in ignore
