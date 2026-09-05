#!/usr/bin/env python3
"""
plot.py - chart tracked item prices over time straight out of the SQLite db.

  python plot.py                                  # everything on the watchlist
  python plot.py --items 13446 13444 --days 14
  python plot.py --metric min_buyout --dark -o cheap.png
  python plot.py --supply                         # supply instead of price

Deliberately thin: it runs one query, then draws. Change the SQL in `series()`
or swap the whole thing for your own - the dataset is the point, this is a
starting handle on it.
"""
from __future__ import annotations

import argparse
import sqlite3

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

# Fixed categorical order - assigned by slot, never cycled. Validated for
# colour-vision deficiency separation as an ordered set; do not reshuffle.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767"]

THEME = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", secondary="#52514e",
                  muted="#898781", grid="#e1e0d9", axis="#c3c2b7",
                  series=SERIES_LIGHT),
    "dark": dict(surface="#1a1a19", ink="#ffffff", secondary="#c3c2b7",
                 muted="#898781", grid="#2c2c2a", axis="#383835",
                 series=SERIES_DARK),
}

METRICS = {
    "market_value": ("market_value_gold", "Market value"),
    "min_buyout": ("min_buyout_gold", "Cheapest buyout"),
}


def series(db: str, items: list[int] | None, days: int) -> pd.DataFrame:
    conn = sqlite3.connect(db)
    where = ["taken_at >= DATETIME('now', ?)"]
    params: list = [f"-{days} day"]
    if items:
        where.append("item_id IN (%s)" % ",".join("?" * len(items)))
        params += items
    else:
        where.append("item_id IN (SELECT item_id FROM watchlist)")
    sql = ("SELECT taken_at, item_id, item_name, min_buyout_gold, "
           "market_value_gold, quantity FROM prices WHERE "
           + " AND ".join(where) + " ORDER BY taken_at")
    df = pd.read_sql(sql, conn, params=params, parse_dates=["taken_at"])
    conn.close()
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="auctions.db")
    ap.add_argument("--items", nargs="*", type=int)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--metric", choices=list(METRICS), default="market_value")
    ap.add_argument("--supply", action="store_true",
                    help="plot quantity posted instead of price")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("-o", "--out", default="prices.png")
    args = ap.parse_args()

    df = series(args.db, args.items, args.days)
    if df.empty:
        print("No rows. Either nothing is on the watchlist yet, or the window "
              "is longer than the history you've collected.")
        return 1

    t = THEME["dark" if args.dark else "light"]
    if args.supply:
        col, ylabel, title = "quantity", "Units posted", "Auction house supply"
    else:
        col, ylabel = METRICS[args.metric]
        title = f"{ylabel} over time"

    names = (df.groupby("item_id")["item_name"].last()
               .reindex(df.groupby("item_id")[col].mean()
                          .sort_values(ascending=False).index))
    if len(names) > 8:
        print(f"note: {len(names)} items requested; charting the 8 with the "
              "highest average and dropping the rest. Facet them instead of "
              "cramming more onto one axis.")
        names = names.head(8)

    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=140)
    fig.patch.set_facecolor(t["surface"])
    ax.set_facecolor(t["surface"])

    for slot, (item_id, name) in enumerate(names.items()):
        g = df[df.item_id == item_id].sort_values("taken_at")
        color = t["series"][slot]
        ax.plot(g["taken_at"], g[col], lw=2, color=color, label=name,
                solid_capstyle="round")
        # direct label at the last point when the chart is small enough to read
        if len(names) <= 4 and not g.empty:
            last = g.iloc[-1]
            ax.annotate(f" {name}", (last["taken_at"], last[col]),
                        color=t["secondary"], fontsize=9,
                        va="center", ha="left", annotation_clip=False)

    ax.set_title(title, color=t["ink"], fontsize=13, loc="left", pad=12)
    ax.set_ylabel(ylabel + ("" if args.supply else " (gold)"),
                  color=t["secondary"], fontsize=10)
    ax.grid(True, axis="y", color=t["grid"], lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(t["axis"])
    ax.tick_params(colors=t["muted"], labelsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_ylim(bottom=0)

    # Identity is never carried by colour alone: legend whenever there are 2+
    # series, and direct labels too when there is room.
    if len(names) >= 2:
        leg = ax.legend(frameon=False, fontsize=9, loc="upper left",
                        bbox_to_anchor=(0, -0.12), ncol=min(4, len(names)))
        for text in leg.get_texts():
            text.set_color(t["secondary"])

    if len(names) <= 4:
        fig.subplots_adjust(right=0.82)
    fig.tight_layout()
    fig.savefig(args.out, facecolor=t["surface"], bbox_inches="tight")
    print(f"wrote {args.out}  ({len(df):,} points, {len(names)} series, "
          f"{df.taken_at.min():%Y-%m-%d} to {df.taken_at.max():%Y-%m-%d})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
