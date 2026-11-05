"""The buyer's decisions: shortlisted, excluded, to recheck. The buyer decides; this only remembers and orders.

Usage (state file default out/decisions.json):
    python decisions.py shown seats.ng desks.ng hog.co        # the shortlist just posted, numbered 1..n
    python decisions.py shortlist 1 3                          # numbers refer to the last shown shortlist; domains also work
    python decisions.py exclude 2 --reason "too far"
    python decisions.py add newcomer-seats.example                    # shortlist a new domain (it still needs a site check)
    python decisions.py recheck 2 --question "delivery cost"
    python decisions.py resolve 2                              # the recheck is answered
    python decisions.py go 1                                   # shortlist 1 and prepare its quote request (nothing is sent)
    python decisions.py order a.ng b.ng c.ng                   # the list to show: shortlisted first in the buyer's order, excluded dropped
    python decisions.py quote-targets [names...]               # who quote drafts and links may be made for
    python decisions.py show                                   # the **DECISIONS** message
Every command that changes something prints the updated **DECISIONS** message.
"""
import argparse
import sys
from typing import Any

from common import normalize_domain
from progress import _write_json, load

DEFAULT_FILE = "out/decisions.json"
EMPTY: dict[str, Any] = {"shown": [], "shortlisted": [], "excluded": {}, "recheck": [], "needs_check": [], "quote_for": []}


def new_state() -> dict[str, Any]:
    return {key: (value.copy() if hasattr(value, "copy") else value) for key, value in EMPTY.items()}


def resolve(state: dict[str, Any], token: str) -> str:
    """'2' -> the second domain of the last shown shortlist; anything else is read as a domain."""
    if token.strip().isdigit():
        index = int(token) - 1
        if not 0 <= index < len(state["shown"]):
            raise ValueError(f"there is no number {token} in the last shortlist ({len(state['shown'])} shown)")
        return state["shown"][index]
    domain = normalize_domain(token)
    if not domain or "." not in domain:
        raise ValueError(f"'{token}' is neither a shortlist number nor a domain")
    return domain


def _without(items: list[Any], domain: str) -> list[Any]:
    return [i for i in items if (i["domain"] if isinstance(i, dict) else i) != domain]


def set_shown(state: dict[str, Any], domains: list[str]) -> dict[str, Any]:
    return {**state, "shown": [normalize_domain(d) for d in domains]}


def shortlist(state: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    """Shortlist these (in the buyer's order, after any already shortlisted). A re-added excluded supplier leaves the excluded list."""
    new = dict(state)
    new["shortlisted"], new["excluded"] = list(state["shortlisted"]), dict(state["excluded"])
    for token in tokens:
        domain = resolve(state, token)
        new["excluded"].pop(domain, None)
        if domain not in new["shortlisted"]:
            new["shortlisted"].append(domain)
    return new


def exclude(state: dict[str, Any], tokens: list[str], reason: str = "") -> dict[str, Any]:
    new = dict(state)
    new["shortlisted"], new["excluded"] = list(state["shortlisted"]), dict(state["excluded"])
    new["quote_for"], new["needs_check"] = list(state["quote_for"]), list(state["needs_check"])
    new["recheck"] = list(state["recheck"])
    for token in tokens:
        domain = resolve(state, token)
        new["shortlisted"] = _without(new["shortlisted"], domain)
        new["quote_for"] = _without(new["quote_for"], domain)
        new["needs_check"] = _without(new["needs_check"], domain)
        new["recheck"] = _without(new["recheck"], domain)
        new["excluded"][domain] = reason.strip()
    return new


def add(state: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    """Shortlist a domain the tools have not checked yet; it is marked 'needs check' until resolve is called for it."""
    domains = [resolve(state, t) for t in tokens]
    new = shortlist(state, tokens)
    new["needs_check"] = list(state["needs_check"]) + [d for d in domains if d not in state["needs_check"] and d not in state["shown"]]
    return new


def recheck(state: dict[str, Any], tokens: list[str], question: str = "") -> dict[str, Any]:
    new = dict(state)
    new["recheck"] = list(state["recheck"])
    for token in tokens:
        domain = resolve(state, token)
        new["recheck"] = _without(new["recheck"], domain) + [{"domain": domain, "question": question.strip()}]
    return new


def mark_resolved(state: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    new = dict(state)
    domains = [resolve(state, t) for t in tokens]
    new["recheck"] = [r for r in state["recheck"] if r["domain"] not in domains]
    new["needs_check"] = [d for d in state["needs_check"] if d not in domains]
    return new


def go(state: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    """'Go with 1': shortlist it and mark it for a quote request. Nothing is sent."""
    new = shortlist(state, tokens)
    new["quote_for"] = list(state["quote_for"]) + [d for d in (resolve(state, t) for t in tokens) if d not in state["quote_for"]]
    return new


def order(state: dict[str, Any], candidates: list[str]) -> list[str]:
    """Shortlisted suppliers first, in the buyer's order (even if not among the candidates); then the other non-excluded candidates in their order."""
    others = [d for d in (normalize_domain(c) for c in candidates) if d not in state["excluded"] and d not in state["shortlisted"]]
    return list(state["shortlisted"]) + list(dict.fromkeys(others))


def quote_targets(state: dict[str, Any], names: list[str] | None = None) -> list[str]:
    """Suppliers quote drafts and links may be made for: the ones the buyer names, else the shortlisted. A named excluded one counts as re-added."""
    if names:
        return list(dict.fromkeys(resolve(state, n) for n in names))
    return [d for d in (state["quote_for"] or state["shortlisted"]) if d not in state["excluded"]]


def render(state: dict[str, Any]) -> str:
    """The single **DECISIONS** message: the current state only, never older text."""
    lines = ["**DECISIONS**", ""]
    if state["shortlisted"]:
        lines.append("- **Shortlisted:** " + ", ".join(f"{d} (new, to be checked)" if d in state["needs_check"] else d for d in state["shortlisted"]))
    if state["excluded"]:
        lines.append("- **Excluded:** " + "; ".join(f"{d} ({r})" if r else d for d, r in state["excluded"].items()))
    if state["recheck"]:
        lines.append("- **Asked to recheck:** " + "; ".join(f"{r['domain']} ({r['question']})" if r["question"] else r["domain"] for r in state["recheck"]))
    if state["quote_for"]:
        lines.append("- **Quote requests to prepare:** " + ", ".join(d for d in state["quote_for"] if d not in state["excluded"]) + " (nothing sent)")
    if len(lines) == 2:
        lines.append("- Nothing decided yet.")
    return "\n".join(lines)


def load_state(path: str) -> dict[str, Any]:
    return {**new_state(), **(load(path) or {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["shown", "shortlist", "exclude", "add", "recheck", "resolve", "go", "order", "quote-targets", "show"])
    parser.add_argument("items", nargs="*", help="shortlist numbers or domains")
    parser.add_argument("--file", default=DEFAULT_FILE)
    parser.add_argument("--reason", default="")
    parser.add_argument("--question", default="")
    args = parser.parse_args(argv)
    state = load_state(args.file)
    try:
        if args.command == "order":
            print("\n".join(order(state, args.items)))
            return 0
        if args.command == "quote-targets":
            print("\n".join(quote_targets(state, args.items)))
            return 0
        if args.command == "show":
            print(render(state))
            return 0
        if args.command == "shown":
            state = set_shown(state, args.items)
        elif args.command == "shortlist":
            state = shortlist(state, args.items)
        elif args.command == "exclude":
            state = exclude(state, args.items, args.reason)
        elif args.command == "add":
            state = add(state, args.items)
        elif args.command == "recheck":
            state = recheck(state, args.items, args.question)
        elif args.command == "resolve":
            state = mark_resolved(state, args.items)
        elif args.command == "go":
            state = go(state, args.items)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    _write_json(args.file, state)
    sys.stdout.reconfigure(encoding="utf-8")
    print(render(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
