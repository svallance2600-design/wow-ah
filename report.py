#!/usr/bin/env python3
"""
report.py - build a "what should I sell" page from the archive.

  py build_db.py            # archive -> auctions.db
  py report.py              # auctions.db -> report.html
  py report.py --open       # ...and open it

Aimed at selling, not flipping: it answers "is this above its own normal, and
is anyone competing with me", not "can I buy low and relist".

Metrics, and what each is worth trusting
----------------------------------------
floor      cheapest buyout right now (TSM minBuyout). What you must beat.
market     TSM's smoothed market value. What it's "worth".
avg        TSM's long-run historical average. TSM computes this, so it is
           meaningful from your very first scan - no waiting for history.
vs avg     market / avg. Above ~1.2 = trading rich. THE sell signal on day one.
floor/mkt  below ~0.7 = someone is dumping under market. Don't list into it.
supply     units posted, from YOUR Auctionator scan. Nothing else provides it.
trend      needs several days of TSM scans; blank until then.

Ratios on tiny numbers lie, so anything with avg below MIN_GOLD is excluded -
an item averaging 2 silver showing "15000x" is arithmetic, not opportunity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import sqlite3
import webbrowser

MIN_GOLD = 1.0          # ignore items whose long-run average is below this
RECIPE = re.compile(r"^(Design|Pattern|Plans|Schematic|Recipe|Formula|Manual|Book):")

# Your trade categories. Edit freely - this is the whitelist.
CATEGORIES = {
    "Primals": r"^Primal (Fire|Water|Air|Earth|Life|Mana|Shadow|Nether|Might)$",
    "TBC herbs": r"^(Felweed|Dreaming Glory|Ragveil|Terocone|Ancient Lichen|"
                 r"Netherbloom|Nightmare Vine|Mana Thistle|Fel Lotus)$",
    "Flasks": r"^Flask of ",
    "Potions": r"(Super (Healing|Mana) Potion|Super Rejuvenation|Destruction Potion|"
               r"Haste Potion|Heroic Potion|Insane Strength|Shrouding Potion|"
               r"Fel Regeneration|Major Dreamless)",
    "Elixirs": r"^(Elixir of|Adept's Elixir|Onslaught Elixir|Fel Strength|Fel Mana)",
}

GEM_BASES = ["Crimson Spinel", "Lionseye", "Empyrean Sapphire", "Seaspray Emerald",
             "Shadowsong Amethyst", "Pyrestone", "Living Ruby", "Noble Topaz",
             "Talasite", "Star of Elune", "Dawnstone", "Nightseye",
             "Blood Garnet", "Flame Spessarite", "Deep Peridot",
             "Golden Draenite", "Shadow Draenite", "Azure Moonstone"]


def latest_prices(conn):
    """Most recent TSM row per item, with your latest Auctionator supply."""
    return {r["name"]: dict(r) for r in conn.execute("""
        WITH last_tsm AS (
            SELECT p.item_key, p.item_id, p.min_buyout, p.market_value,
                   p.historical, p.recent,
                   ROW_NUMBER() OVER (PARTITION BY p.item_key
                                      ORDER BY s.scan_time DESC) rn
            FROM item_prices p JOIN snapshots s USING (snapshot_id)
            WHERE s.source = 'tsm' AND p.market_value > 0),
        last_supply AS (
            SELECT p.item_key, p.quantity,
                   ROW_NUMBER() OVER (PARTITION BY p.item_key
                                      ORDER BY p.day DESC) rn
            FROM item_prices p JOIN snapshots s USING (snapshot_id)
            WHERE s.source = 'auctionator' AND p.quantity IS NOT NULL)
        SELECT i.name, t.item_id,
               t.min_buyout/10000.0 AS floor,
               t.market_value/10000.0 AS market,
               t.historical/10000.0 AS avg,
               a.quantity AS supply
        FROM last_tsm t
        JOIN items i ON i.item_id = t.item_id
        LEFT JOIN last_supply a ON a.item_key = t.item_key AND a.rn = 1
        WHERE t.rn = 1 AND i.name IS NOT NULL""")}


def history(conn):
    """Per-item price series, for sparklines. Empty until several scans exist."""
    out: dict[str, list[float]] = {}
    for r in conn.execute("""
            SELECT i.name, s.scan_time, p.market_value/10000.0 AS v
            FROM item_prices p JOIN snapshots s USING (snapshot_id)
            JOIN items i ON i.item_id = p.item_id
            WHERE s.source='tsm' AND p.market_value > 0
            ORDER BY s.scan_time"""):
        out.setdefault(r["name"], []).append(r["v"])
    return out


def verdict(row):
    """Plain-language call. Deliberately conservative - it says 'wait' a lot."""
    mv, av, fl, sup = row["market"], row["avg"], row["floor"], row["supply"]
    if not av or av < MIN_GOLD:
        return "", ""
    ratio = mv / av
    undercut = (fl / mv) if (fl and mv) else None
    if undercut is not None and undercut < 0.7:
        return "dumped", "bad"
    if ratio >= 1.2 and (sup is None or sup <= 60):
        return "SELL", "good"
    if ratio >= 1.2:
        return "rich, crowded", "warn"
    if ratio <= 0.8:
        return "cheap - restock", "info"
    return "hold", ""


def spark(vals, w=88, h=20):
    if len(vals) < 3:
        return '<span class="muted">-</span>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i*w/(len(vals)-1):.1f},{h-(v-lo)/rng*h:.1f}"
                   for i, v in enumerate(vals))
    up = vals[-1] >= vals[0]
    col = "var(--good)" if up else "var(--bad)"
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


def gem_margins(prices):
    out = []
    for base in GEM_BASES:
        raw = prices.get(base)
        if not raw or not raw["market"]:
            continue
        for name, r in prices.items():
            if name == base or RECIPE.match(name):
                continue
            if name.endswith(" " + base) and r["market"] > 0:
                out.append({"cut": name, "base": base, "cut_price": r["market"],
                            "raw_price": raw["market"],
                            "margin": r["market"] - raw["market"],
                            "cut_supply": r["supply"], "raw_supply": raw["supply"]})
    out.sort(key=lambda d: -d["margin"])
    return out


def esc(s):
    return html.escape(str(s))


def num(v, dp=2):
    return f"{v:,.{dp}f}" if v is not None else '<span class="muted">-</span>'


def build(conn, out_path):
    prices = latest_prices(conn)
    hist = history(conn)
    span = conn.execute("SELECT MIN(scan_time) lo, MAX(scan_time) hi, "
                        "COUNT(*) n FROM snapshots").fetchone()
    tsm_scans = conn.execute("SELECT COUNT(*) n FROM snapshots "
                             "WHERE source='tsm'").fetchone()["n"]

    sections = []
    for title, pattern in CATEGORIES.items():
        rx = re.compile(pattern)
        rows = [r for n, r in prices.items()
                if rx.search(n) and not RECIPE.match(n)
                and r["avg"] and r["avg"] >= MIN_GOLD]
        rows.sort(key=lambda r: -(r["market"] / r["avg"]))
        if rows:
            sections.append((title, rows))

    parts = []
    for title, rows in sections:
        body = []
        for r in rows:
            v, cls = verdict(r)
            ratio = r["market"] / r["avg"] if r["avg"] else 0
            body.append(
                f'<tr><td class="name">{esc(r["name"])}</td>'
                f'<td class="n">{num(r["floor"])}</td>'
                f'<td class="n">{num(r["market"])}</td>'
                f'<td class="n muted">{num(r["avg"])}</td>'
                f'<td class="n"><b>{ratio:.2f}</b></td>'
                f'<td class="n">{num(r["supply"], 0)}</td>'
                f'<td class="spark">{spark(hist.get(r["name"], []))}</td>'
                f'<td><span class="tag {cls}">{esc(v)}</span></td></tr>')
        parts.append(f"""
<section><h2>{esc(title)} <span class="muted">({len(rows)})</span></h2>
<div class="scroll"><table>
<thead><tr><th>item</th><th class="n">floor</th><th class="n">market</th>
<th class="n">avg</th><th class="n">vs avg</th><th class="n">supply</th>
<th>trend</th><th>call</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div></section>""")

    gems = gem_margins(prices)
    grows = "".join(
        f'<tr><td class="name">{esc(g["cut"])}</td>'
        f'<td class="n">{num(g["cut_price"], 1)}</td>'
        f'<td class="n muted">{num(g["raw_price"], 1)}</td>'
        f'<td class="n"><b>{num(g["margin"], 1)}</b></td>'
        f'<td class="n">{100*g["margin"]/g["raw_price"]:.0f}%</td>'
        f'<td class="n">{num(g["cut_supply"], 0)}</td>'
        f'<td class="n muted">{num(g["raw_supply"], 0)}</td></tr>'
        for g in gems[:40])
    negative = sum(1 for g in gems if g["margin"] < 0)

    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dreamscythe-Horde sell board</title>
<style>
:root {{
  color-scheme: light;
  --surface:#fcfcfb; --page:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --good:#0ca30c; --bad:#d03b3b; --warn:#fab219; --info:#2a78d6;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  color-scheme: dark;
  --surface:#1a1a19; --page:#0d0d0d; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --good:#0ca30c; --bad:#e66767; --warn:#fab219; --info:#3987e5;
}} }}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface:#1a1a19; --page:#0d0d0d; --ink:#fff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --good:#0ca30c; --bad:#e66767; --warn:#fab219; --info:#3987e5;
}}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--page); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:32px 20px 64px }}
h1 {{ font-size:22px; margin:0 0 4px }}
h2 {{ font-size:15px; margin:0 0 10px; font-weight:600 }}
.sub {{ color:var(--ink2); margin:0 0 28px }}
section {{ background:var(--surface); border:1px solid var(--grid);
  border-radius:10px; padding:16px; margin-bottom:18px }}
.scroll {{ overflow-x:auto }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums }}
th {{ text-align:left; font-weight:600; color:var(--muted); font-size:11px;
  text-transform:uppercase; letter-spacing:.04em;
  border-bottom:1px solid var(--axis); padding:6px 10px 6px 0; white-space:nowrap }}
td {{ padding:5px 10px 5px 0; border-bottom:1px solid var(--grid) }}
tr:last-child td {{ border-bottom:0 }}
.n {{ text-align:right }}
.name {{ max-width:280px }}
.muted {{ color:var(--muted) }}
.spark {{ width:96px; line-height:0 }}
.tag {{ font-size:11px; padding:2px 7px; border-radius:99px; white-space:nowrap;
  border:1px solid var(--axis); color:var(--ink2) }}
.tag.good {{ color:#fff; background:var(--good); border-color:var(--good) }}
.tag.bad {{ color:#fff; background:var(--bad); border-color:var(--bad) }}
.tag.warn {{ color:#0b0b0b; background:var(--warn); border-color:var(--warn) }}
.tag.info {{ color:#fff; background:var(--info); border-color:var(--info) }}
.note {{ background:var(--surface); border:1px solid var(--grid); border-left:3px solid var(--info);
  border-radius:8px; padding:12px 14px; margin-bottom:18px; color:var(--ink2) }}
</style></head><body><div class="wrap">
<h1>Dreamscythe-Horde sell board</h1>
<p class="sub">Generated {generated} &middot; {span['n']} snapshots
({tsm_scans} TSM) &middot; {esc(str(span['lo'])[:16])} to {esc(str(span['hi'])[:16])}</p>

{'<div class="note"><b>Trend columns are empty by design.</b> Sparklines need several TSM scans; you have ' + str(tsm_scans) + '. The <b>vs avg</b> column works today because TSM computes the long-run average itself.</div>' if tsm_scans < 4 else ''}

{''.join(parts)}

<section><h2>Jewelcrafting cut margins <span class="muted">(cut price minus raw gem)</span></h2>
<p class="sub" style="margin:0 0 12px">Both prices are market value, so this is the
spread before your cut is undercut. {negative} of {len(gems)} cuts currently sell
for <em>less</em> than the raw gem &mdash; those are listed at the bottom of the sort.</p>
<div class="scroll"><table>
<thead><tr><th>cut gem</th><th class="n">cut</th><th class="n">raw</th>
<th class="n">margin</th><th class="n">%</th><th class="n">cut supply</th>
<th class="n">raw supply</th></tr></thead>
<tbody>{grows}</tbody></table></div></section>

<section><h2>How to read this</h2>
<table><tbody>
<tr><td><span class="tag good">SELL</span></td><td>Trading at least 20% above its
own long-run average, and fewer than ~60 units posted. List into it.</td></tr>
<tr><td><span class="tag warn">rich, crowded</span></td><td>Price is high but supply
is deep &mdash; you will be undercut fast.</td></tr>
<tr><td><span class="tag bad">dumped</span></td><td>Cheapest buyout is under 70% of
market value. Someone is clearing stock; don't list against them.</td></tr>
<tr><td><span class="tag info">cheap - restock</span></td><td>Below its average.
Buying / crafting window rather than a selling one.</td></tr>
<tr><td><span class="tag">hold</span></td><td>Within normal range. Nothing to do.</td></tr>
</tbody></table></section>
</div></body></html>"""
    open(out_path, "w", encoding="utf-8").write(doc)
    return out_path, len(prices), len(gems)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="auctions.db")
    ap.add_argument("-o", "--out", default="report.html")
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"{args.db} not found - run build_db.py first")
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    path, n, g = build(conn, args.out)
    conn.close()
    print(f"wrote {path}  ({n:,} priced items, {g} gem cuts)")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
