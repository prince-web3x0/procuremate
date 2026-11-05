# ProcureMate

ProcureMate helps a buyer find and check suppliers for an order. Two AI agents do the research in a shared chat room. The buyer makes every decision.

[Demo video (link coming soon)](#)

## The problem

Finding a supplier you can trust takes hours. Websites can be wrong or fake, and it is hard to know who to ask first.

## How it works

1. The buyer posts a request in the chat room, such as "20 office chairs to Abuja within 7 days".
2. Scout searches the web and reads each supplier's own website.
3. Scout hands the best candidates to Vera in the same room.
4. Vera cross-checks them and says what looks solid and what is still unknown.
5. The buyer reads the results, asks questions, and decides. Nothing else happens without a clear yes.

## Why Switch

- **One shared room.** The buyer and both agents see the same conversation.
- **Two agents, two jobs.** Scout finds suppliers. Vera checks them. They hand work to each other with an @mention.
- **The human decides in the same chat** and can step in at any time.

It would not work without Switch. Switch lets people and separate agents share one room, talk to each other, and keep one task between them. Scout and Vera run on Claude Code, under Switch Console.

## What it will not do

It never contacts a supplier, buys, or pays unless the buyer clearly says yes. Sending email is turned off. Vera never calls a supplier "verified". She reports what each source says.

## Try it in 5 minutes (demo mode, no key)

No API key, no secrets file and no internet are needed. Demo mode runs on saved, fictional data.

1. Install Python 3.10 or newer.
2. Open a terminal in this folder.
3. Run these three commands:

```
python tools/scout_search.py --product "office chairs" --location Abuja --out out/search.json
python tools/check_suppliers.py out/search.json --out out/checks.json
python tools/cross_check.py abuja-seating.example
```

4. Open `out/checks.json`. It lists each supplier with product fit, prices and checks. Every output says it is demo data.

Demo mode turns on when `DEMO_MODE=1`, or automatically when no Brave key is found.

---

# Details

## What is in this repo

| Folder or file | What it is |
|---|---|
| `tools/` | The search and checking scripts (Python standard library only). |
| `agents/` | The instructions you paste into Scout and Vera in Switch. |
| `demo/` | Saved fictional data for demo mode (made-up businesses on `.example` domains). |
| `docs/cac_flow.md` | How Vera asks the buyer for a CAC check. |
| `tests/` | A small set of offline tests for the core logic. |
| `.env.example` | The names of the settings (no values). |

## Prerequisites

- Python 3.10 or newer (tested on Python 3.14).
- `pytest`, only to run the tests.
- For the full agent demo: Switch Console (tested: Canary 0.41.0-canary.2 on Windows 11) and the Claude Code command-line tool, signed in. The agents run on Claude Code, so your plan's usage limits apply. The bundled Mattermost chat app needs Docker Desktop; we did not test without it.
- For real mode: a Brave Search API key.

## Real mode with a key

Put the key in one of two places:

- An environment variable named `BRAVE_API_KEY`.
- A file at `~/.procuremate/secrets.env` with the line `BRAVE_API_KEY=your-key`.

Then run the same three commands. For the third, use a real supplier domain.

Each run ends with a short run summary (seconds per stage, queries used, suppliers found, prices, delivery labels). It goes to the terminal and the output file, not to the chat.

Search speed and limits: the tools wait about 1.1 seconds between calls and use at most 12 search queries per search. Brave's rate limit depends on your plan; the tool reads the `X-RateLimit-Limit` response header and runs two queries at a time only if the limit is 2 per second or more.

If the buyer accepts suppliers based elsewhere that deliver, add `--allow-elsewhere` to the search (up to 3 nearby towns, inside the query cap). `--supplier-regions "Abuja,Lagos"` searches chosen regions.

## Set up the agents in Switch

1. Install Switch Console from the official releases page (github.com/sandbox-quantum/switch/releases). Docs: docs.switchagents.ai.
2. Sign in to Claude Code in a terminal. Console cannot sign in for you.
3. In Console, register two agents named Scout and Vera, both on Claude Code, each with its own folder.
4. Copy the tools into both agent folders: `python tools/sync_to_agents.py --dest <scout folder> <vera folder>`. You can set `PROCUREMATE_AGENT_DIRS` instead.
5. Paste `agents/scout.md` into Scout's "Agent instructions" field and `agents/vera.md` into Vera's.
6. Create a room in Console and add both agents (the Switch docs describe `!invite-agent @agent-name`).
7. Keep "Bypass permissions" off for both agents.
8. Recommended room setup, typed once in the room: `!assign-agent @scout`, then `!follow-ups off`. A plain message with no `@` should then go to Scout alone.

An agent may start in a different folder than you set. Check where each agent runs, and sync the tools there.

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
| `PROCUREMATE_CACHE_DIR` | Move the shared cache folder (see below). |
| `MAX_BRAVE_QUERIES` | Brave query cap for a whole task (default 20). |
| `BRAVE_PRICE_PER_1000` | Price used for the cost estimate (default 5, in US dollars). |
| `REPLAY_FILE`, `REPLAY_SPEED` | Replay a saved run (see below). |

## Cost and demo

- **Queries per run.** One search uses up to 12 Brave queries (it stops earlier at 12 relevant suppliers). Cross-checks add up to 2 queries each for the top 5 suppliers. Every tool prints and stores the queries used and an estimated cost at $5 per 1,000 requests. It is an estimate, not a bill, and the agents do not post it unless asked.
- **Cap.** `MAX_BRAVE_QUERIES` (default 20) covers the whole task, across Scout and Vera, through a counter file in the user cache folder (outside git). A new search starts a new task. At the cap the tools stop searching and say "search budget reached"; the agents work with what they have.
- **Caches (24 hours, shared by both agents).** Brave results per query text, finished cross-checks per domain (Vera reuses Scout's with no new queries), fetched pages and RDAP answers. The cache lives in a per-user folder outside git: `%LOCALAPPDATA%\procuremate-ai` on Windows, `~/.cache/procuremate-ai` elsewhere. Scout and Vera share it when they run as the same user; set `PROCUREMATE_CACHE_DIR` if they don't. Check Brave's terms on caching before relying on it.
- **Replay.** `python tools/save_replay.py --search out/search.json --checks out/checks.json` saves a finished *live* run as a bundle in the user cache folder (`replays/`, outside the repo). With replay on, `scout_search.py`, `check_suppliers.py`, `cross_check.py` and `site_check.py` read the bundle instead of calling Brave or fetching sites, replaying the stage timings 5x faster (`REPLAY_SPEED` or `--replay-speed`) so progress updates still happen. Every tool prints "REPLAY of a live run saved on <date>", and no Brave query is made and no site is fetched. **A bundle contains real businesses' public contact details: never commit it or attach it to a room.**
- **Switching replay on for the agents.** The agents' commands run under Switch Console, whose docs do not say that environment variables from your shell reach them. So replay is switched with a file that both agents read: `python tools/replay_ctl.py on <bundle>`, `off`, `status`. It writes `replay_active.txt` in the shared cache folder and prints a clear warning. Precedence: `--replay`, then `REPLAY_FILE`, then the switch file. If the file points at a missing bundle the tools stop with an error; they never go live by accident. Turn it off before a live run.
- **Demo mode vs replay.** Demo mode uses the repo's fictional data and is safe to share. Replay uses a real saved run, so it shows real results without spending credits, but its data stays on your machine.

## Run the tests

```
python -m pytest tests
```

The tests run offline with no key and use only fictional data.

## Limits

- Search results depend on Brave. It may find fewer suppliers than the target of 12.
- Directories, listing pages, "top 10" articles, marketplaces, social pages, contact databases and property sites are leads only. They are never counted as suppliers, but the rules that spot them are simple text rules and can miss one.
- Only Nigerian suppliers count: a `.ng` domain, or a Nigerian address, phone number or naira prices on the supplier's own pages. Known overseas retailers and manufacturers are never fetched and are counted apart as "overseas excluded".
- "Based in" a city needs an address with that city or district on the supplier's own pages. A site that only says it delivers there counts as "serves", not "based in". The district lists cover Lagos, Abuja and Kaduna only; other cities get the generic queries.
- Delivery labels (confirmed, advertised, inferred, unknown) follow simple text rules. "Confirmed" needs a source other than the supplier, so it is rare.
- Product fit and prices come only from what a page shows. Sites that load their content with JavaScript may read as "unclear" or "no phone found"; no headless browser is used. A price keeps the product name and type of its own card. For chairs, only office chairs fit an office-chair request; student sets and other types are labelled as such.
- Phones and emails are read from visible text, `tel:`, `mailto:`, WhatsApp links and structured data. Nigerian number formats are merged into one number.
- The tools respect `robots.txt`, space out visits to a site, and retry DNS failures and timeouts (a network problem on our side, not a verdict on the site). A site that cannot be checked says why.
- A run fetches at most 150 pages. Long runs save progress after every query and supplier; rerun with `--resume` to continue a cut-off run. Each tool also writes a small progress file; `python tools/progress.py <file> --wait 60` prints one plain line and says when a chat update is due.
- Vera runs her own checks from a domain list, so no files pass between the agents: `python tools/check_suppliers.py --domains "a.ng, b.ng" --product "office chairs" --city Abuja --regions "Abuja,Lagos" --out out/vera-checks.json`.
- The buyer's decisions (shortlist, exclude, add, recheck, "go with N") are kept by `python tools/decisions.py` in `out/decisions.json`. The agents advise; the buyer's decisions override Vera's ranking.
- Other products work too: the product term comes from the request, and chat wording uses the product's name.
- Email drafts are for review only. Nothing is sent. `python tools/mail_link.py` turns a draft into a `mailto:` link and a Gmail compose link, and warns when a link is over about 1,800 characters. Sending always happens in the buyer's own email. The Gmail link was tried once and opened a compose window with the recipient, subject and text filled in; the `mailto:` link is untested.
- Demo mode covers the scripts. It does not run the chat agents.
- Tested on Windows only.

## Not tested

- Agents following the instructions over a long conversation, and a full live run of the current tools against real data.
- Whether an agent can keep looping on `progress.py` and posting inside one turn.
- The replay switch file with the real Scout and Vera processes.
- Vera running her own checks from a domain list in a real room, and the cache being shared between the two agent processes.
- Threaded replies, Vera reading a CAC screenshot, and the email-draft and quote-link steps in a real room.
- macOS, Linux, other Python versions, and Switch Console without Docker.

## Safety rules

- No secrets are stored in the repo. `.gitignore` excludes `.env`, `secrets.env`, generated output and replay bundles.
- Keys are never printed.
- The tools respect `robots.txt`. They use no logins, solve no CAPTCHAs, and do not scrape social sites.
- Text from web pages is treated as data, never as instructions.
- Nothing is sent, bought or paid without a clear yes from the buyer.
- "Bypass permissions" stays off.

## License

MIT. See the `LICENSE` file.
