#!/usr/bin/env python3
"""
import_auctionator.py - pull Auctionator scan data off this PC into the archive.

Runs on YOUR machine (the SavedVariables live here, so GitHub can't reach them).
Designed to be scheduled and forgotten:

  * PC off        -> it simply doesn't run, and nothing breaks
  * File unchanged -> it exits immediately, writes nothing, commits nothing
  * File changed   -> it writes one archive file stamped with the file's own
                      modification time, and optionally commits and pushes

What it captures per scan, per item:

  min_buyout  Auctionator's "m" - last seen minimum price. POINT IN TIME, so
              this is the field that differs between two scans on the same day.
  day/low/high/quantity
              Auctionator's daily buckets. Two scans in one day merge into one
              bucket (low=min, high=max, quantity=max), so these are daily
              resolution no matter how often you scan.

Every day still held in the database is exported each run, so the first import
back-fills whatever history Auctionator has been keeping.

  python import_auctionator.py                    # look, don't commit
  python import_auctionator.py --commit           # commit and push if changed
  python import_auctionator.py --force            # re-import even if unchanged
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys

import ahdb
from auctionator import Cbor, day_to_date, extract

DEFAULT_WOW = r"C:\Program Files (x86)\World of Warcraft\_anniversary_"
STATE_FILE = ".auctionator_state.json"


def saved_variable_files(wow_root: str) -> list[str]:
    """Every account's Auctionator.lua. .bak files are deliberately excluded."""
    pattern = os.path.join(wow_root, "WTF", "Account", "*",
                           "SavedVariables", "Auctionator.lua")
    return sorted(glob.glob(pattern))


def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except (ValueError, OSError):
            pass
    return {}


def rows_for_realm(data: dict, scan_day: int):
    """Flatten Auctionator's per-item, per-day tables into archive rows."""
    rows = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        key = str(key)
        item_id = int(key) if key.isdigit() else None
        highs = entry.get("h") or {}
        lows = entry.get("l") or {}
        avail = entry.get("a") or {}
        if not isinstance(highs, dict):
            continue

        days = set(highs)
        if isinstance(lows, dict):
            days |= set(lows)
        if isinstance(avail, dict):
            days |= set(avail)

        for day in days:
            try:
                day_num = int(day)
            except (TypeError, ValueError):
                continue
            high = highs.get(day)
            low = lows.get(day) if isinstance(lows, dict) else None
            qty = avail.get(day) if isinstance(avail, dict) else None
            rows.append({
                "item_key": key,
                "item_id": item_id,
                # "m" describes right now, so it belongs only to the day this
                # scan happened - stamping it on older days would be a lie.
                "min_buyout": entry.get("m") if day_num == scan_day else None,
                "quantity": qty,
                "day": day_to_date(day_num).isoformat(),
                "day_low": low if low is not None else high,
                "day_high": high,
            })
    return rows


def git(args: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wow", default=os.environ.get("WOW_PATH", DEFAULT_WOW),
                    help="WoW version folder (the one containing WTF and Interface)")
    ap.add_argument("--archive", default="data")
    ap.add_argument("--repo", default=".", help="repo root, for --commit")
    ap.add_argument("--commit", action="store_true",
                    help="git add/commit/push when something new was written")
    ap.add_argument("--force", action="store_true",
                    help="import even if the file hasn't changed")
    args = ap.parse_args()

    files = saved_variable_files(args.wow)
    if not files:
        print(f"No Auctionator.lua under {args.wow}\\WTF\\Account\\*\\SavedVariables",
              file=sys.stderr)
        print("Check --wow points at the right WoW version folder.", file=sys.stderr)
        return 1

    state_path = os.path.join(args.archive, STATE_FILE)
    state = load_state(state_path)
    wrote_any = False

    for path in files:
        account = path.split(os.sep)[-3]
        mtime = os.path.getmtime(path)
        stamp = dt.datetime.fromtimestamp(mtime, dt.timezone.utc)
        scan_time = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not args.force and abs(state.get(path, 0) - mtime) < 1:
            print(f"{account}: unchanged since {scan_time} - nothing to do")
            continue

        try:
            blobs = extract(path, None)
        except SystemExit:
            print(f"{account}: no price database in file, skipping")
            state[path] = mtime
            continue

        # Auctionator counts days from its own epoch; the scan's own day is the
        # one whose "m" values are current.
        scan_day = int((mtime - ahdb_scan_epoch()) // 86400)

        for realm, blob in blobs.items():
            try:
                data = Cbor(blob).load()
            except Exception as exc:                      # noqa: BLE001
                print(f"{account}/{realm}: decode failed ({exc}), skipping")
                continue
            if not isinstance(data, dict) or not data:
                continue

            rows = rows_for_realm(data, scan_day)
            if not rows:
                continue

            realm_slug = realm.lower().replace(" ", "-")
            archive = ahdb.archive_path(args.archive, "auctionator",
                                        realm_slug, scan_time)
            if os.path.exists(archive) and not args.force:
                print(f"{account}/{realm}: already archived at {scan_time}")
                continue

            days = sorted({r["day"] for r in rows})
            meta = {"source": "auctionator", "scan_time": scan_time,
                    "collected_at": dt.datetime.now(dt.timezone.utc)
                                      .isoformat(timespec="seconds"),
                    "realm": realm_slug, "realm_name": realm,
                    "faction": realm.rsplit(" ", 1)[-1].lower(),
                    "account": account,
                    "item_count": len({r["item_key"] for r in rows}),
                    "day_span": f"{days[0]}..{days[-1]}"}
            size = ahdb.write_archive(archive, meta, rows)
            print(f"{account}/{realm}: wrote {os.path.relpath(archive)}  "
                  f"{len(rows):,} rows, {meta['item_count']:,} items, "
                  f"days {meta['day_span']}, {size/1024:.0f} KB")
            wrote_any = True

        state[path] = mtime

    os.makedirs(args.archive, exist_ok=True)
    json.dump(state, open(state_path, "w"), indent=1)

    if not wrote_any:
        print("nothing new")

    if not args.commit:
        return 0

    # Commit whatever is actually pending, not just what this run wrote - an
    # earlier run may have written files that were never committed.
    git(["add", "-A"], args.repo)
    rc, _ = git(["diff", "--cached", "--quiet"], args.repo)
    if rc == 0:
        print("working tree clean - nothing to commit")
        return 0

    rc, out = git(["status", "--short", "--cached"], args.repo)
    print("committing:")
    for line in out.splitlines()[:12]:
        print("  " + line)

    git(["commit", "-m", f"auctionator scan {dt.datetime.now():%Y-%m-%d %H:%M}"],
        args.repo)
    for attempt in range(3):
        git(["pull", "--rebase", "--autostash"], args.repo)
        rc, out = git(["push"], args.repo)
        if rc == 0:
            print("pushed")
            return 0
    print(f"push failed: {out}", file=sys.stderr)
    return 1


def ahdb_scan_epoch() -> int:
    from auctionator import SCAN_DAY_0
    return SCAN_DAY_0


if __name__ == "__main__":
    raise SystemExit(main())
