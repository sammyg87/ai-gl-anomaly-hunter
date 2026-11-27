"""Creates a fake general ledger (May-Oct 2026) for the made-up Sunridge Farms,
with a few problems planted in October for the agent to find."""
import numpy as np, pandas as pd
from datetime import date, timedelta
rng = np.random.default_rng(21)

vendors = [  # vendor, account, typical amount, std, entries per month
    ("Valley Diesel Supply", "Fuel & diesel", 6500, 900, 8),
    ("Central Ag Chemicals", "Chemicals & fertilizer", 9500, 2000, 9),
    ("Pacific Box & Pack", "Packing materials", 18000, 3000, 8),
    ("SJ Valley Equipment Repair", "Repairs & maintenance", 4200, 1500, 10),
    ("PG&E", "Utilities", 38000, 3000, 1),
    ("Westside Farm Insurance", "Insurance", 22000, 0, 1),
    ("Tulare Office Supply", "Office supplies", 450, 150, 4),
    ("Kings Labor Contractors", "Harvest labor", 52000, 8000, 8),
]
users = ["mlopez", "jchen", "rdiaz"]
rows, je = [], 10000

def bizday(y, m):
    d = date(y, m, int(rng.integers(1, 28)))
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d

for m in range(5, 11):
    for v, acct, amt, sd, n in vendors:
        for _ in range(n):
            je += 1
            a = round(max(50, rng.normal(amt, sd)), 2) if sd else float(amt)
            rows.append({"je_id": f"JE{je}", "date": bizday(2026, m).isoformat(), "account": acct, "vendor": v,
                         "invoice": f"INV-{rng.integers(100000, 999999)}", "description": f"{acct} - {v}",
                         "amount": a, "posted_by": str(rng.choice(users))})

oct_rows = [r for r in rows if r["date"].startswith("2026-10")]
def add(**kw):
    global je
    je += 1
    rows.append({"je_id": f"JE{je}", "posted_by": "mlopez", **kw})

# 1. Duplicate payment: same vendor, invoice and amount posted twice
dup = next(r for r in oct_rows if r["vendor"] == "Pacific Box & Pack")
add(date="2026-10-23", account=dup["account"], vendor=dup["vendor"], invoice=dup["invoice"],
    description=dup["description"], amount=dup["amount"])
# 2. Fuel invoice coded to Office supplies (wrong account)
add(date="2026-10-14", account="Office supplies", vendor="Valley Diesel Supply", invoice="INV-774120",
    description="Bulk diesel delivery - Block 7", amount=7180.00)
# 3. Large round-dollar manual entry posted on a Saturday
add(date="2026-10-17", account="Repairs & maintenance", vendor="Cash / Manual JE", invoice="",
    description="Misc adjustment", amount=25000.00)
# 4. Brand-new vendor, large payment
add(date="2026-10-28", account="Repairs & maintenance", vendor="Rapid Ag Services LLC", invoice="RA-0001",
    description="Equipment services", amount=18750.00)
# 5. Split to stay under a $5,000 approval limit: two entries, same vendor, same day
for amt in (4950.00, 4925.00):
    add(date="2026-10-09", account="Repairs & maintenance", vendor="SJ Valley Equipment Repair",
        invoice=f"INV-{rng.integers(100000, 999999)}", description="Tractor repair", amount=amt)
# 6. Amount far outside vendor history (chemicals ~ $9.5K, this is $41K)
add(date="2026-10-21", account="Chemicals & fertilizer", vendor="Central Ag Chemicals", invoice="INV-883301",
    description="Fertilizer - fall application", amount=41200.00)

df = pd.DataFrame(rows).sort_values(["date", "je_id"])
df.to_csv("gl_detail.csv", index=False)
print(f"wrote gl_detail.csv ({len(df)} entries)")
