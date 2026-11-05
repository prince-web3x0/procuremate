"""Delivery label per supplier, in plain words: confirmed, advertised, inferred or unknown.

  confirmed  - a source other than the supplier says it delivers to the buyer's city
  advertised - the supplier itself says so (its pages or its own search result), or says it delivers nationwide
  inferred   - no delivery statement, but the supplier is based in or near the buyer's city
  unknown    - none of the above

Nothing is guessed beyond these rules; each label carries the evidence it came from.
"""
import re
from typing import Any

from common import normalize_domain

DELIVERY_WORD = r"(?:deliver\w*|ship\w*|dispatch\w*|distribut\w*)"
NATIONWIDE = re.compile(r"nationwide|countrywide|all states|across nigeria|anywhere in nigeria", re.IGNORECASE)
LABELS = ("confirmed", "advertised", "inferred", "unknown")


def _city_pattern(city_terms: list[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(t.lower()) for t in city_terms)
    return re.compile(rf"{DELIVERY_WORD}[^.;]{{0,80}}?\b(?:{alt})\b|\b(?:{alt})\b[^.;]{{0,40}}{DELIVERY_WORD}", re.IGNORECASE)


def _evidence(value: str, item: dict[str, Any], excerpt: str) -> dict[str, str]:
    return {"value": value, "source_url": item.get("source_url", ""), "retrieved_at": item.get("retrieved_at", ""), "excerpt": excerpt[:200]}


def _delivery_items(site_fields: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list(((site_fields or {}).get("delivery_statements") or {}).get("items", []))


def delivery_label(
    city_terms: list[str],
    domain: str,
    location_fit_rank: int | None,
    site_fields: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
    cross_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """city_terms: the buyer's city and its districts. records: the supplier's Brave search records."""
    if not [t for t in city_terms if t.strip()]:
        return {"label": "unknown", "reason": "no buyer city given", "evidence": []}
    pattern = _city_pattern(city_terms)
    independent = ((cross_check or {}).get("checks", {}).get("independent_listing", {}) or {}).get("items", [])
    for item in independent:
        text = f"{item.get('value', '')} {item.get('excerpt', '')}"
        if pattern.search(text):
            return {"label": "confirmed", "reason": "a source other than the supplier says it delivers to the buyer's city",
                    "evidence": [_evidence(item.get("value", ""), item, item.get("excerpt", ""))]}
    own = [("site", i, i.get("value", "")) for i in _delivery_items(site_fields)]
    own += [
        ("search result", r, re.sub(r"<[^>]+>", "", f"{r.get('title', '')} {r.get('snippet', '')}"))
        for r in (records or []) if normalize_domain(r.get("source_url", "")) == domain
    ]
    for kind, item, text in own:
        if pattern.search(text):
            return {"label": "advertised", "reason": f"the supplier's {kind} says it delivers there",
                    "evidence": [_evidence(text, item, text)]}
    for kind, item, text in own:
        if NATIONWIDE.search(text) and re.search(DELIVERY_WORD, text, re.IGNORECASE):
            return {"label": "advertised", "reason": f"the supplier's {kind} says it delivers nationwide (the buyer's city is not named)",
                    "evidence": [_evidence(text, item, text)]}
    if location_fit_rank in (0, 1):
        return {"label": "inferred", "reason": "based in or near the buyer's city; no delivery statement found", "evidence": []}
    return {"label": "unknown", "reason": "no delivery statement for the buyer's city and not based nearby", "evidence": []}
