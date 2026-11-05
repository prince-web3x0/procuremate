"""Brave query accounting for a whole task: a shared counter and cap, a 24-hour query cache, and a cross-check cache.

The counter file lives in the user cache folder (outside any synced folder and outside git), so Scout and Vera,
which are separate processes, spend from the same budget. The task starts when scout_search.py starts a new
search; the other tools add to it. Cost is an estimate (price per 1,000 requests is configurable), never a bill.
"""
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from common import PageCache, default_cache_dir, now_iso
from progress import _write_json, load

DEFAULT_CAP = 20
DEFAULT_PRICE_PER_1000 = 5.0  # US dollars; check your own Brave plan
CAP_ENV = "MAX_BRAVE_QUERIES"
PRICE_ENV = "BRAVE_PRICE_PER_1000"
BUDGET_REACHED = "search budget reached"
_LOCK = threading.Lock()  # queries can run two at a time in one process


class BudgetReached(Exception):
    """The per-task Brave query cap has been reached."""


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def max_queries() -> int:
    return int(_float_env(CAP_ENV, DEFAULT_CAP))


def shared_dir(name: str) -> Path:
    """A folder next to the page cache (same per-user location, or PROCUREMATE_CACHE_DIR's sibling)."""
    return default_cache_dir().with_name(name)


class Budget:
    """The shared per-task counter. charge() before every real Brave request; cache hits are free."""

    def __init__(self, path: str | Path | None = None, cap: int | None = None, price_per_1000: float | None = None) -> None:
        self._path = str(path or shared_dir("brave") / "task_counter.json")
        self.cap = max_queries() if cap is None else cap
        self.price = _float_env(PRICE_ENV, DEFAULT_PRICE_PER_1000) if price_per_1000 is None else price_per_1000

    def _state(self) -> dict[str, Any]:
        return {"started_at": now_iso(), "by_tool": {}, "cache_hits": 0, "refused": 0, **(load(self._path) or {})}

    def start_task(self) -> None:
        """A new task: the counter starts again from zero."""
        _write_json(self._path, {"started_at": now_iso(), "by_tool": {}, "cache_hits": 0, "refused": 0})

    def total(self) -> int:
        return sum(self._state()["by_tool"].values())

    def charge(self, tool: str) -> None:
        with _LOCK:
            state = self._state()
            if sum(state["by_tool"].values()) >= self.cap:
                state["refused"] += 1
                _write_json(self._path, state)
                raise BudgetReached(f"{BUDGET_REACHED}: {self.cap} Brave queries used for this task")
            state["by_tool"][tool] = state["by_tool"].get(tool, 0) + 1
            _write_json(self._path, state)

    def note_cache_hit(self) -> None:
        with _LOCK:
            state = self._state()
            state["cache_hits"] += 1
            _write_json(self._path, state)

    def summary(self) -> dict[str, Any]:
        state = self._state()
        used = sum(state["by_tool"].values())
        return {
            "queries_used": used, "by_tool": state["by_tool"], "cache_hits": state["cache_hits"], "cap": self.cap,
            "budget_reached": state["refused"] > 0,
            "estimated_cost_usd": round(used * self.price / 1000, 4), "price_per_1000_usd": self.price,
            "note": f"Estimate at ${self.price:g} per 1,000 requests; the real cost depends on your Brave plan. Not for chat unless asked.",
        }

    def line(self) -> str:
        s = self.summary()
        parts = ", ".join(f"{tool} {n}" for tool, n in s["by_tool"].items()) or "none"
        reached = f" SEARCH BUDGET REACHED (cap {s['cap']})." if s["budget_reached"] else f" (cap {s['cap']})"
        return (f"Brave queries this task: {s['queries_used']} ({parts}); {s['cache_hits']} answered from the cache; "
                f"estimated cost about ${s['estimated_cost_usd']:.3f} at ${s['price_per_1000_usd']:g}/1,000 (an estimate).{reached}")


class QueryCache:
    """Brave results per query text for 24 hours, shared by every agent of the user. Check Brave's terms on caching."""

    def __init__(self, directory: str | Path | None = None, hours: float = 24, clock: Callable[[], float] = time.time) -> None:
        self._cache = PageCache(directory or shared_dir("brave") / "queries", hours=hours, clock=clock)

    @staticmethod
    def _key(query: str) -> str:
        return "q:" + " ".join(query.lower().split())

    def get(self, query: str) -> list[dict[str, str]] | None:
        return self._cache.get(self._key(query))

    def put(self, query: str, results: list[dict[str, str]]) -> None:
        self._cache.put(self._key(query), results)


class CrossCheckCache:
    """A finished cross-check per domain for 24 hours, so Vera does not repeat Scout's (or her own) Brave queries."""

    def __init__(self, directory: str | Path | None = None, hours: float = 24, clock: Callable[[], float] = time.time) -> None:
        self._cache = PageCache(directory or shared_dir("brave") / "cross", hours=hours, clock=clock)

    def get(self, domain: str) -> dict[str, Any] | None:
        return self._cache.get("cross:" + domain)

    def put(self, domain: str, result: dict[str, Any]) -> None:
        self._cache.put("cross:" + domain, result)


def guarded(search_fn: Callable[[str], list[dict[str, str]]], budget: Budget, cache: QueryCache | None, tool: str,
            error_cls: type[Exception] = RuntimeError) -> Callable[[str], list[dict[str, str]]]:
    """A search function that answers from the cache when it can, else spends one query of the task budget."""

    def wrapper(query: str) -> list[dict[str, str]]:
        hit = cache.get(query) if cache else None
        if hit is not None:
            budget.note_cache_hit()
            return hit
        try:
            budget.charge(tool)
        except BudgetReached as err:
            raise error_cls(str(err)) from err
        results = search_fn(query)
        if cache:
            cache.put(query, results)
        return results

    return wrapper


def print_line(budget: Budget) -> None:
    print(budget.line(), file=sys.stderr)
