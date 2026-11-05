"""Budget share, listed-price phrases, shortlist cards and cross-check selection."""
import check_suppliers as cs
import shortlist as sl
from common import Fetcher
from conftest import FakeNet


def test_money_and_share() -> None:
    assert sl.parse_money("₦2.5m") == 2_500_000 and sl.parse_money("500k") == 500_000 and sl.parse_money(None) is None
    assert sl.per_chair_share(2_000_000, 20) == 100_000 and sl.per_chair_share(2_000_000, None) is None


def test_card_lines_show_the_listed_price_and_never_invent() -> None:
    low = {"number": 345_000, "text": "₦345,000", "chair_type": "executive chairs"}
    lines = sl.card_lines("a.ng", "Abuja", "Lagos", "claim", low, 100_000, "+2348031112222", None)
    assert lines[0] == "a.ng (Lagos; delivery to Abuja: says it delivers)"
    assert lines[1] == "Price: from ₦345,000 listed per chair (executive chairs), over the ₦100,000 per-chair share, including delivery"
    assert lines[2] == "Contact: +2348031112222, no email found; call or WhatsApp +2348031112222"
    assert sl.card_lines("b.ng", "Abuja", None, "unknown", None, None, None, None) == [
        "b.ng (no address found; delivery to Abuja: not stated)", "Price: no price found", "Contact: phone not found, email not found"]


def test_a_supplier_with_a_full_address_and_a_listing_is_cross_checked_before_one_with_no_address() -> None:
    page = "<html><body><h1>Office chairs</h1>{}</body></html>"
    routes = {"https://noaddr.ng/": page.format(""), "https://addr.ng/": page.format("<p>Address: 1 Road, Garki, Abuja</p>")}
    listing = [{"source_url": "https://directory.example/x", "value": "x"}]
    report = {"request": {"product": "office chairs", "location": "Abuja"},
              "suppliers": [{"domain": "noaddr.ng", "records": []}, {"domain": "addr.ng", "records": listing}]}
    out = cs.check_suppliers(report, lambda q: [], fetcher_factory=lambda: Fetcher(opener=FakeNet(routes), max_pages=2),
                             page_fetcher_factory=lambda: Fetcher(opener=FakeNet({})), get_json=lambda u: {}, top_n=1)
    crossed = [s["domain"] for s in out["suppliers"] if s["cross_check"]]
    assert crossed == ["addr.ng"] and out["suppliers"][0]["card"][0].startswith("addr.ng (Abuja;")


def test_rdap_answers_are_cached_so_the_bootstrap_list_is_fetched_once(monkeypatch) -> None:
    import cross_check as cc

    calls: list[str] = []
    monkeypatch.setattr(cc, "_http_json", lambda url: calls.append(url) or {"services": []})
    assert cc._cached_http_json(cc.RDAP_BOOTSTRAP) == {"services": []}
    assert cc._cached_http_json(cc.RDAP_BOOTSTRAP) == {"services": []}
    assert calls == [cc.RDAP_BOOTSTRAP]  # the second lookup came from the cache


def test_a_failed_rdap_lookup_is_not_cached(monkeypatch) -> None:
    import cross_check as cc

    def boom(url: str):
        raise OSError("down")

    monkeypatch.setattr(cc, "_http_json", boom)
    for _ in range(2):
        try:
            cc._cached_http_json("https://rdap.example/domain/x.ng")
        except OSError:
            pass
    monkeypatch.setattr(cc, "_http_json", lambda url: {"ok": True})
    assert cc._cached_http_json("https://rdap.example/domain/x.ng") == {"ok": True}
