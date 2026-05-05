"""Fetch most active Polymarket wallets from the Data API and output JSON."""
import json
import time
from datetime import datetime, timedelta, timezone
import urllib.request

BASE = "https://data-api.polymarket.com/trades"
now = datetime.now(timezone.utc)
wallets = {}

# Sample 7 time windows across 30 days (500 trades each = 3500 total samples)
windows = [
    (now - timedelta(hours=6), now),
    (now - timedelta(days=1), now - timedelta(hours=12)),
    (now - timedelta(days=3), now - timedelta(days=2)),
    (now - timedelta(days=7), now - timedelta(days=5)),
    (now - timedelta(days=14), now - timedelta(days=12)),
    (now - timedelta(days=21), now - timedelta(days=19)),
    (now - timedelta(days=28), now - timedelta(days=26)),
]

for start, end in windows:
    after_ts = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{BASE}?limit=500&after={after_ts}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            trades = json.loads(resp.read())
    except Exception as e:
        print(f"Error fetching {after_ts}: {e}")
        continue

    for t in trades:
        addr = (t.get("proxyWallet") or "").lower()
        if not addr:
            continue
        usd = float(t.get("price", 0)) * float(t.get("size", 0))
        ts = int(t.get("timestamp", 0))

        if addr not in wallets:
            wallets[addr] = {"count": 0, "volume": 0.0, "last_ts": 0, "name": t.get("name") or t.get("pseudonym")}
        wallets[addr]["count"] += 1
        wallets[addr]["volume"] += usd
        wallets[addr]["last_ts"] = max(wallets[addr]["last_ts"], ts)

    print(f"Window {after_ts}: {len(trades)} trades, {len(wallets)} unique wallets")
    time.sleep(1)

ranked = sorted(wallets.items(), key=lambda x: x[1]["count"], reverse=True)
qualified = [(addr, d) for addr, d in ranked if d["count"] >= 3 and d["volume"] >= 1000][:500]

print(f"\nTotal unique wallets: {len(wallets)}")
print(f"Qualified (3+ trades, $1K+ vol): {len(qualified)}")
print("\nTop 20 by activity:")
for addr, d in qualified[:20]:
    name = d["name"] or "-"
    cnt = d["count"]
    vol = d["volume"]
    print(f"  {addr[:12]}... trades={cnt:>3} vol=${vol:>10,.0f} name={name}")

output = [
    {"address": addr, "count": d["count"], "volume": d["volume"], "last_ts": d["last_ts"], "name": d["name"]}
    for addr, d in qualified
]
with open("/tmp/active_whales.json", "w") as f:
    json.dump(output, f)

print(f"\nWrote {len(qualified)} wallets to /tmp/active_whales.json")
