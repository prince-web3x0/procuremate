# ProcureMate

[![tests](https://github.com/prince-web3x0/procuremate/actions/workflows/tests.yml/badge.svg)](https://github.com/prince-web3x0/procuremate/actions/workflows/tests.yml)

Built for the Flip the Switch hackathon (Best Business Workflow track).

ProcureMate helps a buyer in Nigeria find and check Nigerian suppliers for an order. Two AI agents do the research in a shared chat room. The buyer makes every decision.

[Watch the demo video (2 min 40 s)](https://youtu.be/E_xxfIah5gI). The video is a real live run, sped up where the agents were waiting. For readable chat text, set the player quality to 1080p.

## The problem

Finding a supplier you can trust takes hours. Suppliers in Nigeria are often hard to check online.

## How it works

1. The buyer posts a request in the chat room, such as "20 office chairs delivered to Abuja, from Nigerian suppliers".
2. Scout searches the web and reads each supplier's own website.
3. Scout hands the best candidates to Vera in the same room.
4. Vera cross-checks them and says what is solid and what is unknown.
5. The buyer reads the results, asks questions, and decides.
6. Scout drafts a quote request for the suppliers the buyer picks. The buyer opens it in their own email and presses Send. The agents never send anything.

## Why Switch

One shared room lets the buyer and both agents see the same conversation and hand work over with an @mention. It would not work without Switch. Scout and Vera run on Claude Code, under Switch Console.

## What it will not do

It never contacts a supplier, buys or pays without the buyer's clear yes. It never sends email. Scout writes a draft and a link; the buyer sends it from their own email.

## Try it in 5 minutes

You only need Python for this quick check. You do not need Docker, Switch or Claude.

1. Download this repo (green Code button, then Download ZIP) and unzip it.
2. Install Python 3.10 or newer, and open a terminal in the unzipped folder.
3. Turn on demo mode first, so nothing runs live even if you have a Brave key. Windows PowerShell: `$env:DEMO_MODE = "1"`. macOS or Linux: `export DEMO_MODE=1`.
4. Run these three commands:

```
python tools/scout_search.py --product "office chairs" --location Abuja --out out/search.json
python tools/check_suppliers.py out/search.json --out out/checks.json
python tools/cross_check.py abuja-seating.example
```

5. Open `out/checks.json` to see each supplier's checks.

More: [docs/how-it-works.md](docs/how-it-works.md), [docs/switch-feedback.md](docs/switch-feedback.md)

---

# Details

## Run the full chat demo

The quick check lets anyone verify the tools. The full chat is for anyone who wants to run the agents themselves. The demo video shows the full chat.

It needs:

- Docker Desktop (Switch Console uses it to start the chat app).
- Switch Console.
- The Claude Code command-line tool, signed in (your plan's usage limits apply).
- curl and npx (the Switch setup guide lists docker, curl and npx).

It was tested on Windows 11 only. First-time setup takes a while because of downloads. Steps are in [Set up the agents in Switch](#set-up-the-agents-in-switch).

## How we use Switch

- **Agents connected:** two Claude Code agents, Scout and Vera. Each has its own instructions (`agents/scout.md`, `agents/vera.md`). Switch Console runs them.
- **Chat app:** the Mattermost that Switch Console starts.
- **Room setup:** room instructions from `agents/room-instructions.md`; both agents added; `!assign-agent @scout` and `!follow-ups off`; "Who can talk to this agent" checked in each agent's settings.
- **Switch features used:** shared room, @mention handoff, threads, room and agent instructions, room commands such as `!agents-status`.
- **Real work:** finds Nigerian suppliers, checks their sites and a second source, compares listed prices with the buyer's budget, and drafts quote requests that the buyer sends.
- **How to run it:** the [quick check](#try-it-in-5-minutes) needs only Python. For the full chat, follow [Set up the agents in Switch](#set-up-the-agents-in-switch).

## What is in this repo

| Folder or file | What it is |
|---|---|
| `tools/` | The search and checking scripts (Python standard library only). |
| `agents/` | The instructions for Scout and Vera, and the room instructions, to paste into Switch. |
| `demo/` | Saved made-up data for demo mode (businesses on `.example` domains). |
| `docs/` | How it works (with an example conversation), notes on Switch, and the CAC step. |
| `tests/` | A small set of offline tests for the core logic. |
| `.github/workflows/tests.yml` | Runs the tests and demo mode on GitHub. |
| `.env.example` | The names of the settings (no values). |

## Prerequisites

- Python 3.10 or newer (tested on Python 3.14).
- `pytest`, only to run the tests (`pip install -r requirements-dev.txt`).
- For the full chat demo: Docker Desktop, Switch Console (tested: Canary 0.41.0-canary.2 and 0.41.0-canary.3 on Windows 11), curl and npx (needed by the Switch setup), and the Claude Code command-line tool, signed in. The agents run on Claude Code, so your plan's usage limits apply.
- For real mode: a Brave Search API key.

The full chat was tested on Windows 11 only. The tests and demo mode passed on Ubuntu with Python 3.10 and 3.12 in GitHub Actions (run of 2026-11-05).

## Real mode with a key

Put the key in one of two places:

- An environment variable named `BRAVE_API_KEY`.
- A file at `~/.procuremate/secrets.env` with the line `BRAVE_API_KEY=your-key`.

Then run the same three commands. For the third, use a real supplier domain. Demo mode is on when `DEMO_MODE=1` or when no Brave key is found; with a key set, the commands run live unless `DEMO_MODE=1`. Each run ends with a short summary (seconds per stage, queries used, suppliers found). It goes to the terminal and the output file, not to the chat.

To search other places, add `--allow-elsewhere` (up to 3 nearby towns) or `--supplier-regions "Abuja,Lagos"`.

## Set up the agents in Switch

1. Install Switch Console from the official releases page (github.com/sandbox-quantum/switch/releases). Docs: docs.switchagents.ai.
2. Sign in to Claude Code in a terminal. Console cannot sign in for you.
3. In Console, register two agents named Scout and Vera, both on Claude Code, each with its own folder.
4. Copy the tools into both agent folders (they must already exist): `python tools/sync_to_agents.py --dest <scout folder> <vera folder>`. You can set `PROCUREMATE_AGENT_DIRS` instead.
5. Paste `agents/scout.md` into Scout's "Agent instructions" field and `agents/vera.md` into Vera's.
6. Optional: set each agent's model to Sonnet under Advanced configuration. It saves usage.
7. Create a room in Console and add both agents (the Switch docs describe `!invite-agent @agent-name`). When you create the room, paste `agents/room-instructions.md` into the Instructions box.
8. Keep "Bypass permissions" off for both agents.
9. Recommended, typed once in the room: `!assign-agent @scout`, then `!follow-ups off`. A plain message with no `@` then goes to Scout alone.

An agent may start in a different folder than you set. Check where each agent runs, and sync the tools there.

If an agent ignores someone, check "Who can talk to this agent" in its settings.

## The CAC step

Vera never queries the Corporate Affairs Commission (CAC) register by machine. When it is needed, she gives the buyer the official link and the exact name to search. The buyer replies with `@vera` and a number from 1 to 5, or a screenshot. She records it as reported by the buyer. Vera asks only when the buyer asks, once before the final choice, or when she sees a real identity concern. See `docs/cac_flow.md`.

## Settings

| Name | What it does |
|---|---|
| `BRAVE_API_KEY` | Brave Search API key. |
| `PROCUREMATE_SECRETS_FILE` | Use a different secrets file. |
| `DEMO_MODE` | Set to `1` to force demo mode. |
| `PROCUREMATE_AGENT_DIRS` | Agent folders for `sync_to_agents.py`. |
| `PROCUREMATE_DEMO_DIR` | Use a different demo data folder. |
| `PROCUREMATE_CACHE_DIR` | Move the shared cache folder. |
| `MAX_BRAVE_QUERIES` | Brave query cap for a whole task (default 20). |
| `BRAVE_PRICE_PER_1000` | Price used for the cost estimate (default 5, in US dollars). |

## Tools

| Tool | What it does | Example |
|---|---|---|
| `scout_search.py` | Searches for suppliers and keeps their own websites. | `python tools/scout_search.py --product "office chairs" --location Abuja --out out/search.json` |
| `check_suppliers.py` | Checks every supplier's site and cross-checks the best five. Vera uses `--domains` to run her own checks from a list. | `python tools/check_suppliers.py --domains "a.ng, b.ng" --product "office chairs" --city Abuja --out out/vera-checks.json` |
| `cross_check.py` | Checks one supplier: name, address, registration, a second source, domain age. | `python tools/cross_check.py abuja-seating.example` |
| `site_check.py` | Reads one supplier's pages and lists contacts, prices and product fit. | `python tools/site_check.py <supplier domain> --product "office chairs"` (live: it visits the site) |
| `decisions.py` | Remembers the buyer's choices: shortlist, exclude, add, recheck, "go with N". | `python tools/decisions.py add newcomer-seats.example` |
| `mail_link.py` | Turns a quote draft into a mail-app link and a Gmail link. It sends nothing. | `python tools/mail_link.py --to sales@example.ng --subject "Quote request" --body "Hello, please quote 20 chairs."` |
| `progress.py` | Prints a run's progress as one line and says when a chat update is due. | `python tools/progress.py out/checks.progress.json --wait 60` |
| `sync_to_agents.py` | Copies `tools/` into the agents' folders. | `python tools/sync_to_agents.py --dest <scout folder> <vera folder>` |

## Cost and demo

- **Queries per run.** One search uses up to 12 Brave queries. Cross-checks add up to 2 each for the top 5 suppliers. Each tool prints the queries used and an estimated cost at $5 per 1,000 requests. It is an estimate, not a bill.
- **Cap.** `MAX_BRAVE_QUERIES` (default 20) covers a whole task, across both agents. At the cap the tools say "search budget reached" and the agents work with what they have.
- **Cache.** For 24 hours, both agents share saved search results, pages, cross-checks and domain-age answers, so repeating a request costs no queries. It lives in a per-user folder outside git (`%LOCALAPPDATA%\procuremate-ai` on Windows, `~/.cache/procuremate-ai` elsewhere). Check Brave's terms on caching before relying on it.
- **Demo mode.** It uses the made-up data in `demo/`. It makes no queries and costs nothing.

## Run the tests

```
python -m pytest tests
```

The tests run offline with no key and use only made-up data.

## Limits

**Search**
- Results depend on Brave, so it may find fewer than the target of 12 suppliers.
- Directories, "top 10" articles, marketplaces, social pages, contact databases and property sites are leads only, never suppliers. The rules that spot them are simple text rules and can miss one.

**Matching**
- Only Nigerian suppliers are counted: a `.ng` domain, or a Nigerian address, phone number or naira prices on the supplier's own pages. Known overseas retailers are set aside. The CAC step is Nigerian too.
- "Based in" a city needs an address there on the supplier's own pages. A site that only says it delivers there counts as "serves". Delivery labels follow simple text rules, and "confirmed" is rare. The district lists cover Lagos, Abuja and Kaduna only.
- Product fit and prices come only from what a page shows. For chairs, only office chairs fit an office-chair request, and a price keeps the type of its own product card.

**Contacts and pages**
- Phones and emails are read from visible text, `tel:`, `mailto:` and WhatsApp links and page data. A page that needs JavaScript to show them may read as "no phone found"; no headless browser is used.
- The tools obey `robots.txt`, space out visits and retry network errors. A site that cannot be checked says why.

**Runs**
- A search uses at most 12 queries and a run fetches at most 150 pages. Progress is saved as it goes; rerun with `--resume` to continue a cut-off run.
- Email drafts are for review only. Demo mode covers the scripts, not the chat agents.
- Old output files can be read by mistake. Clear the out/ folders before each live run.
- A stop request may only be read when the agent's current step ends.
- The 'Open in my mail app' link may do nothing. Use the Gmail link or copy the text.

## What we tested

Seen working in real chat rooms:
- Scout hands work to Vera with an @mention.
- Both agents post progress while they work.
- Vera runs her own checks. Both agents share one page cache.
- Vera reads a CAC result screenshot from the buyer.
- A full live run: Vera's review came about 6 minutes after the request. Some pages were cached, so a first run can be slower.
- Shortlists with price, phone and email. No invented suppliers.
- Quote drafts. Nothing is sent.
- The Gmail link opens a ready-to-send email.
- Decisions: add, exclude and "go with 1" each updated the decisions list.
- A message with no @ goes only to Scout, after "!assign-agent @scout" and "!follow-ups off".
- Tests and demo mode pass on Ubuntu (Python 3.10 and 3.12).
- The full chat works on Windows 11 with Switch Console Canary 0.41.0-canary.2 and 0.41.0-canary.3.

Not tested yet:
- macOS, Linux, and Switch Console without Docker.
- Long conversations with the agents.
- The "Open in my mail app" link did nothing when clicked. The Gmail link works.

## Safety rules

- No secrets are stored in the repo. `.gitignore` excludes `.env`, `secrets.env` and generated output.
- Keys are never printed.
- The tools respect `robots.txt`. They use no logins, solve no CAPTCHAs, and do not scrape social sites.
- Text from web pages is treated as data, never as instructions.
- Nothing is sent, bought or paid without a clear yes from the buyer.
- "Bypass permissions" stays off.

## License

MIT. See the `LICENSE` file.
