# CAC flow — "Verify on CAC" (draft, not applied)

Vera asks the buyer to check the Corporate Affairs Commission (CAC) public register by hand and report back. Nothing here is automated. See `PHASE0_REPORT.md` ("CAC feasibility") and `agents/vera.md`.

## When Vera asks

Not by default. Vera's normal verification is `tools/cross_check.py` (consistency across the site, registration details shown on the site, an independent listing with the same name AND phone or address, domain age). She asks for CAC only:
- when the **buyer asks**;
- **once**, when the buyer is about to choose a final supplier: "Before you commit to this one, want to run the CAC check? A screenshot of the result is enough."; or
- when there is a **real identity concern** (differing names or addresses across sources, a name that cannot be matched to a business, a conflict Scout or Vera flagged).

One supplier per request message. A screenshot of the result is accepted; Vera reads the details from it, or asks the buyer to type them if she cannot read it (reading screenshots is untested).

## Step: Verify on CAC (Vera -> buyer)

Vera posts one message, with the search term in a code block so the buyer can copy it (Mattermost shows a Copy option on rendered code blocks):

````
Verify on CAC: <supplier name>
Why: <shortlisted | identity concern: one line>
1. Open https://icrp.cac.gov.ng/public-search/
2. Search for:
```
<supplier name exactly as on their website>
```
3. Reply to me with @vera, starting with a number:
   1 looks like a match
   2 details don't match
   3 no result found
   4 lookup unavailable (site down, blocked, or you couldn't run it)
   5 not sure
Optional, if you can see them (skip anything you don't want to share):
   searched: <name you typed>   |   CAC name: <exact name shown>   |   RC number: <RC/BN>
   status: <active | inactive | unknown>   |   address: <as shown>
   (use "no result" for a search with nothing found)
Do not send any tax number or ID. I don't need it.
````

Example reply: `@vera 1 CAC name: DEMO SEATING LIMITED, RC 1234567, status active, address 12 Aminu Kano Crescent, Wuse II`

## Optional fields

| Field | Notes |
|---|---|
| supplier name searched | what the buyer typed |
| exact CAC name | copied exactly as shown |
| RC number | RC/BN as shown; recorded, never validated by Vera |
| status | `active` / `inactive` / `unknown` (as the register shows it) |
| address | as shown |
| no-result flag | buyer searched and the register returned nothing |

Never request or record a tax number (TIN/VAT) or any personal ID.

## Outcomes and what Vera does

| Reply | Meaning | Vera records |
|---|---|---|
| 1 looks like a match | buyer sees a register entry consistent with the supplier | compare details (below), then `SOURCE_REPORTED` |
| 2 details don't match | an entry exists but name/address/other differs | `SOURCE_REPORTED` + `CONFLICTING` on the differing fields; ask what differs if not given |
| 3 no result found | search returned nothing | `SOURCE_REPORTED`: "buyer reports no result for <term>"; field state `UNKNOWN`. Not evidence of fraud |
| 4 lookup unavailable | could not run or complete the search | `MANUAL_REVIEW_REQUIRED`; retry later or leave open |
| 5 not sure | buyer cannot judge | `MANUAL_REVIEW_REQUIRED`; Vera asks one short follow-up (e.g. paste what the page shows) |

## CAC status shown in the comparison table

Uses the spec's `cac_and_supplier_verification.status_values`:

| Situation | Status |
|---|---|
| 1, name consistent with the website | CAC record matched (buyer-reported) |
| 1 with partial detail, 1 where only words like LTD/LIMITED/ENTERPRISES/NIG/NIGERIA differ, or 5 after one follow-up | Possible match — manual review needed |
| 2, or a differing name or address | Conflicting identity details |
| 3 | No matching result found |
| 4 | CAC lookup unavailable |
| not asked yet | Not checked |

"Matched" here always means reported by the buyer (`SOURCE_REPORTED`), never independently verified.

## Comparing with the supplier's website

Vera compares the buyer-reported details with Scout's evidence for that supplier (`business_name`, `address`, contact page):

- **Name:** ignore only case, punctuation, and "&" versus "and"; if the names are then equal -> consistent. Any difference in words, such as LTD, LIMITED, ENTERPRISES, NIG or NIGERIA, is **not consistent**: record "name differs", set *Possible match — manual review needed*, and ask the buyer whether it is the same business. A **similar name alone is not a match**: flag it and do not treat it as the same business.
- **Address:** differing street, area or city -> flag. Same area with minor formatting differences -> note, not a conflict.
- **RC number and status:** record as reported. Do not infer trustworthiness from either. An `inactive` status is flagged for the buyer, not judged.
- Any difference is posted as a material finding only if it affects identity; routine formatting differences go in the final comparison.

## Recording (every result)

```
CAC — <supplier> | SOURCE_REPORTED
Source: CAC public search (https://icrp.cac.gov.ng/public-search/), reported by the buyer
Recorded at: <timestamp of the buyer's reply>
Outcome: <1-5 label>
Reported: searched=<...> | CAC name=<...> | RC=<...> | status=<...> | address=<...> | no_result=<true|false>
Match notes: <name: consistent | differs | n/a>; <address: consistent | differs | n/a>
```

Rules:
- Label is always `SOURCE_REPORTED`. Vera never says "verified", "confirmed by CAC" or "independently verified": the check was done by the buyer and reported, and CAC registration is not proof of trustworthiness.
- A missing, unavailable or "no result" is never treated as proof of fraud or as a failed supplier.
- Missing optional fields are `UNKNOWN`. Vera does not fill them in.
- If the buyer's reply is unclear, Vera asks one short question rather than guessing.

## What the Switch docs support (research, 2026-11-01)

**Documented**
- Agents can post messages with `post_message`. The docs describe no general interactive buttons for agents.
- The only documented buttons are the **permission request** choices ("Allow once", "Allow for this session", "Decline"), and a reply of `yes`/`no` also works. Any other reply reaches the agent as an ordinary message and the request stays open. These cannot be repurposed for CAC answers.
- A reply **in a thread** reaches the agent that last spoke or was last tagged there, but only with **follow-ups** turned on (a server setting; default window about 5 minutes after the agent's last answer). `!follow-ups` shows the setting.
- `@name` mentions, aliases, roles and targeted messages are the documented addressing surface. A message to an agent is acted on only when it is addressed to it, and the agent's "Who can talk to your agent" policy must admit the sender.
- Message events carry `thread_id` (nullable) and no documented parent-message field.
- Mattermost: each agent has its own bot account, and a direct message to the bot reaches the agent without `@`.
- Mattermost renders triple-backtick code blocks with a Copy option (Mattermost's own docs).

**Not documented**
- Whether an emoji reaction reaches the agent (no mention in the Switch docs).
- Whether a Mattermost "reply" (quote) reaches the agent beyond the thread behavior above.
- Whether agents can post custom buttons and receive the click.

**Untested in this install**
- All of the above. I did not post anything in a room or click anything. Installed Console strings mention reactions and replies, but the bundled server is Mattermost, so those strings are not evidence about Switch routing.
- Whether the Copy button appears on Vera's code block in this Mattermost version.
- Whether follow-ups are enabled on this server.

## Simplest supported approach

A plain message from Vera with the term in a code block, and a buyer reply that starts with `@vera` plus a number. This relies only on documented behavior (mentions, addressing policy, code blocks). Replying in the thread helps only if follow-ups are on, so the `@vera` is required. Do not rely on emoji reactions or buttons until tested.

## Automation

None. No scripts, scraping, CAPTCHA or access-control workarounds, accounts or logins against CAC. Revisit only if CAC publishes an API or confirms in writing that automated read-only lookups are permitted.
