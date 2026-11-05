"""The buyer's decisions: shortlist, exclude, add, re-add, recheck, order, quote targets, and the DECISIONS message."""
from pathlib import Path
from typing import Any

import pytest

import decisions as dc


def _shown() -> dict[str, Any]:
    return dc.set_shown(dc.new_state(), ["a.ng", "b.ng", "c.ng", "d.ng"])


def test_numbers_refer_to_the_last_shown_shortlist_and_domains_work_too() -> None:
    s = dc.shortlist(_shown(), ["1", "3"])
    assert s["shortlisted"] == ["a.ng", "c.ng"]
    assert dc.shortlist(s, ["https://www.D.ng/"])["shortlisted"] == ["a.ng", "c.ng", "d.ng"]


@pytest.mark.parametrize("token", ["0", "5", "-1", "nonsense"])
def test_a_bad_number_or_word_is_refused(token: str) -> None:
    with pytest.raises(ValueError):
        dc.shortlist(_shown(), [token])


def test_exclude_removes_from_the_shortlist_and_keeps_the_reason() -> None:
    s = dc.shortlist(_shown(), ["1", "2"])
    s = dc.exclude(s, ["2"], "too far")
    assert s["shortlisted"] == ["a.ng"] and s["excluded"] == {"b.ng": "too far"}


def test_a_re_added_supplier_leaves_the_excluded_list_and_comes_back_at_the_end() -> None:
    s = dc.exclude(dc.shortlist(_shown(), ["1", "2"]), ["1"], "price")
    s = dc.shortlist(s, ["a.ng"])
    assert s["excluded"] == {} and s["shortlisted"] == ["b.ng", "a.ng"]


def test_add_shortlists_a_new_domain_marked_as_needing_a_check() -> None:
    s = dc.add(_shown(), ["newcomer-seats.example"])
    assert s["shortlisted"] == ["newcomer-seats.example"] and s["needs_check"] == ["newcomer-seats.example"]
    assert "newcomer-seats.example (new, to be checked)" in dc.render(s)
    assert dc.mark_resolved(s, ["newcomer-seats.example"])["needs_check"] == []


def test_adding_a_domain_already_shown_does_not_need_a_new_check() -> None:
    assert dc.add(_shown(), ["a.ng"])["needs_check"] == []


def test_shortlisted_suppliers_always_appear_in_the_buyers_order_and_excluded_never() -> None:
    s = dc.exclude(dc.shortlist(_shown(), ["3", "1"]), ["2"], "")
    assert dc.order(s, ["a.ng", "b.ng", "c.ng", "d.ng"]) == ["c.ng", "a.ng", "d.ng"]
    assert dc.order(s, ["d.ng"]) == ["c.ng", "a.ng", "d.ng"]  # shortlisted ones appear even when the new list lacks them


def test_quote_targets_are_the_shortlisted_or_the_named_never_the_excluded() -> None:
    s = dc.exclude(dc.shortlist(_shown(), ["1", "2", "3"]), ["2"], "")
    assert dc.quote_targets(s) == ["a.ng", "c.ng"]
    assert dc.quote_targets(s, ["4"]) == ["d.ng"]  # a named supplier counts
    assert dc.quote_targets(dc.new_state()) == []


def test_go_with_one_shortlists_it_and_marks_only_it_for_a_quote_request() -> None:
    s = dc.go(_shown(), ["1"])
    assert s["shortlisted"] == ["a.ng"] and dc.quote_targets(s) == ["a.ng"]
    assert "Quote requests to prepare:** a.ng (nothing sent)" in dc.render(s)


def test_an_excluded_quote_target_is_dropped() -> None:
    s = dc.exclude(dc.go(_shown(), ["1"]), ["1"], "no")
    assert dc.quote_targets(s) == [] and "Quote requests" not in dc.render(s)


def test_recheck_records_the_question_and_resolve_clears_it() -> None:
    s = dc.recheck(_shown(), ["2"], "delivery cost")
    assert s["recheck"] == [{"domain": "b.ng", "question": "delivery cost"}]
    assert "**Asked to recheck:** b.ng (delivery cost)" in dc.render(s)
    assert dc.mark_resolved(s, ["2"])["recheck"] == []
    assert len(dc.recheck(s, ["2"], "lead time")["recheck"]) == 1  # one open question per supplier


def test_the_message_is_the_current_state_only() -> None:
    s = dc.shortlist(_shown(), ["1", "3"])
    s = dc.exclude(s, ["3"], "over budget")
    assert dc.render(s) == "**DECISIONS**\n\n- **Shortlisted:** a.ng\n- **Excluded:** c.ng (over budget)"
    assert dc.render(dc.new_state()) == "**DECISIONS**\n\n- Nothing decided yet."


def test_the_command_keeps_state_between_calls_and_prints_the_message(tmp_path: Path, capsys: Any) -> None:
    f = str(tmp_path / "d.json")
    assert dc.main(["shown", "a.ng", "b.ng", "c.ng", "--file", f]) == 0
    assert dc.main(["shortlist", "1", "3", "--file", f]) == 0
    capsys.readouterr()
    assert dc.main(["exclude", "3", "--reason", "too far", "--file", f]) == 0
    assert capsys.readouterr().out.strip() == "**DECISIONS**\n\n- **Shortlisted:** a.ng\n- **Excluded:** c.ng (too far)"
    assert dc.main(["order", "b.ng", "c.ng", "a.ng", "--file", f]) == 0
    assert capsys.readouterr().out.split() == ["a.ng", "b.ng"]
    assert dc.main(["shortlist", "9", "--file", f]) == 2 and "no number 9" in capsys.readouterr().err
