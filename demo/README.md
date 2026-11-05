# Demo fixtures

Everything here is fictional. Businesses use reserved `.example` domains, and phone numbers use the `555` range.
These files let `scout_search.py`, `check_suppliers.py` and `cross_check.py` run with no API key and no network
(`DEMO_MODE=1`, or automatically when no key is found).

- `index.json`: URL to saved page
- `pages/`: saved supplier, directory and listing pages
- `brave_results.json`: stand-in search results (discovery queries, name lookups, independent-listing checks)
- `rdap.json`: stand-in domain registration dates
