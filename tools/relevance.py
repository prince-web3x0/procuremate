"""Is a supplier Nigerian? Overseas retailers and manufacturers are counted apart and never become suppliers.

A supplier is relevant only if it is Nigerian: a .ng domain, or a Nigerian address, a Nigerian phone number
or prices in naira on its own pages. A known overseas company stays overseas unless its domain is .ng.
"""
import re
from typing import Any

from common import normalize_domain
from regions import CITY_PROFILES

# Big overseas retailers, manufacturers and global marketplace pages. A site that is not on this list and shows
# no Nigerian sign on its own pages is also treated as overseas once its pages have been read.
OVERSEAS_DOMAINS = {
    "homedepot.com", "officedepot.com", "haworth.com", "humanscale.com", "btod.com", "okamura.com", "okamura.co.jp",
    "steelcase.com", "hermanmiller.com", "hon.com", "staples.com", "uline.com", "wayfair.com", "ikea.com", "walmart.com",
    "amazon.com", "alibaba.com", "aliexpress.com", "global.jumia.com", "jumia.com", "lowes.com", "target.com", "costco.com",
}
NIGERIAN_PLACES = sorted({p for profile in CITY_PROFILES.items() for p in [profile[0].title(), *profile[1]["districts"], *profile[1]["nearby"]]}
                         | {"Nigeria", "Port Harcourt", "Ibadan", "Kano", "Enugu", "Benin City", "Abeokuta", "Owerri", "Warri", "Jos", "Ilorin", "Ojo"})
NIGERIAN_PLACE_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in NIGERIAN_PLACES) + r")\b", re.IGNORECASE)
NIGERIAN_PHONE_RE = re.compile(r"\+234|\b234[ -]?[789][01]\d|(?<!\d)0[789][01]\d[ -]?\d{3}[ -]?\d{4}")
NAIRA_RE = re.compile(r"₦|\bNGN\b|\bnaira\b", re.IGNORECASE)


def is_known_overseas(domain: str) -> bool:
    """A known overseas company, unless it has a .ng domain."""
    domain = normalize_domain(domain)
    if domain.endswith(".ng"):
        return False
    return any(domain == d or domain.endswith("." + d) for d in OVERSEAS_DOMAINS)


def search_text_is_nigerian(domain: str, text: str) -> bool:
    """A weak, search-stage guess from a result's title and snippet (the pages are read later)."""
    domain = normalize_domain(domain)
    if is_known_overseas(domain):
        return False
    return domain.endswith(".ng") or bool(NIGERIAN_PLACE_RE.search(text) or NIGERIAN_PHONE_RE.search(text) or NAIRA_RE.search(text))


def nigeria_evidence(domain: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Why a supplier is Nigerian, from its own pages: {"relevant": bool, "reasons": [...]}."""
    domain = normalize_domain(domain)
    if is_known_overseas(domain):
        return {"relevant": False, "reasons": [], "why_not": "a known overseas retailer or manufacturer"}
    reasons = []
    if domain.endswith(".ng"):
        reasons.append(".ng domain")
    if any(NIGERIAN_PLACE_RE.search(i["value"]) for i in fields["address"].get("items", [])):
        reasons.append("Nigerian address on its pages")
    if any(NIGERIAN_PHONE_RE.search(i["value"]) or i["value"].startswith("+234") for i in fields["phone"].get("items", [])):
        reasons.append("Nigerian phone number on its pages")
    if any(i.get("currency") == "NGN" or NAIRA_RE.search(i.get("value", "")) for i in fields["prices"].get("items", [])):
        reasons.append("prices in naira on its pages")
    if reasons:
        return {"relevant": True, "reasons": reasons}
    return {"relevant": False, "reasons": [], "why_not": "no .ng domain, Nigerian address, phone number or naira price on its own pages"}
