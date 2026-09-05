#!/usr/bin/env python3
"""
watch.py - manage watchlist.txt, the list of items you actually care about.

The watchlist never affects collection; everything is always collected. It only
decides what build_db.py and plot.py show you, so you can change your mind
freely and rebuild against history you already have.

  python watch.py search flask          # find item ids by name
  python watch.py add 13446 13444 8846
  python watch.py add "Major Healing Potion"
  python watch.py list
  python watch.py rm 8846
  python watch.py top --limit 30        # busiest items, as whitelist candidates
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import sys

import ahdb


def names(root: str) -> dict[int, str]:
    path = os.path.join(root, "items.csv")
    if not os.path.exists(path):
        return {}
    return {int(r["item_id"]): r["name"]
            for r in csv.DictReader(open(path, newline=""))}


def read_lines(path: str) -> list[str]:
    return open(path).read().splitlines() if os.path.exists(path) else []


def write_list(path: str, ids: list[int], lookup: dict[int, str]) -> None:
    with open(path, "w") as fh:
        fh.write("# Item ids to include when building a filtered database.\n")
        fh.write("# Collection always captures everything; this is a view filter.\n")
        for item_id in ids:
            label = lookup.get(item_id)
            fh.write(f"{item_id}" + (f"  # {label}\n" if label else "\n"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="data")
    ap.add_argument("--file", default="watchlist.txt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add"); p_add.add_argument("items", nargs="+")
    p_rm = sub.add_parser("rm"); p_rm.add_argument("items", nargs="+")
    sub.add_parser("list")
    p_search = sub.add_parser("search"); p_search.add_argument("term")
    p_top = sub.add_parser("top")
    p_top.add_argument("--limit", type=int, default=25)
    p_top.add_argument("--db", default="auctions.db")
    args = ap.parse_args()

    lookup = names(args.archive)
    current = ahdb.load_watchlist(args.file)

    if args.cmd == "list":
        if not current:
            print(f"{args.file} is empty")
        for item_id in current:
            print(f"{item_id:>8}  {lookup.get(item_id, '?')}")
        return 0

    if args.cmd == "search":
        hits = [(i, n) for i, n in sorted(lookup.items(), key=lambda kv: kv[1])
                if args.term.lower() in n.lower()]
        for item_id, name in hits[:60]:
            mark = "*" if item_id in current else " "
            print(f"{mark} {item_id:>8}  {name}")
        if not hits:
            print("no match. Names come from the TSM scan - run collect.py once, "
                  "or look the id up on wowhead and add it directly.")
        elif len(hits) > 60:
            print(f"... {len(hits)-60} more")
        return 0

    if args.cmd == "top":
        # Suggest whitelist candidates: the items with the most posted supply,
        # which is a decent proxy for "actually traded".
        if not os.path.exists(args.db):
            print(f"{args.db} not found - run build_db.py first.", file=sys.stderr)
            return 1
        conn = ahdb.connect(args.db)
        rows = conn.execute(
            "SELECT item_id, item_name, ROUND(AVG(COALESCE(quantity,0))) AS supply, "
            "ROUND(AVG(market_value_gold),2) AS price, COUNT(*) AS seen "
            "FROM prices GROUP BY item_id "
            "ORDER BY supply DESC, seen DESC LIMIT ?", (args.limit,)).fetchall()
        for r in rows:
            mark = "*" if r["item_id"] in current else " "
            print(f"{mark} {r['item_id']:>8}  {str(r['item_name'])[:38]:38} "
                  f"supply~{r['supply']:>6}  {r['price']:>9}g")
        conn.close()
        print("\n* = already watched. Add with: python watch.py add <ids>")
        return 0

    resolved = []
    for token in args.items:
        if token.isdigit():
            resolved.append(int(token))
            continue
        matches = [i for i, n in lookup.items() if n.lower() == token.lower()]
        if not matches:
            print(f"no item named {token!r} in the scan - add it by id instead",
                  file=sys.stderr)
            continue
        resolved.append(matches[0])

    if args.cmd == "add":
        merged = list(dict.fromkeys(current + resolved))
    else:
        merged = [i for i in current if i not in resolved]

    write_list(args.file, merged, lookup)
    changed = set(merged) ^ set(current)
    for item_id in sorted(changed):
        verb = "added" if item_id in merged else "removed"
        print(f"{verb} {item_id}  {lookup.get(item_id, '?')}")
    print(f"{len(merged)} items in {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
