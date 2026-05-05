"""Import leaderboard wallets into whale_scores via psql."""
import json
import subprocess
import sys

with open("/tmp/leaderboard_whales.json") as f:
    entries = json.load(f)

values = []
for e in entries:
    addr = (e.get("proxyWallet") or "").lower()
    if not addr:
        continue
    pnl = float(e.get("pnl", 0))
    vol = float(e.get("vol", 0))
    name = (e.get("userName") or "").replace("'", "''")[:60]

    if vol > 0:
        roi = pnl / vol
        implied_wr = min(0.85, max(0.40, 0.50 + roi * 2.0))
    else:
        implied_wr = 0.50

    big_wr = implied_wr * 0.80
    median_gain = max(0, min(0.30, roi * 0.5)) if vol > 0 else 0
    composite = (0.50 * big_wr + 0.30 * max(0, median_gain) + 0.20 * implied_wr) * 100
    composite = min(75, max(15, composite))

    values.append(
        f"('{addr}', '{name}', {pnl:.2f}, {implied_wr:.4f}, {big_wr:.4f}, "
        f"{median_gain:.4f}, -0.05, 60, {vol:.2f}, {composite:.2f}, true, NOW())"
    )

sql = """
INSERT INTO whale_scores (
    address, display_name, total_pnl_usd, win_rate, big_win_rate,
    median_gain_pct, median_loss_pct, markets_traded,
    total_volume_usd, composite_score, is_active, scored_at
) VALUES
""" + ",\n".join(values) + """
ON CONFLICT (address) DO UPDATE SET
    display_name = COALESCE(EXCLUDED.display_name, whale_scores.display_name),
    total_pnl_usd = EXCLUDED.total_pnl_usd,
    win_rate = EXCLUDED.win_rate,
    big_win_rate = EXCLUDED.big_win_rate,
    median_gain_pct = EXCLUDED.median_gain_pct,
    total_volume_usd = EXCLUDED.total_volume_usd,
    composite_score = GREATEST(whale_scores.composite_score, EXCLUDED.composite_score),
    is_active = true,
    scored_at = NOW();
"""

with open("/tmp/upsert_whales.sql", "w") as f:
    f.write(sql)

print(f"Generated SQL for {len(values)} wallets")

result = subprocess.run(
    ["psql", "-h", "127.0.0.1", "-U", "predbot", "-d", "predbot", "-f", "/tmp/upsert_whales.sql"],
    capture_output=True, text=True,
    env={"PGPASSWORD": "6fb2cc5c72a6c2a2ff253fd38a274651", "PATH": "/usr/bin:/bin"}
)
if result.returncode == 0:
    print("Import successful:", result.stdout.strip().split("\n")[-1])
else:
    print("Error:", result.stderr[:500])
