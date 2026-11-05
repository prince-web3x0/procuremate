"""Check suppliers found by scout_search: site_check on every own-site supplier, cross_check on the best N.

Usage:
    python check_suppliers.py out/search.json [--top 5] [--out out/checks.json] [--resume]

Every supplier's pages are fetched once (robots.txt respected, page-capped) and the free RDAP domain-age
lookup is run for all of them. Brave searches (at most 2 per supplier) are spent only on the top N, chosen by:
  1. product fit (yes, then unclear; a supplier that does not sell the product is never cross-checked),
  2. strong evidence (an independent mention in the search results, or a domain at least 5 years old),
  3. where it is based (an address on its own pages), then whether it says it delivers to the buyer's city.
For each supplier the output separates "based in" (only with an address on its own pages) from "delivers to"
(confirmed, or the supplier's claim), lists phones and emails with where each was found, names the quote
recipient (an address from the supplier's own pages, never guessed), and says why a site could not be checked.
Progress is written to --out after every supplier; --resume continues a cut-off run. Total time is printed.
Signals are not proof of fraud.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date
from typing import Any, Callable

from common import Fetcher, dns_check, polite_fetcher, SecretError, format_run_summary, load_secret, normalize_domain, now_iso, parse_html, seconds_text
from demo import DEMO_NOTICE, DEMO_SUMMARY_PREFIX, demo_available, demo_requested, make_opener, make_rdap, make_search
from cross_check import MAX_INDEPENDENT_PAGES, GetJson, _cached_http_json, cross_check_pages, domain_age
from delivery import LABELS, delivery_label
from budget import BUDGET_REACHED, Budget, CrossCheckCache, QueryCache, guarded, print_line as print_budget
from progress import ProgressFile, progress_path
from replay import load_bundle, notice, replay_checks, replay_path, replay_speed
from relevance import is_known_overseas, nigeria_evidence
from regions import based_in as find_based_in, location_terms, profile_for, regions_in
from scout_search import SearchError, SearchFn, _write_json, brave_search, classify, count_unsupported, paced
from shortlist import budget_fit, card_lines, lowest_listed_price, parse_money, per_chair_share, price_phrase
from site_check import collect_pages, product_terms, site_report

DEFAULT_TOP = 5
SITE_MAX_PAGES = 5
LONG_DOMAIN_YEARS = 5.0
FIT_ORDER = {"yes": 0, "unclear": 1, "no": 2}
DELIVERY_STATUS = {"confirmed": "confirmed", "advertised": "claim", "inferred": "inferred", "unknown": "unknown"}
MAX_RUN_FETCHES = 150  # page fetches (not counting cached ones) in one run; suppliers after the cap are listed as not checked
RETRY_PASS_WAIT = 30  # seconds before the one slower retry pass over sites that failed with DNS or timeout
NETWORK_STATES = ("NETWORK_ERROR", "TIMEOUT")
PHASES = {"checking sites": 2, "retrying sites we could not reach": 2, "cross-checking the best": 3}  # phase 1 is the search
NO_EMAIL_NOTE = "no email found; use the contact form or phone"


def counting(search_fn: SearchFn | None) -> tuple[SearchFn | None, list[int]]:
    """Wrap a search function so the number of queries actually attempted can be reported."""
    calls = [0]
    if search_fn is None:
        return None, calls

    def wrapper(query: str) -> list[dict[str, str]]:
        calls[0] += 1
        return search_fn(query)

    return wrapper, calls


def evidence_signals(supplier: dict[str, Any], age: dict[str, Any], has_address: bool = False) -> list[str]:
    """Reasons a supplier has strong evidence before any Brave cross-check is spent on it."""
    domain = supplier["domain"]
    signals = []
    if any(normalize_domain(r.get("source_url", "")) != domain for r in supplier.get("records", [])):
        signals.append("independent mention in the search results")
    if has_address:
        signals.append("full address on its own pages")
    if age.get("state") == "FOUND" and age.get("years", 0) >= LONG_DOMAIN_YEARS:
        signals.append(f"domain registered {age['years']} years ago")
    return signals


DOMAIN_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)


def parse_domains(value: str) -> list[str]:
    """Domains from a comma-separated list, pasted lines such as 'seats.ng (Lagos; no price)', or a text file of them. Deduplicated."""
    text = open(value, encoding="utf-8").read() if os.path.isfile(value) else value
    found: list[str] = []
    for match in DOMAIN_RE.finditer(re.sub(r"\([^)]*\)", " ", text)):  # bracket notes are not domains
        domain = normalize_domain(match.group(1))
        if domain and domain not in found:
            found.append(domain)
    return found


def report_from_domains(domains: list[str], product: str, city: str, regions: list[str]) -> dict[str, Any]:
    """A search report for a given domain list, so a check can start without a search file."""
    return {"mode": "live", "request": {"product": product, "location": city, "supplier_regions": regions},
            "suppliers": [{"domain": d, "site_url": f"https://{d}/", "records": [], "location_fit": None} for d in domains]}


def cross_budget_ok(cross: dict[str, Any]) -> bool:
    """Cache a cross-check only if its independent-listing search really ran (not stopped by the search budget)."""
    return BUDGET_REACHED not in json.dumps(cross.get("checks", {}).get("independent_listing", {}))


def lead_kind(supplier: dict[str, Any]) -> str | None:
    """What a search-file 'supplier' really is, re-checked with the current classifier (None = a real own site).

    Older search files may list a directory, article, contact database or property site as a supplier; those are leads.
    """
    domain = supplier["domain"]
    kind = classify(f"https://{domain}/")
    for record in supplier.get("records", []):
        if normalize_domain(record.get("source_url", "")) == domain and kind == "supplier_site":
            kind = classify(record["source_url"], record.get("title", ""), record.get("snippet", ""))
    return None if kind == "supplier_site" else kind


# --- based in / delivers to ------------------------------------------------------------------------

def _address_lines(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"url": p["url"], "retrieved_at": p["retrieved_at"], "lines": parse_html(p["html"])["lines"]} for p in pages]


def based_in(fields: dict[str, Any], pages: list[dict[str, Any]], regions: list[str]) -> dict[str, Any]:
    """Where the supplier is based: only an address (city or district) on its own pages counts."""
    found = find_based_in(fields["address"].get("items", []), _address_lines(pages), regions)
    if found:
        return found
    note = "no address with a city or district was found on its own pages" if pages else "no page could be fetched"
    return {"region": None, "district": None, "note": note}


def _based_nearby(fields: dict[str, Any], pages: list[dict[str, Any]], location: str) -> bool:
    text = " ".join([i["value"] for i in fields["address"].get("items", [])] + [ln for p in _address_lines(pages) for ln in p["lines"] if len(ln) < 200])
    return bool(regions_in(text, [t for t in location_terms(location)[1]]))


def _same_city(region: str | None, location: str) -> bool:
    return bool(region) and profile_for(region)[0] == profile_for(location)[0]


def _delivers_to(label: dict[str, Any], location: str) -> dict[str, Any]:
    evidence = (label.get("evidence") or [{}])[0]
    return {"city": location.strip().title(), "status": DELIVERY_STATUS[label["label"]], "reason": label["reason"],
            "source_url": evidence.get("source_url"), "retrieved_at": evidence.get("retrieved_at"), "excerpt": evidence.get("excerpt")}


def _location_rank(based: dict[str, Any], delivers: dict[str, Any], location: str) -> int:
    """0 based in the delivery city, 1 says it delivers there, 2 based elsewhere, 3 nothing known."""
    if _same_city(based.get("region"), location):
        return 0
    if delivers["status"] in ("claim", "confirmed"):
        return 1
    return 2 if based.get("region") else 3


# --- contact and quote recipient ---------------------------------------------------------------------

def main_phone(fields: dict[str, Any]) -> dict[str, Any] | None:
    """One main phone number: the one on a contact page, else the one seen on most pages, else the first. Normalized +234 form."""
    items = fields["phone"].get("items", [])
    if not items:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int]:
        pages = [item["source_url"], *item.get("seen_on", [])]
        return (1 if any(re.search(r"contact", u, re.I) for u in pages) else 0, len(set(pages)))

    best = max(items, key=score)
    return {"value": best["value"], "source_url": best["source_url"], "found_in": best.get("found_in", [])}


def quote_recipient(fields: dict[str, Any], domain: str, page_urls: list[str]) -> dict[str, Any]:
    """The address a quote request may go to: one found on the supplier's own pages, with its source. Never guessed."""
    emails = fields["email"].get("items", [])
    own = [e for e in emails if normalize_domain(e["value"].split("@")[-1]) == domain] or emails
    if own:
        first = own[0]
        return {"email": first["value"], "address_from": first["source_url"], "found_in": first.get("found_in", []),
                "other_emails": [e["value"] for e in own[1:]]}
    page = next((u for u in page_urls if re.search(r"contact", u, re.I)), page_urls[0] if page_urls else None)
    notes = [n["kind"] for n in fields["phone"].get("notes", []) + fields["email"].get("notes", []) if n["kind"] not in ("no phone found", "no email found")]
    phone = main_phone(fields)
    note = f"no email found; call or WhatsApp {phone['value']}" if phone else NO_EMAIL_NOTE
    return {"email": None, "note": note, "page": page, "phone": phone["value"] if phone else None,
            "phones": [p["value"] for p in fields["phone"].get("items", [])], "other_contact": sorted(set(notes))}


def contact_block(fields: dict[str, Any], domain: str, page_urls: list[str]) -> dict[str, Any]:
    return {
        "main_phone": main_phone(fields),
        "phones": fields["phone"].get("items", []), "multiple_phone_numbers": bool(fields["phone"].get("multiple_numbers")),
        "emails": fields["email"].get("items", []),
        "notes": fields["phone"].get("notes", []) + [n for n in fields["email"].get("notes", []) if n["kind"] == "no email found"],
        "quote_recipient": quote_recipient(fields, domain, page_urls),
    }


# --- handoff counts ----------------------------------------------------------------------------------

MAX_NOTE_WORDS = 11  # chat lines: a short bracket note per supplier (room for a listed price such as 'from ₦345,000, executive chairs')


def short_line(result: dict[str, Any]) -> str:
    """One chat line for a supplier: 'domain (note)', the note at most 8 words. No phones, links or dates."""
    domain = result["domain"]
    if result["check_status"] == "could not be checked":
        reason = result["why_not_checked"][0]["state"] if result["why_not_checked"] else ""
        note = "couldn't reach it (our side)" if reason in NETWORK_STATES else "couldn't check its site"
        return f"{domain} ({note})"
    based = result["based_in"].get("region")
    delivers = {"confirmed": "another source says it delivers", "claim": "says it delivers"}.get(result["delivers_to"]["status"], "no delivery info")
    parts = [f"based {based}" if based else "no address", delivers, price_phrase(result.get("lowest_listed_price"))]
    words = "; ".join(parts).split()
    return f"{domain} ({' '.join(words[:MAX_NOTE_WORDS])})"


SHORTLIST_SIZE = 5


def shortlist_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The best few for the automatic shortlist: chair fit yes first, then budget fit, then evidence, then location.

    Scout merges Vera's 'Quote first' to the top; each card has three lines (the fourth is Vera's sentence).
    """
    def key(r: dict[str, Any]) -> tuple[int, int, int, int, int]:
        fit = FIT_ORDER.get((r["product_fit"] or {}).get("state", "unclear"), 1)
        return (fit, BUDGET_ORDER[r["budget_fit"]], 0 if r["cross_check"] else 1, _location_rank(r["based_in"], r["delivers_to"], ""), r["rank"])

    usable = [r for r in results if (r["product_fit"] or {}).get("state") in ("yes", "unclear") and r["check_status"] != "could not be checked"]
    return [{"domain": r["domain"], "card": r["card"], "budget_fit": r["budget_fit"], "product_fit": (r["product_fit"] or {}).get("state")}
            for r in sorted(usable, key=key)[:SHORTLIST_SIZE]]


def handoff_counts(search_report: dict[str, Any], results: list[dict[str, Any]], excluded: list[dict[str, str]],
                   overseas: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """The counts for the handoff: Nigerian own-site suppliers only; overseas sites and leads are counted apart."""
    request = search_report.get("request") or {}
    location = request.get("location") or "the buyer's city"
    term = (product_terms(request.get("product") or "") or ["matching"])[0].capitalize()
    city = location.strip().title()
    chair = [r for r in results if (r["product_fit"] or {}).get("state") == "yes"]
    listed = [r for r in results if (r["product_fit"] or {}).get("state") in ("yes", "unclear")]  # the candidates: the counts below never contradict this list
    based = [r for r in listed if _same_city(r["based_in"].get("region"), location)]
    elsewhere = [r for r in listed if r["based_in"].get("region") and r not in based]
    serves = [r for r in listed if r not in based and r["delivers_to"]["status"] in ("claim", "confirmed")]

    def unclear(group: list[dict[str, Any]]) -> str:
        n = sum(1 for r in group if (r["product_fit"] or {}).get("state") == "unclear")
        return f", {n} fit unclear" if n else ""

    leads = len(search_report.get("leads_short_list") or []) + len(excluded)
    overseas_count = len(overseas or [])
    counts = {"relevant_suppliers": len(results), "suppliers_found": len(results), "chair_sellers": len(chair),
              "chair_sellers_unclear": sum(1 for r in results if (r["product_fit"] or {}).get("state") == "unclear"),
              "based_in_city": len(based), "based_in_other_regions": len(elsewhere), "serves_city": len(serves),
              "overseas_excluded": overseas_count, "leads_only": leads, "label": term}
    counts["lines"] = [
        f"- Relevant suppliers (Nigeria): {len(results)} (own sites only)",
        f"- {term} sellers (Nigeria): {len(chair)}",
        f"- Based in {city}: {len(based)} (an address there on their own pages{unclear(based)})",
        f"- Based in other chosen regions: {len(elsewhere)} (an address there on their own pages{unclear(elsewhere)})",
        f"- Serve {city}: {len(serves)} (say they deliver there, no address there{unclear(serves)})",
        f"- Overseas excluded: {overseas_count} (not relevant)",
        f"- Leads only: {leads} (directories, marketplaces, social)",
    ]
    mark = unclear(based).replace(", ", " (", 1) + ")" if unclear(based) else ""
    counts["compact_lines"] = [  # the three bullets under **Found** in the handoff
        f"- {len(results)} relevant suppliers, {len(chair)} {term.lower()} sellers",
        f"- Based in {city}: {len(based)}{mark} / other regions: {len(elsewhere)} / serve {city}: {len(serves)}",
        f"- Overseas excluded: {overseas_count}, leads only: {leads}",
    ]
    return counts


def _items_in(obj: Any) -> list[dict[str, Any]]:
    """Every evidence item (dicts inside an 'items' list) anywhere in a nested result."""
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if isinstance(obj.get("items"), list):
            found += [i for i in obj["items"] if isinstance(i, dict)]
        for value in obj.values():
            found += _items_in(value)
    elif isinstance(obj, list):
        for value in obj:
            found += _items_in(value)
    return found


def count_unsupported_items(*results: Any) -> int:
    """Evidence items that would be a claim without a source: no source_url, retrieved_at or excerpt."""
    return sum(1 for r in results for i in _items_in(r) if not (i.get("source_url") and i.get("retrieved_at") and i.get("excerpt")))


def check_summary_rows(s: dict[str, Any]) -> list[tuple[str, str]]:
    fit, labels = s["product_fit_counts"], s["delivery_labels"]
    dup = s["duplicates_removed_in_search"]
    return [
        ("stage seconds", seconds_text(s["stage_seconds"])),
        (s["queries_label"], str(s["queries_used"])),
        ("search file: queries used", str(s["search_queries_used"]) if s["search_queries_used"] is not None else "n/a"),
        ("own-site suppliers checked", str(s["own_site_suppliers_checked"])),
        ("overseas excluded (not relevant)", str(s["overseas_excluded"])),
        ("page fetches / not checked (fetch cap)", f"{s['page_fetches']} / {s['not_checked_fetch_cap']}"),
        ("retry pass over network failures", f"{s['retry_pass_sites']} sites"),
        ("sites that could not be checked", f"{s['could_not_be_checked']} (reasons are in the output)"),
        ("duplicates removed (search)", str(dup) if dup is not None else "n/a"),
        ("chair sellers (fit yes)", f"{fit['yes']} (unclear {fit['unclear']}, no {fit['no']})"),
        ("suppliers with a price", f"{s['suppliers_with_a_price']} ({s['suppliers_with_a_price_for_product']} for the product)"),
        ("delivery labels", " | ".join(f"{name} {labels[name]}" for name in LABELS)),
        ("unsupported claims", str(s["unsupported_claims"])),
        ("total time", f"{s['stage_seconds']['total']:.1f} s"),
    ]


BUDGET_ORDER = {"within": 0, "unknown": 1, "over": 2}


def _rank_key(entry: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    """Who gets a cross-check: (a) chair fit, (b) independent listing or full address, (c) budget fit, then location.

    A supplier is never left out only because it is in another city; a mere mention in the search file only breaks ties.
    """
    return (
        FIT_ORDER.get((entry["product_fit"] or {}).get("state", "unclear"), 1),
        max(0, 2 - len(entry["evidence_signals"])),
        BUDGET_ORDER[entry["budget_fit"]],
        entry["location_rank"],
        ((entry["location_fit"] or {}).get("rank", 3)),
        entry["_index"],
    )


def _why(entry: dict[str, Any]) -> str:
    fit = (entry["product_fit"] or {}).get("state", "unclear")
    where = {0: "based in the delivery city", 1: "says it delivers there", 2: "based elsewhere", 3: "location unknown"}[entry["location_rank"]]
    return "; ".join([f"product fit {fit}", *entry["evidence_signals"], where])


def _check_status(site: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    pages = site["pages"]
    failed = [{"role": a.get("role"), "url": a["url"], "state": a["state"], "why": a.get("why", ""), "robots_rule": a.get("robots_rule", "")}
              for a in pages if a["state"] != "OK"]
    if not pages or pages[0]["state"] != "OK":
        return "could not be checked", failed
    return ("partly checked" if failed else "checked"), failed


def check_suppliers(
    search_report: dict[str, Any],
    search_fn: SearchFn | None,
    fetcher_factory: Callable[[], Fetcher] = lambda: polite_fetcher(SITE_MAX_PAGES),
    page_fetcher_factory: Callable[[], Fetcher] = lambda: polite_fetcher(MAX_INDEPENDENT_PAGES),
    top_n: int = DEFAULT_TOP,
    get_json: GetJson = _cached_http_json,
    today: date | None = None,
    mode: str = "live",
    clock: Callable[[], float] = time.monotonic,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    resume: dict[str, Any] | None = None,
    max_fetches: int = MAX_RUN_FETCHES,
    sleep: Callable[[float], None] = time.sleep,
    network_check: dict[str, Any] | None = None,
    budget: int | None = None,
    quantity: int | None = None,
    cross_cache: Any = None,
) -> dict[str, Any]:
    """mode: 'live' or 'demo' (saved fixtures; the output says so).

    on_progress gets a partial report after every supplier; resume is such a partial report from an earlier run.
    """
    demo = mode == "demo"
    request = search_report.get("request") or {}
    product, location = request.get("product"), request.get("location") or ""
    regions = list(dict.fromkeys([location, *(request.get("supplier_regions") or [])])) if location else list(request.get("supplier_regions") or [])
    noun = (product_terms(product or "") or ["chair"])[0]  # the product word from the request ("desk", "chair"): chat wording and price phrases use it
    share = per_chair_share(budget or request.get("budget"), quantity or request.get("quantity"))
    counted, calls = counting(search_fn)
    excluded = [{"domain": s["domain"], "kind": k} for s in search_report.get("suppliers", []) if (k := lead_kind(s))]
    real_suppliers = [s for s in search_report.get("suppliers", []) if not lead_kind(s)]
    overseas = {o["domain"]: {"domain": o["domain"], "reason": o.get("reason", "a known overseas retailer or manufacturer")} for o in search_report.get("overseas_excluded", [])}
    for s in [s for s in real_suppliers if is_known_overseas(s["domain"])]:
        overseas[s["domain"]] = {"domain": s["domain"], "reason": "a known overseas retailer or manufacturer"}  # never fetched
    real_suppliers = [s for s in real_suppliers if not is_known_overseas(s["domain"])]
    skipped: list[dict[str, str]] = []
    fetches = 0
    prior = {e["domain"]: e for e in (resume or {}).get("suppliers", [])}
    stages = {"site_checks": 0.0, "domain_age": 0.0, "retry_pass": 0.0, "cross_checks": 0.0}
    began = clock()
    done: dict[str, dict[str, Any]] = {}  # domain -> progress entry (resumable)

    def partial(phase: str, stage: str = "", items_done: int = 0, items_total: int | None = None) -> dict[str, Any]:
        return {"status": "partial", "progress": {"phase": phase, "stage": stage or phase, "items_done": items_done, "items_total": items_total,
                                                  "suppliers_done": len(done), "suppliers_total": len(real_suppliers),
                                                  "elapsed_seconds": round(clock() - began, 1)},
                "request": request, "mode": mode, "suppliers": list(done.values())}

    def save(phase: str, stage: str = "", items_done: int = 0, items_total: int | None = None) -> None:
        if on_progress:
            on_progress(partial(phase, stage, items_done, items_total))

    checked: list[dict[str, Any]] = []
    save("site checks start", "checking sites", 0, len(real_suppliers))
    for index, supplier in enumerate(real_suppliers):
        domain = supplier["domain"]
        old = prior.get(domain)
        if old and old.get("site_check"):  # finished before the run was cut off: reuse, do not visit again
            site, age, attempts, pages = old["site_check"], old["domain_age"], old["site_check"]["pages"], None
        elif fetches >= max_fetches:
            skipped.append({"domain": domain, "reason": f"not checked: the cap of {max_fetches} page fetches for this run was reached"})
            continue
        else:
            mark = clock()
            attempts, pages = collect_pages(domain, fetcher_factory())
            fetches += sum(1 for a in attempts if not a.get("from_cache"))
            site = site_report(domain, attempts, pages, product, supplier.get("records"))
            stages["site_checks"] += clock() - mark
            mark = clock()
            age = domain_age(domain, get_json, today)  # RDAP: free, no Brave query
            stages["domain_age"] += clock() - mark
        done[domain] = {"domain": domain, "stage": "site checked", "site_check": site, "domain_age": age, "cross_check": (old or {}).get("cross_check"),
                        "cross_check_state": (old or {}).get("cross_check_state")}
        save(f"site checks {index + 1}/{len(real_suppliers)}", "checking sites", len(checked) + 1, len(real_suppliers))
        checked.append({"index": index, "supplier": supplier, "old": old, "site": site, "age": age, "attempts": attempts, "pages": pages})

    # One slower retry pass over every site that failed with DNS or a timeout: that is our network, not a verdict on the site.
    failed = [c for c in checked if c["pages"] is not None and c["attempts"] and c["attempts"][0]["state"] in NETWORK_STATES]
    retried = 0
    if failed:
        sleep(RETRY_PASS_WAIT)
        mark = clock()
        for c in failed:
            attempts, pages = collect_pages(c["supplier"]["domain"], fetcher_factory())
            retried += 1
            if not attempts or attempts[0]["state"] != c["attempts"][0]["state"] or pages:
                c.update(attempts=attempts, pages=pages, site=site_report(c["supplier"]["domain"], attempts, pages, product, c["supplier"].get("records")))
                done[c["supplier"]["domain"]]["site_check"] = c["site"]
            c["retried"] = True
        stages["retry_pass"] = clock() - mark
        save("retry pass", "retrying sites we could not reach", retried, len(failed))

    staged: list[dict[str, Any]] = []
    for c in checked:
        index, supplier, old, site, age, attempts, pages = c["index"], c["supplier"], c["old"], c["site"], c["age"], c["attempts"], c["pages"]
        domain = supplier["domain"]
        fields = site["fields"]
        if attempts and attempts[0]["state"] == "OK" and not nigeria_evidence(domain, fields)["relevant"]:
            overseas[domain] = {"domain": domain, "reason": nigeria_evidence(domain, fields)["why_not"]}
            done.pop(domain, None)
            continue
        fields = site["fields"]
        page_urls = [a["url"] for a in attempts if a["state"] == "OK"]
        based = based_in(fields, pages or [], regions) if pages is not None else (old or {}).get("based_in") or {"region": None, "note": "not re-read after resume"}
        near = bool(pages) and _based_nearby(fields, pages, location) if location else False
        base_rank = 0 if _same_city(based.get("region"), location) else (1 if near else 3)
        label = delivery_label(location_terms(location)[0] if location else [], domain, base_rank, fields, supplier.get("records"), None)
        delivers = _delivers_to(label, location or "the buyer's city")
        lowest = lowest_listed_price(fields["prices"].get("items", []), noun)
        has_address = bool(fields["address"].get("items")) or bool(based.get("region"))
        staged.append({
            "lowest": lowest, "budget_fit": budget_fit(lowest, share), "domain": domain, "location_fit": supplier.get("location_fit"), "product_fit": site.get("product_fit"), "based_in": based, "delivers_to": delivers,
            "location_rank": _location_rank(based, delivers, location), "evidence_signals": evidence_signals(supplier, age, has_address), "site_check": site,
            "contact": contact_block(fields, domain, page_urls), "_attempts": attempts, "_pages": pages, "_age": age, "_index": index,
            "_records": supplier.get("records", []), "_old": old, "_retried": bool(c.get("retried")),
        })

    chosen = 0
    cross_total = min(top_n, sum(1 for e in staged if (e["product_fit"] or {}).get("state", "unclear") != "no"))  # how many cross-checks will run
    results: list[dict[str, Any]] = []
    for position, entry in enumerate(sorted(staged, key=_rank_key), start=1):
        fit = (entry["product_fit"] or {}).get("state", "unclear")
        cross, state = None, f"not run (outside the top {top_n} by product fit, then evidence, then location)"
        old = entry["_old"] or {}
        if fit == "no":
            state = "not run (does not appear to sell the product)"
        elif chosen < top_n:
            chosen += 1
            if old.get("cross_check"):
                cross, state = old["cross_check"], "run"
            else:
                pages = entry["_pages"]
                if pages is None:  # resumed: the pages were not kept, fetch them once more for the cross-check
                    _, pages = collect_pages(entry["domain"], fetcher_factory())
                save(f"cross-checks {chosen - 1}/{top_n}", "cross-checking the best", chosen - 1, cross_total)
                mark = clock()
                cross = cross_cache.get(entry["domain"]) if cross_cache else None
                if cross:  # a cross-check done in the last 24 hours (by Scout or Vera): no new Brave queries
                    state = "run (reused from a check in the last 24 hours)"
                else:
                    cross = cross_check_pages(entry["domain"], entry["_attempts"], pages, counted, page_fetcher_factory(), get_json, today, entry["_age"])
                    if cross_cache and cross_budget_ok(cross):
                        cross_cache.put(entry["domain"], cross)
                    state = "run"
                stages["cross_checks"] += clock() - mark
        done[entry["domain"]] = {**done[entry["domain"]], "stage": "cross-checked" if cross else "site checked", "cross_check": cross, "cross_check_state": state, "based_in": entry["based_in"]}
        save(f"cross-checks {chosen}/{top_n}", "cross-checking the best", chosen, cross_total)
        site_status, why_not = _check_status(entry["site_check"])
        final_label = delivery_label(location_terms(location)[0] if location else [], entry["domain"],
                                     0 if _same_city(entry["based_in"].get("region"), location) else 3, entry["site_check"]["fields"], entry["_records"], cross)
        results.append({
            "rank": position, "domain": entry["domain"], "check_status": site_status, "why_not_checked": why_not,
            "retried_after_network_error": entry["_retried"], "relevance": nigeria_evidence(entry["domain"], entry["site_check"]["fields"]),
            "product_fit": entry["product_fit"], "based_in": entry["based_in"], "delivers_to": _delivers_to(final_label, location or "the buyer's city"),
            "location_fit": entry["location_fit"], "selection_reason": _why(entry), "contact": entry["contact"],
            "site_check": entry["site_check"], "delivery": final_label,
            "domain_age": entry["_age"], "cross_check": cross, "cross_check_state": state,
            "lowest_listed_price": entry["lowest"], "budget_fit": entry["budget_fit"],
        })
        results[-1]["short_line"] = short_line(results[-1])
        results[-1]["card"] = card_lines(entry["domain"], location or "the buyer's city", entry["based_in"].get("region"), results[-1]["delivers_to"]["status"],
                                         entry["lowest"], share, (entry["contact"]["main_phone"] or {}).get("value"),
                                         entry["contact"]["quote_recipient"].get("email"), noun)
    fits = [(r["product_fit"] or {}).get("state", "unclear") for r in results]
    queries_label = "Demo queries read from fixtures (no Brave call)" if demo else "Brave queries used for cross-checks"
    stages["total"] = clock() - began
    priced = [r for r in results if r["site_check"]["fields"]["prices"]["state"] == "FOUND"]
    search_summary = search_report.get("run_summary") or {}
    run_summary = {
        "mode": mode,
        "stage_seconds": {name: round(secs, 2) for name, secs in stages.items()},
        "queries_label": queries_label,
        "queries_used": calls[0],
        "search_queries_used": search_summary.get("queries_used"),
        "own_site_suppliers_checked": len(results),
        "overseas_excluded": len(overseas),
        "not_checked_fetch_cap": len(skipped),
        "page_fetches": fetches,
        "retry_pass_sites": retried,
        "dns_warning": (network_check or {}).get("warning"),
        "could_not_be_checked": sum(1 for r in results if r["check_status"] == "could not be checked"),
        "duplicates_removed_in_search": search_summary.get("duplicates_removed"),
        "product_fit_counts": {name: fits.count(name) for name in ("yes", "unclear", "no")},
        "suppliers_with_a_price": len(priced),
        "suppliers_with_a_price_for_product": sum(1 for r in priced if any(i.get("matches_product") for i in r["site_check"]["fields"]["prices"]["items"])),
        "delivery_labels": {name: sum(1 for r in results if r["delivery"]["label"] == name) for name in LABELS},
        "unsupported_claims": count_unsupported([x for s in real_suppliers for x in s.get("records", [])])
        + count_unsupported_items([{"site_check": r["site_check"], "cross_check": r["cross_check"], "domain_age": r["domain_age"], "delivery": r["delivery"]} for r in results]),
        "note": "This summary is for the output, not for chat.",
    }
    return {
        "mode": mode,
        "status": "complete",
        "progress": {"phase": "done", "suppliers_done": len(results), "suppliers_total": len(real_suppliers), "elapsed_seconds": round(stages["total"], 1)},
        "data_source": DEMO_NOTICE if demo else "live supplier pages, Brave Search and RDAP",
        "checked_at": now_iso(),
        "request": search_report.get("request"),
        "summary": (DEMO_SUMMARY_PREFIX if demo else "") + (
            f"Site-checked {len(results)} own-site suppliers (product fit: {fits.count('yes')} yes, {fits.count('unclear')} unclear, "
            f"{fits.count('no')} no); {run_summary['could_not_be_checked']} could not be checked; cross-checked {chosen} by product fit, then evidence, then location. "
            f"{queries_label}: {calls[0]}."
        ),
        "brave_queries_used": calls[0],
        "run_summary": run_summary,
        "handoff_counts": handoff_counts(search_report, results, excluded, list(overseas.values())),
        "per_chair_share": {"naira": share, "note": "budget divided by quantity, including delivery; compared with listed prices only"} if share else None,
        "shortlist_candidates": shortlist_candidates(results),
        "overseas_excluded": list(overseas.values()),
        "not_checked": skipped,
        "network_check": network_check,
        "excluded_as_leads": excluded,
        "suppliers": results,
        "note": "Signals only: a new domain, a missing listing or a mismatch is never proof of fraud.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("search_json", nargs="?", help="JSON file written by scout_search.py --out (or use --domains)")
    parser.add_argument("--domains", help="check these domains instead of a search file: comma-separated, pasted handoff lines, or a text file")
    parser.add_argument("--product", help="the product, for product fit (with --domains)")
    parser.add_argument("--city", help="the delivery city (with --domains)")
    parser.add_argument("--regions", help="supplier regions, comma-separated (with --domains)")
    parser.add_argument("--budget", help="the buyer's budget, for the per-chair share (e.g. 2500000 or 2.5m); with --quantity")
    parser.add_argument("--quantity", type=int, help="how many units, for the per-unit share")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--out", help="write JSON here (also written after every supplier, so a cut-off run leaves partial results)")
    parser.add_argument("--resume", action="store_true", help="continue a cut-off run from the partial --out file")
    parser.add_argument("--progress", help="progress file (default: next to --out, as <name>.progress.json); print it with progress.py")
    parser.add_argument("--replay", help="replay a saved live run from this bundle (or set REPLAY_FILE): no Brave call, no site fetched; see save_replay.py")
    parser.add_argument("--replay-speed", type=float, help="replay speed-up for the stage timings (default 5, or REPLAY_SPEED)")
    args = parser.parse_args(argv)
    replay = replay_path(args.replay)
    if replay:
        try:
            bundle = load_bundle(replay)
        except ValueError as err:
            print(f"Error: {err}", file=sys.stderr)
            return 2
        print(notice(bundle), file=sys.stderr)
        wanted = parse_domains(args.domains) if args.domains else None
        report = replay_checks(bundle, ProgressFile(args.progress or progress_path(args.out), "check_suppliers"), replay_speed(args.replay_speed), domains=wanted)
        if args.out:
            _write_json(args.out, report)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    if args.domains:
        domains = parse_domains(args.domains)
        if not domains or not args.city or not args.product:
            print("Error: --domains needs at least one domain, --product and --city.", file=sys.stderr)
            return 2
        search_report = report_from_domains(domains, args.product, args.city, [x.strip() for x in (args.regions or "").split(",") if x.strip()])
    elif args.search_json:
        with open(args.search_json, encoding="utf-8") as handle:
            search_report = json.load(handle)
    else:
        print("Error: give a search file or --domains.", file=sys.stderr)
        return 2
    previous: dict[str, Any] | None = None
    if args.resume and args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as handle:
            previous = json.load(handle)
        if previous.get("status") == "complete":
            print(f"{args.out} is already a complete run; delete it to check again.", file=sys.stderr)
            return 0
        print(f"Resuming: {len(previous.get('suppliers', []))} suppliers from the partial run are reused.", file=sys.stderr)
    # Demo mode: DEMO_MODE=1, or no key when the search file itself came from demo mode.
    demo = demo_requested()
    search_fn: SearchFn | None = None
    if not demo:
        try:
            key = load_secret("BRAVE_API_KEY")
            task_budget = Budget()
            search_fn = guarded(paced(lambda q: brave_search(q, key)), task_budget, QueryCache(), "vera_check_suppliers" if args.domains else "check_suppliers", SearchError)
        except SecretError as err:
            if search_report.get("mode") == "demo":
                demo = True
            else:
                print(f"Warning: independent-listing searches skipped ({err})", file=sys.stderr)
    if demo and not demo_available():
        print("Error: demo mode needs the demo/ fixtures from a full clone of the repo.", file=sys.stderr)
        return 2
    last: dict[str, Any] = {}

    progress_file = ProgressFile(args.progress or progress_path(args.out), "check_suppliers")

    def save(partial: dict[str, Any]) -> None:
        last.clear()
        last.update(partial)
        if args.out:
            _write_json(args.out, partial)
        p = partial.get("progress", {})
        stage = p.get("stage", "starting")
        progress_file.update(stage, p.get("items_done", 0), p.get("items_total"), p.get("suppliers_done", 0),
                             interrupted=partial.get("status") == "interrupted", phase=PHASES.get(stage))

    started = time.monotonic()
    network = None if demo else dns_check()
    if network and network["warning"]:
        print(f"Warning: {network['warning']}", file=sys.stderr)
    kwargs: dict[str, Any] = {"top_n": max(0, args.top), "on_progress": save, "resume": previous, "network_check": network,
                              "budget": parse_money(args.budget), "quantity": args.quantity}
    try:
        if demo:
            print(DEMO_NOTICE, file=sys.stderr)
            report = check_suppliers(
                search_report, make_search(),
                fetcher_factory=lambda: Fetcher(opener=make_opener(), max_pages=SITE_MAX_PAGES),
                page_fetcher_factory=lambda: Fetcher(opener=make_opener(), max_pages=MAX_INDEPENDENT_PAGES),
                get_json=make_rdap(), mode="demo", **kwargs,
            )
        else:
            report = check_suppliers(search_report, search_fn, cross_cache=CrossCheckCache(), **kwargs)
            if search_fn is not None:
                report.setdefault("run_summary", {})["brave_budget"] = task_budget.summary()
                print_budget(task_budget)
    except BaseException:
        if last:
            save({**last, "status": "interrupted"})
            print(f"Interrupted after {time.monotonic() - started:.0f} s. Partial results are in {args.out}; run again with --resume.", file=sys.stderr)
        raise
    if args.out:
        _write_json(args.out, report)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    done_count = (report.get("progress") or {}).get("suppliers_done", len(report.get("suppliers", [])))
    progress_file.update("cross-checking the best", done_count, done_count, done_count, finished=True, phase=3)
    if "run_summary" in report:
        print(format_run_summary("demo" if demo else "live", check_summary_rows(report["run_summary"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
