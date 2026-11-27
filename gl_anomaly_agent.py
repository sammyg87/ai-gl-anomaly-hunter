"""
GL Anomaly Hunter
-----------------
Reviews a month of general ledger entries before close and writes a review memo.

How it works:
  1. RULES (code) scan every entry and flag anything suspicious: duplicates,
     wrong account, weekend or manual entries, round dollars, new vendors,
     unusual amounts, and payments split to stay under the approval limit.
  2. THE AGENT (Claude + tools) investigates each flag: it pulls the entry, the
     vendor's history and related entries, then decides "Needs review" or
     "Likely OK", with a risk level, a reason, and a question for the owner.
     Rules catch everything; the agent separates real problems from noise.
  3. MEMO: Claude writes a short controller memo from the findings. A guardrail
     checks that every entry ID and dollar figure in it is real.

Usage:
  pip3 install anthropic pandas
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 gl_anomaly_agent.py                  # latest month in the file
  python3 gl_anomaly_agent.py --month 2026-10
  python3 gl_anomaly_agent.py --no-ai          # rules only, no API key needed
"""

import argparse
import json
import re

import pandas as pd

GL_FILE = "gl_detail.csv"
MODEL = "claude-sonnet-5-5"
MAX_AGENT_TURNS = 40
APPROVAL_LIMIT = 5000      # entries at or above this need sign-off
ROUND_DOLLAR_MIN = 5000    # flag round-thousand amounts at or above this
OUTLIER_X = 2.5            # flag amounts this many times the vendor's median


# ---------------- 1. RULES ----------------
def load(month: str | None):
    gl = pd.read_csv(GL_FILE, dtype={"invoice": str}).fillna({"invoice": ""})
    gl["month"] = gl["date"].str[:7]
    month = month or gl["month"].max()
    if month not in set(gl["month"]):
        raise SystemExit(f"No entries for {month} in {GL_FILE}")
    return month, gl


def run_rules(month: str, gl: pd.DataFrame) -> dict[str, list[str]]:
    cur = gl[gl["month"] == month]
    hist = gl[gl["month"] < month]
    flags: dict[str, list[str]] = {}

    def flag(je, why):
        flags.setdefault(je, []).append(why)

    usual_acct = hist.groupby("vendor")["account"].agg(lambda s: s.mode().iat[0]).to_dict()
    median_amt = hist.groupby("vendor")["amount"].median().to_dict()
    known_vendors = set(hist["vendor"])

    # duplicates: same vendor + invoice + amount anywhere in the ledger
    dupe_keys = gl[gl["invoice"] != ""].groupby(["vendor", "invoice", "amount"])["je_id"].apply(list)
    dupe_keys = {k: v for k, v in dupe_keys.items() if len(v) > 1}

    for r in cur.itertuples():
        key = (r.vendor, r.invoice, r.amount)
        if key in dupe_keys:
            others = [j for j in dupe_keys[key] if j != r.je_id]
            flag(r.je_id, f"Possible duplicate: same vendor, invoice and amount as {', '.join(others)}")
        if r.vendor in usual_acct and usual_acct[r.vendor] != r.account:
            flag(r.je_id, f"Unusual account: {r.vendor} is normally coded to {usual_acct[r.vendor]}")
        if pd.Timestamp(r.date).weekday() >= 5:
            flag(r.je_id, "Posted on a weekend")
        if "manual" in r.vendor.lower() or r.invoice == "":
            flag(r.je_id, "Manual entry with no invoice")
        if r.amount >= ROUND_DOLLAR_MIN and r.amount % 1000 == 0:
            flag(r.je_id, "Round-dollar amount")
        if r.vendor not in known_vendors:
            flag(r.je_id, "New vendor (no history in prior months)")
        if r.vendor in median_amt and r.amount >= OUTLIER_X * median_amt[r.vendor]:
            flag(r.je_id, f"Amount is {r.amount / median_amt[r.vendor]:.1f}x this vendor's usual")

    # split payments: 2+ entries, same vendor, same day, each just under the approval limit
    near = cur[(cur["amount"] < APPROVAL_LIMIT) & (cur["amount"] >= 0.9 * APPROVAL_LIMIT)]
    for (_, _), g in near.groupby(["vendor", "date"]):
        if len(g) >= 2:
            for je in g["je_id"]:
                flag(je, f"Possible split to avoid ${APPROVAL_LIMIT:,} approval limit "
                         f"({len(g)} entries same vendor, same day, total ${g['amount'].sum():,.2f})")
    return flags


# ---------------- 2. THE AGENT ----------------
class Tools:
    def __init__(self, month, gl, flags):
        self.month, self.gl, self.flags = month, gl, flags
        self.findings: dict[str, dict] = {}

    def entry(self, je_id):
        e = self.gl[self.gl["je_id"] == je_id]
        if e.empty:
            return {"error": f"No entry {je_id}"}
        r = e.iloc[0].to_dict()
        r["flags"] = self.flags.get(je_id, [])
        return r

    def vendor_history(self, vendor):
        h = self.gl[(self.gl["vendor"] == vendor) & (self.gl["month"] < self.month)]
        if h.empty:
            return {"vendor": vendor, "history": "No prior entries: new vendor."}
        return {"vendor": vendor, "prior_entries": len(h),
                "accounts_used": h["account"].value_counts().to_dict(),
                "median_amount": round(float(h["amount"].median()), 2),
                "min_amount": float(h["amount"].min()), "max_amount": float(h["amount"].max()),
                "monthly_totals": h.groupby("month")["amount"].sum().round(2).to_dict()}

    def related(self, vendor, invoice=""):
        m = self.gl["vendor"] == vendor
        if invoice:
            m &= self.gl["invoice"] == invoice
        else:
            m &= self.gl["month"] == self.month
        cols = ["je_id", "date", "account", "invoice", "description", "amount", "posted_by"]
        return {"entries": self.gl.loc[m, cols].to_dict("records")}

    def record(self, je_id, verdict, risk, reason, question=""):
        if je_id not in self.flags:
            raise ValueError(f"{je_id} is not a flagged entry. Flagged: {list(self.flags)}")
        self.findings[je_id] = {"verdict": verdict, "risk": risk, "reason": reason, "question": question}
        return {"saved": True, "remaining": [j for j in self.flags if j not in self.findings]}


TOOL_DEFS = [
    {"name": "get_entry", "description": "Get one journal entry and the rules it tripped.",
     "input_schema": {"type": "object", "properties": {"je_id": {"type": "string"}}, "required": ["je_id"]}},
    {"name": "get_vendor_history", "description": "Vendor's prior months: accounts used, typical amounts, monthly totals.",
     "input_schema": {"type": "object", "properties": {"vendor": {"type": "string"}}, "required": ["vendor"]}},
    {"name": "find_related_entries", "description": "Entries for the same vendor (and invoice, if given). "
     "Without an invoice, returns this vendor's entries in the review month.",
     "input_schema": {"type": "object", "properties": {"vendor": {"type": "string"}, "invoice": {"type": "string"}},
                      "required": ["vendor"]}},
    {"name": "record_finding", "description": "Record your conclusion for one flagged entry.",
     "input_schema": {"type": "object", "properties": {
         "je_id": {"type": "string"},
         "verdict": {"type": "string", "enum": ["Needs review", "Likely OK"]},
         "risk": {"type": "string", "enum": ["High", "Medium", "Low"]},
         "reason": {"type": "string", "description": "One or two sentences, citing what you found."},
         "question": {"type": "string", "description": "Question for the entry owner (if Needs review)."}},
         "required": ["je_id", "verdict", "risk", "reason"]}},
]


def investigate_with_claude(tools: Tools) -> None:
    import anthropic

    client = anthropic.Anthropic()
    flag_list = {je: tools.flags[je] for je in tools.flags}
    messages = [{"role": "user", "content": f"""You are a senior accountant reviewing the general ledger
for {tools.month} before month-end close at a farming company.

Automated rules flagged these entries (entry ID -> reasons):
{json.dumps(flag_list, indent=1)}

For EVERY flagged entry: use the tools to investigate (the entry, the vendor's history, related
entries), then call record_finding. Rules over-flag on purpose, so separate real problems
from normal activity. A fixed monthly premium in round dollars, for example, is Likely OK.
Rate risk by dollar impact and how likely it is to be an error or a control issue.
Don't calculate totals yourself. When every entry has a finding, reply DONE."""}]

    for _ in range(MAX_AGENT_TURNS):
        resp = client.messages.create(model=MODEL, max_tokens=4000, tools=TOOL_DEFS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                i = b.input
                out = {"get_entry": lambda: tools.entry(i["je_id"]),
                       "get_vendor_history": lambda: tools.vendor_history(i["vendor"]),
                       "find_related_entries": lambda: tools.related(i["vendor"], i.get("invoice", "")),
                       "record_finding": lambda: tools.record(i["je_id"], i["verdict"], i["risk"],
                                                              i["reason"], i.get("question", ""))}[b.name]()
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)})
            except Exception as e:
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Error: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})

    for je in tools.flags:  # safety net
        tools.findings.setdefault(je, {"verdict": "Needs review", "risk": "Medium",
                                       "reason": "Agent did not review this entry.", "question": ""})


def investigate_by_rules(tools: Tools) -> None:
    """Non-AI fallback for testing: every flag needs review; risk by rule type."""
    for je, why in tools.flags.items():
        text = " ".join(why)
        risk = "High" if any(k in text for k in ("duplicate", "split", "Unusual account")) else "Medium"
        tools.findings[je] = {"verdict": "Needs review", "risk": risk, "reason": "; ".join(why), "question": ""}


# ---------------- 3. MEMO + GUARDRAIL ----------------
def write_memo(facts: dict) -> str:
    import anthropic

    client = anthropic.Anthropic()
    prompt = f"""Write a short GL review memo for the controller, for {facts['month']} close.

Rules:
- Use ONLY the facts below; never calculate new numbers. Cite entry IDs (e.g. JE10301).
- Open with one line: how many entries were reviewed, how many need review, and the dollars at stake.
- Then the "Needs review" items, highest risk first: what's wrong, the amount, the next step.
- Then one line on what was cleared as Likely OK and why.
- Under 250 words, Markdown, plain business English.

FACTS:
{json.dumps(facts, indent=1, default=str)}"""
    resp = client.messages.create(model=MODEL, max_tokens=1200, messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if b.type == "text")


def guardrail(text: str, facts: dict, valid_ids: set[str]) -> list[str]:
    problems = [f"unknown entry {j}" for j in sorted(set(re.findall(r"JE\d+", text)) - valid_ids)]
    known = []

    def walk(x):
        if isinstance(x, dict):
            [walk(v) for v in x.values()]
        elif isinstance(x, list):
            [walk(v) for v in x]
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            known.append(abs(float(x)))
        elif isinstance(x, str):  # dollar amounts written inside rule/finding text count too
            known.extend(float(v.replace(",", "")) for v in re.findall(r"\$([\d,]+(?:\.\d+)?)", x))
    walk(facts)
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d+)?)\s?([KkMm])?(?![A-Za-z])", text):
        try:
            val = float(m.group(1).replace(",", "").rstrip("."))
        except ValueError:
            continue
        mult = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)
        val *= mult
        if not any(abs(val - k) <= max(0.5 * mult, val * 0.02, 1) for k in known):
            problems.append(m.group(0))
    return sorted(set(problems))


# ---------------- 4. REPORT ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month")
    ap.add_argument("--no-ai", action="store_true")
    args = ap.parse_args()

    month, gl = load(args.month)
    flags = run_rules(month, gl)
    cur = gl[gl["month"] == month]
    print(f"{len(cur)} entries in {month}; rules flagged {len(flags)}. Investigating...")

    tools = Tools(month, gl, flags)
    (investigate_by_rules if args.no_ai else investigate_with_claude)(tools)

    order = {"High": 0, "Medium": 1, "Low": 2}
    rows = []
    for je, f in tools.findings.items():
        e = tools.entry(je)
        rows.append({"je_id": je, "date": e["date"], "vendor": e["vendor"], "account": e["account"],
                     "amount": float(e["amount"]), "rules": flags[je], **f})
    rows.sort(key=lambda r: (r["verdict"] != "Needs review", order[r["risk"]], -r["amount"]))
    needs = [r for r in rows if r["verdict"] == "Needs review"]

    facts = {"month": month, "entries_reviewed": len(cur), "entries_flagged_by_rules": len(flags),
             "entries_needing_review": len(needs),
             "dollars_needing_review": round(sum(r["amount"] for r in needs), 2),
             "findings": rows}

    memo, warn = "_Memo skipped (--no-ai)._", ""
    if not args.no_ai:
        memo = write_memo(facts)
        bad = guardrail(memo, facts, set(flags))
        if bad:
            warn = f"\n> ⚠️ **Check these** — not found in the data: {', '.join(bad)}\n"

    table = ["| Verdict | Risk | Entry | Date | Vendor | Account | Amount | Finding | Question for owner |",
             "|---|---|---|---|---|---|---:|---|---|"]
    for r in rows:
        table.append(f"| {r['verdict']} | {r['risk']} | {r['je_id']} | {r['date']} | {r['vendor']} | {r['account']} | "
                     f"${r['amount']:,.2f} | {r['reason']} | {r['question'] or '—'} |")

    report = f"""# GL Review — {month}

**{len(cur)}** entries reviewed · **{len(flags)}** flagged by rules · **{len(needs)}** need review (${facts['dollars_needing_review']:,.2f})

## Memo
{memo}
{warn}
## Findings
{chr(10).join(table)}
"""
    out = f"gl_review_{month}.md"
    with open(out, "w") as f:
        f.write(report)
    print(report)
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
