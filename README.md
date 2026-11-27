# AI GL Anomaly Hunter

An AI agent that reviews a month of **general ledger** entries before close, investigates anything suspicious, and writes a review memo for the controller.

Built by **Samuel Gonzalez**, Farm Analyst. Part of a series of finance agents, alongside my Flash Report Agent and Rolling Forecast Agent (see my GitHub profile).

---

## The problem

Before every close, someone has to scan the ledger for problems: duplicate payments, miscoded entries, unsupported manual entries, payments split to dodge approval limits. Simple rules catch these, but they also flag lots of normal activity. Someone still has to dig into each flag and decide whether it's real.

## How it works

**Rules catch everything. The agent separates real problems from noise.**

```
gl_detail.csv
     │
     ▼
1. RULES (Python): flag every entry that trips a check
     │
     ▼
2. AGENT LOOP (Claude + tools), for each flagged entry:
     get_entry ─► get_vendor_history ─► find_related_entries ─► record_finding
                                                    (verdict, risk, reason, question for owner)
     │
     ▼
3. MEMO: Claude writes the controller memo
     │
     ▼
4. GUARDRAIL: every entry ID and $ figure in the memo must exist in the data
```

**Rule checks:** duplicate vendor + invoice + amount · vendor posted to an unusual account · weekend posting · manual entry with no invoice · round-dollar amounts · new vendor · amount 2.5x or more above the vendor's median · multiple same-day entries just under the $5,000 approval limit

**Agentic features:** tool use for investigation · autonomous multi-step review of every flag · judgment calls with written reasoning · error recovery on bad tool calls · a guardrail that checks entry IDs and dollar figures

## Sample results (October 2026, fictional data)

**56** entries reviewed · **9** flagged by rules · **7** need review ($117,853.43) · **2** cleared as false alarms

Six problems were planted in the test ledger. **The agent caught all six and correctly cleared both false alarms**, including telling the original invoice apart from its duplicate repost.

| Verdict | Risk | Entry | Vendor | Amount | Agent's finding |
|---|---|---|---|---:|---|
| Needs review | High | JE10297 | Cash / Manual JE | $25,000.00 | Round-dollar manual entry posted on a Saturday with no invoice and a vague "Misc adjustment" description. |
| Needs review | High | JE10298 | Rapid Ag Services LLC | $18,750.00 | New vendor, first invoice (RA-0001) for a round amount and vague services. Fits a possible fictitious vendor. |
| Needs review | High | JE10295 | Pacific Box & Pack | $15,848.43 | Same vendor, invoice and amount as JE10263, posted by a different user. Duplicate posting. |
| Needs review | Medium | JE10301 | Central Ag Chemicals | $41,200.00 | 4.3x the vendor's median and about 3x its historical max. Plausible seasonally, but needs support. |
| Needs review | Medium | JE10296 | Valley Diesel Supply | $7,180.00 | A diesel delivery coded to Office supplies, while all 40 prior entries went to Fuel & diesel. Reclass. |
| Needs review | Medium | JE10299 / JE10300 | SJ Valley Equipment Repair | $4,950.00 / $4,925.00 | Same day, same poster, same "Tractor repair," each just under the $5,000 limit. Looks like a deliberate split. |
| Likely OK | Low | JE10282 | Westside Farm Insurance | $22,000.00 | Fixed monthly premium. The last 5 months were exactly $22,000, so the round amount is normal. |
| Likely OK | Low | JE10263 | Pacific Box & Pack | $15,848.43 | The original posting. The later repost, JE10295, is the one to reverse. |

**Excerpt from the controller memo the agent wrote**

> **56 entries reviewed; 7 need review, with $117,853.43 at stake.**
>
> **JE10295, $15,848.43, Pacific Box & Pack.** It has the same vendor, invoice (INV-570345) and amount as JE10263, and was posted 10/23 by a different user. It looks like a duplicate. *Next step:* reverse JE10295 and confirm the invoice wasn't paid twice.
>
> **JE10299 ($4,950) and JE10300 ($4,925), SJ Valley Equipment Repair.** Both are "Tractor repair" entries posted 10/09 by the same user, each just under the $5,000 approval limit. *Next step:* get the work orders and approvals for the combined amount.

See [`gl_review_2026-10.md`](gl_review_2026-10.md) for the full memo and findings.

## Run it yourself

```bash
pip3 install anthropic pandas
export ANTHROPIC_API_KEY="your-key"

python3 gl_anomaly_agent.py                  # latest month in the ledger
python3 gl_anomaly_agent.py --month 2026-10
python3 gl_anomaly_agent.py --no-ai          # rules only, no API key needed
```

Output: `gl_review_<month>.md`

## Files

| File | What it is |
|---|---|
| `gl_anomaly_agent.py` | The agent: rules, investigation loop, memo, guardrail |
| `gl_detail.csv` | 6 months of GL entries (May–Oct 2026) for **Sunridge Farms**, a made-up company, with problems planted in October |
| `make_gl_data.py` | Generates the ledger and the planted problems |
| `gl_review_2026-10.md` | Example output |

> All data is fictional. No real company figures are used.

## Using your own data

Any CSV with: `je_id`, `date` (YYYY-MM-DD), `account`, `vendor`, `invoice`, `description`, `amount`, `posted_by`. Thresholds are set at the top of the script: `APPROVAL_LIMIT`, `ROUND_DOLLAR_MIN`, `OUTLIER_X`.

## What I'd add next

- Look back further for duplicates paid across months
- Benford's Law test on amounts
- Export findings to Excel with a sign-off column for reviewers

## Tech

Python · pandas · Claude API (tool use) · Markdown output
