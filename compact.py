#!/usr/bin/env python3
"""
compact.py - merge a month of scan files into one long-format file.

The main win is FILE COUNT, not bytes. Collecting hourly produces ~8,700 small
files a year; that is slow to clone, slow to walk, and a lot of git objects.
One file per month per source fixes that.

Byte savings are a bonus and depend entirely on how static your realm's prices
are: rows are sorted by (item_id, scan_time) so each item's history lands on
consecutive lines, which compresses well when prices repeat and barely at all
when everything moves every scan. Measured on synthetic worst-case data (every
item changing every scan) the saving was ~1%; on a quiet realm it is much
better. So this always does a DRY RUN first and prints the real number for
your data - decide from that, don't take my word for it.

  python compact.py --month 2026-09              # measure, change nothing
  python compact.py --month 2026-09 --apply      # write it, delete the pieces
  python compact.py --all --apply                # every complete past month

Compacted files are read transparently by build_db.py, so this is safe to run
whenever; nothing downstream needs to know.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import os
import sys

import ahdb

COMPACT_COLUMNS = ["scan_time"] + ahdb.ARCHIVE_COLUMNS


def month_dirs(root: str, source: str | None = None):
    for src in sorted(os.listdir(root)):
        path = os.path.join(root, src)
        if not os.path.isdir(path) or (source and src != source):
            continue
        for month in sorted(os.listdir(path)):
            mdir = os.path.join(path, month)
            if os.path.isdir(mdir):
                yield src, month, mdir


def compact_month(src: str, month: str, mdir: str, apply: bool) -> tuple[int, int, int]:
    parts = [os.path.join(mdir, f) for f in sorted(os.listdir(mdir))
             if f.endswith(".csv.gz") and not f.startswith("_compacted-")]
    if len(parts) < 2:
        return 0, 0, 0

    before = sum(os.path.getsize(p) for p in parts)
    rows, meta_seen = [], {}
    for path in parts:
        meta, records = ahdb.read_archive(path)
        scan_time = meta.get("scan_time", "")
        meta_seen = {k: v for k, v in meta.items()
                     if k in ("source", "region", "realm", "faction", "game_type")}
        for record in records:
            record["scan_time"] = scan_time
            rows.append(record)

    # The whole point: group each item's history together so the numbers
    # next to each other are similar.
    rows.sort(key=lambda r: (int(r.get("item_id") or 0), r["scan_time"]))

    buf = io.StringIO()
    for key in sorted(meta_seen):
        buf.write(f"# {key}={meta_seen[key]}\n")
    buf.write(f"# compacted_month={month}\n# scan_count={len(parts)}\n")
    writer = csv.DictWriter(buf, fieldnames=COMPACT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    out = os.path.join(mdir, f"_compacted-{src}-{month}.csv.gz")
    tmp = out + ".tmp"
    with open(tmp, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(buf.getvalue().encode())
    after = os.path.getsize(tmp)

    if apply:
        os.replace(tmp, out)
        for path in parts:
            os.remove(path)
    else:
        os.remove(tmp)
    return before, after, len(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="data")
    ap.add_argument("--month", help="e.g. 2026-09")
    ap.add_argument("--source", choices=["tsm", "blizzard"])
    ap.add_argument("--all", action="store_true",
                    help="every month except the current one")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without this it only reports")
    args = ap.parse_args()

    if not args.month and not args.all:
        ap.error("pass --month YYYY-MM or --all")

    this_month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    total_before = total_after = total_files = total_months = 0
    for src, month, mdir in month_dirs(args.archive, args.source):
        if args.month and month != args.month:
            continue
        if args.all and month == this_month:
            continue          # still being written to
        before, after, files_in = compact_month(src, month, mdir, args.apply)
        if not before:
            continue
        total_months += 1
        total_before += before
        total_after += after
        total_files += files_in
        print(f"{src}/{month}: {files_in} files, {before/1e6:.1f} MB "
              f"-> 1 file, {after/1e6:.1f} MB "
              f"({100*(1-after/before):+.0f}% bytes)")

    if not total_before:
        print("nothing to compact")
        return 0
    print(f"\ntotal: {total_files} files -> {total_months}, "
          f"{total_before/1e6:.1f} MB -> {total_after/1e6:.1f} MB "
          f"({100*(1-total_after/total_before):+.0f}% bytes)")
    if not args.apply:
        print("dry run - nothing changed. Re-run with --apply to keep it.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
