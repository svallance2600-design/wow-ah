#!/usr/bin/env python3
"""
build_db.py - build a queryable SQLite database from the archive.

The archive is the record; this is a disposable index over it. Rebuild it
whenever you want, filtered however you want. Changing your watchlist is a
rebuild, not a re-collection.

  python build_db.py                        # everything, all sources
  python build_db.py --watchlist            # only ids in watchlist.txt
  python build_db.py --source blizzard --since 2026-09-01
  python build_db.py --items 13446 13444 --db potions.db
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

import ahdb


def load_names(root: str) -> list[tuple]:
    path = os.path.join(root, "items.csv")
    if not os.path.exists(path):
        return []
    return [(int(r["item_id"]), r["name"])
            for r in csv.DictReader(open(path, newline=""))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="data")
    ap.add_argument("--db", default="auctions.db")
    ap.add_argument("--source", choices=["tsm", "blizzard"],
                    help="limit to one source (default: both)")
    ap.add_argument("--since", help="only scans on/after this date, e.g. 2026-09-01")
    ap.add_argument("--watchlist", action="store_true",
                    help="only item ids listed in watchlist.txt")
    ap.add_argument("--watchlist-file", default="watchlist.txt")
    ap.add_argument("--items", nargs="*", type=int, help="only these item ids")
    args = ap.parse_args()

    keep: set[int] | None = None
    if args.items:
        keep = set(args.items)
    elif args.watchlist:
        keep = set(ahdb.load_watchlist(args.watchlist_file))
        if not keep:
            print(f"{args.watchlist_file} is empty or missing - nothing to filter to. "
                  "Add item ids (one per line), or drop --watchlist to build "
                  "everything.", file=sys.stderr)
            return 1

    if os.path.exists(args.db):
        os.remove(args.db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(args.db + suffix):
            os.remove(args.db + suffix)
    conn = ahdb.connect(args.db)

    files = list(ahdb.iter_archive(args.archive, args.source))
    if not files:
        print(f"No scan files under {args.archive}/. Run collect.py first.",
              file=sys.stderr)
        return 1

    scans = price_rows = skipped = 0
    for path in files:
        meta, rows = ahdb.read_archive(path)
        # A compacted month holds many scans in one file, with scan_time per
        # row; a raw scan file holds one, with scan_time in the header.
        if "compacted_month" in meta:
            groups: dict[str, list[dict]] = {}
            for row in rows:
                groups.setdefault(row.get("scan_time", ""), []).append(row)
        else:
            groups = {meta.get("scan_time", ""): rows}

        for scan_time, scan_rows in sorted(groups.items()):
            n = ingest(conn, meta, scan_time, scan_rows, keep, args.since)
            if n is None:
                skipped += 1
            else:
                scans += 1
                price_rows += n

    names = load_names(args.archive)
    if keep is not None:
        names = [n for n in names if n[0] in keep]
    conn.executemany("INSERT OR REPLACE INTO items (item_id, name) VALUES (?,?)", names)
    if keep is not None:
        conn.executemany("INSERT OR IGNORE INTO watchlist (item_id) VALUES (?)",
                         [(i,) for i in sorted(keep)])
    conn.commit()

    span = conn.execute(
        "SELECT MIN(scan_time) lo, MAX(scan_time) hi, COUNT(*) n FROM snapshots"
    ).fetchone()
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT item_id) n FROM item_prices").fetchone()["n"]
    conn.execute("ANALYZE")
    conn.close()

    size = os.path.getsize(args.db) / 1e6
    print(f"{args.db}: {span['n']} scans, {price_rows:,} price rows, "
          f"{distinct:,} distinct items, {size:.1f} MB")
    print(f"  span {span['lo']} .. {span['hi']}"
          + (f"  ({skipped} scans skipped by --since)" if skipped else ""))
    if keep is not None:
        print(f"  filtered to {len(keep)} watched items")
    return 0


def ingest(conn, meta, scan_time, rows, keep, since) -> int | None:
    """Insert one scan. Returns rows written, or None if the scan was skipped."""
    if since and scan_time[:10] < since:
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO snapshots (source, scan_time, collected_at, "
        "region, realm_slug, faction, item_count, total_auctions) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (meta.get("source"), scan_time, meta.get("collected_at"),
         meta.get("region"), meta.get("realm"), meta.get("faction") or None,
         int(meta.get("item_count") or 0) or len(rows),
         int(meta.get("total_auctions") or 0) or None))
    if not cur.rowcount:          # this scan is already in the db
        return 0
    sid = cur.lastrowid

    batch = []
    for row in rows:
        try:
            item_id = int(row["item_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if keep is not None and item_id not in keep:
            continue
        batch.append((
            sid, item_id,
            intish(row.get("min_buyout")), intish(row.get("market_value")),
            intish(row.get("mean_buyout")), intish(row.get("recent")),
            intish(row.get("historical")), intish(row.get("quantity")),
            intish(row.get("num_auctions"))))
    conn.executemany(
        "INSERT OR IGNORE INTO item_prices (snapshot_id, item_id, min_buyout, "
        "market_value, mean_buyout, recent, historical, quantity, num_auctions) "
        "VALUES (?,?,?,?,?,?,?,?,?)", batch)
    return len(batch)


def intish(value):
    if value in (None, "", "None"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
