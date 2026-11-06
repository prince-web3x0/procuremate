"""Brave query counter and cap, query cache, and cross-check reuse."""
from pathlib import Path
from typing import Any

import pytest

import budget as bg
import check_suppliers as cs
import scout_search as ss
from common import Fetcher
from conftest import FakeNet, fixture_text


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


