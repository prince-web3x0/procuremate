"""Cities, districts and addresses: which region a supplier is based in, from an address on its own pages.

"Based in" needs an address with a city or district on the supplier's own pages. A supplier that only
mentions a city (in a search snippet, or to say it delivers there) is not based there.
The profiles cover the cities the demo covers (Abuja, Lagos, Kaduna); other cities work with the generic queries only.
"""
import re
from typing import Any

# districts: commercial areas used for local queries and for recognising an address.
# nearby: towns used to recognise "close to this city". cities: nearby towns searched when the buyer widens the area.
CITY_PROFILES: dict[str, dict[str, list[str]]] = {
    "abuja": {
        "districts": ["Wuse", "Garki", "Jabi", "Utako", "Maitama", "Gwarinpa", "Mabushi"],
        "nearby": ["Nasarawa", "Kubwa", "Suleja", "Karu", "Kuje", "Gwagwalada", "Bwari", "Lugbe", "Apo", "FCT"],
        "cities": ["Kubwa", "Suleja", "Karu", "Gwagwalada"],
    },
    "lagos": {
        "districts": ["Ikeja", "Lekki", "Ikoyi", "Victoria Island", "Surulere", "Yaba", "Apapa"],
        "nearby": ["Ikorodu", "Badagry", "Ajah", "Mushin", "Oshodi", "Maryland", "Festac", "Ogba", "Agege"],
        "cities": ["Ikorodu", "Ajah", "Badagry"],
    },
    "kaduna": {
        "districts": ["Barnawa", "Kawo", "Ungwan Rimi", "Kaduna North", "Kaduna South", "Sabon Tasha"],
        "nearby": ["Zaria", "Kachia", "Rigasa", "Kagoro"],
        "cities": ["Zaria", "Rigasa"],
    },
}
# A line that looks like a street address: a number or plot/block/suite, or a street word.
ADDRESS_LINE = re.compile(
    r"\b\d+[a-z]?\b[^.]{0,80}\b(?:street|st\.|road|rd\.|avenue|ave\.|crescent|close|plaza|way|estate|expressway|drive|lane|boulevard|layout|market|complex|area)\b"
    r"|\b(?:plot|block|suite|shop|no\.?)\s*\w+[^.]{0,60}\b(?:street|road|avenue|crescent|close|plaza|way|estate|expressway|drive|lane|area|district)\b"
    r"|\bP\.?O\.? ?Box\b",
    re.IGNORECASE,
)


def profile_for(location: str) -> tuple[str, dict[str, list[str]] | None]:
    key = location.strip().lower()
    for city, profile in CITY_PROFILES.items():
        if city in key:
            return city, profile
    return key, None


def location_terms(location: str) -> tuple[list[str], list[str]]:
    """(the city and its districts, nearby towns) for a location; just the name when it has no profile."""
    city, profile = profile_for(location)
    if profile:
        return [city] + profile["districts"], profile["nearby"]
    return [city], []


def _word(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term.lower())}\b")


def regions_in(text: str, regions: list[str]) -> list[str]:
    """The regions (in the given order) whose city or district names appear in text."""
    low = text.lower()
    out = []
    for region in regions:
        if any(_word(term).search(low) for term in location_terms(region)[0]):
            out.append(region)
    return out


def based_in(address_items: list[dict[str, Any]], lines_by_page: list[dict[str, Any]], regions: list[str]) -> dict[str, Any] | None:
    """The first address on the supplier's own pages that names one of the regions' city or districts.

    address_items: the site's address field items (JSON-LD, <address>, 'Address:' lines).
    lines_by_page: [{"url", "retrieved_at", "lines"}] for address-looking lines (footer or contact text).
    """
    candidates: list[tuple[str, str, str, str]] = [(i["value"], i["source_url"], i["retrieved_at"], i.get("excerpt", i["value"])) for i in address_items]
    for page in lines_by_page:
        candidates += [(line, page["url"], page["retrieved_at"], line) for line in page["lines"] if len(line) < 200 and ADDRESS_LINE.search(line)]
    for text, url, retrieved_at, excerpt in candidates:
        found = regions_in(text, regions)
        if found:
            low = text.lower()  # the region named first wins: "Ikeja, Lagos. We deliver to Abuja" is a Lagos address
            region = min(found, key=lambda r: min(m.start() for t in location_terms(r)[0] if (m := _word(t).search(low))))
            district = next((t for t in location_terms(region)[0][1:] if _word(t).search(text.lower())), None)
            return {"region": region, "district": district, "address": text.strip()[:200], "source_url": url,
                    "retrieved_at": retrieved_at, "excerpt": excerpt[:200]}
    return None
