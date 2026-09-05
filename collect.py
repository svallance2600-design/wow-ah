#!/usr/bin/env python3
"""
collect.py - append one immutable scan file to the archive.

Collects EVERY item the source publishes. Filtering happens later, at
build_db.py time, so changing your mind about what matters never costs you
data you already have.

  python collect.py                     # tsm, dreamscythe-horde
  python collect.py --source blizzard --realm dreamscythe --faction horde
  python collect.py --source both       # run both, whichever have refreshed

Both sources dedupe on the upstream scan time, so scheduling this hourly is
safe and cheap: if nothing has refreshed, it writes nothing and exits 0.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import io
import os
import sys

import requests

import ahdb


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def as_int(value):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def to_iso(http_date: str) -> str:
    """'Thu, 04 Sep 2026 20:41:44 GMT' -> '2026-09-04T20:41:44Z'."""
    if not http_date:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return (parsed.astimezone(dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def already_have(root: str, source: str, realm: str, scan_time: str) -> bool:
    return os.path.exists(ahdb.archive_path(root, source, realm, scan_time))


def report(path: str, size: int, rows: int, scan_time: str) -> None:
    print(f"  wrote {os.path.relpath(path)}  "
          f"{rows:,} items, {size/1024:.0f} KB, scan {scan_time}", file=sys.stderr)


# ---------------------------------------------------------------------------
# TSM public CSV
# ---------------------------------------------------------------------------

def collect_tsm(root: str, game_type: str, region: str, realm: str) -> bool:
    url = ahdb.tsm_url(game_type, region, realm)
    r = requests.get(url, timeout=180, headers={"Accept-Encoding": "gzip"})
    if r.status_code == 404:
        raise SystemExit(
            f"404 from {url}\nCheck the slugs against your realm's page URL:\n"
            "  https://tradeskillmaster.com/<gameType>/<region>/<realm>\n"
            "  e.g. https://tradeskillmaster.com/classic/us-fresh/dreamscythe-horde")
    r.raise_for_status()

    src_rows = list(csv.DictReader(io.StringIO(r.text)))
    if not src_rows:
        print("  tsm: empty CSV", file=sys.stderr)
        return False

    scan_time = next((x.get("updatedAt") for x in src_rows if x.get("updatedAt")), "")
    if already_have(root, "tsm", realm, scan_time):
        print(f"  tsm: already have scan {scan_time}", file=sys.stderr)
        return False

    rows, names = [], []
    for src in src_rows:
        item_id = as_int(src.get("itemId"))
        if item_id is None:
            continue
        rows.append({
            "item_key": str(item_id),
            "item_id": item_id,
            "min_buyout": as_int(src.get("minBuyout")),
            "market_value": as_int(src.get("marketValue")),
            "recent": as_int(src.get("recent")),
            "historical": as_int(src.get("historical")),
        })
        if src.get("name"):
            names.append((item_id, src["name"]))

    path = ahdb.archive_path(root, "tsm", realm, scan_time)
    meta = {"source": "tsm", "scan_time": scan_time, "collected_at": utcnow(),
            "region": region, "realm": realm, "game_type": game_type,
            "faction": realm.rsplit("-", 1)[-1] if "-" in realm else "",
            "item_count": len(rows)}
    size = ahdb.write_archive(path, meta, rows)
    report(path, size, len(rows), scan_time)
    update_item_names(root, names)
    return True


def update_item_names(root: str, pairs: list[tuple[int, str]]) -> None:
    """Names live in one small file, not repeated in every scan."""
    path = os.path.join(root, "items.csv")
    known: dict[int, str] = {}
    if os.path.exists(path):
        for row in csv.DictReader(open(path, newline="")):
            known[int(row["item_id"])] = row["name"]
    before = len(known)
    for item_id, name in pairs:
        known.setdefault(item_id, name)
    if len(known) == before:
        return
    os.makedirs(root, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item_id", "name"])
        for item_id in sorted(known):
            writer.writerow([item_id, known[item_id]])
    print(f"  items.csv: {len(known):,} names (+{len(known)-before})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Blizzard Game Data API (raw listings -> supply + depth)
# ---------------------------------------------------------------------------

def aggregate(auctions: list[dict], depth_fraction: float = 0.25):
    """Collapse raw auctions into per-item stats.

    In Classic, `buyout` is the price for the WHOLE stack, so unit price is
    buyout / quantity. Bid-only auctions count toward quantity but are
    excluded from every price figure.
    """
    by_item: dict[int, list[tuple[int, int]]] = {}
    for a in auctions:
        item_id = (a.get("item") or {}).get("id")
        if item_id is None:
            continue
        qty = a.get("quantity") or 0
        if qty <= 0:
            continue
        unit = a["buyout"] // qty if a.get("buyout") else (a.get("unit_price") or 0)
        by_item.setdefault(item_id, []).append((unit, qty))

    rows = []
    for item_id, entries in by_item.items():
        total_qty = sum(q for _, q in entries)
        priced = sorted([(u, q) for u, q in entries if u > 0])
        if not priced:
            rows.append({"item_key": str(item_id), "item_id": item_id,
                         "quantity": total_qty, "num_auctions": len(entries)})
            continue
        priced_qty = sum(q for _, q in priced)
        target = max(1, int(priced_qty * depth_fraction))
        taken = acc = 0
        for unit, qty in priced:
            use = min(qty, target - taken)
            acc += unit * use
            taken += use
            if taken >= target:
                break
        rows.append({
            "item_key": str(item_id),
            "item_id": item_id,
            "min_buyout": priced[0][0],
            "market_value": acc // taken if taken else priced[0][0],
            "mean_buyout": sum(u * q for u, q in priced) // priced_qty,
            "quantity": total_qty,
            "num_auctions": len(entries),
        })
    return rows


def collect_blizzard(root: str, region: str, realm: str, faction: str) -> bool:
    bz = ahdb.Blizzard.from_env(region)
    ns, cr_id = ahdb.resolve_realm(bz, realm)

    _, ah_index = bz.get(f"/data/wow/connected-realm/{cr_id}/auctions/index", ns)
    if ah_index and ah_index.get("auctions"):
        houses = [(h.get("id"),
                   (h.get("name") or ahdb.AUCTION_HOUSES.get(h.get("id"), "")).lower())
                  for h in ah_index["auctions"]]
    else:
        houses = [(None, "all")]
    if faction != "all":
        houses = [h for h in houses if faction in (h[1] or "")] or houses

    wrote = False
    for ah_id, house_faction in houses:
        path_suffix = f"/{ah_id}" if ah_id is not None else ""
        resp, data = bz.get(
            f"/data/wow/connected-realm/{cr_id}/auctions{path_suffix}", ns)
        if not data:
            print(f"  blizzard: no data for auction house {ah_id}", file=sys.stderr)
            continue

        # Last-Modified is an HTTP date; normalise to ISO so both sources sort
        # and parse identically.
        scan_time = to_iso(resp.headers.get("Last-Modified", "")) or utcnow()
        key = f"{realm}-{house_faction}"
        if already_have(root, "blizzard", key, scan_time):
            print(f"  blizzard/{house_faction}: already have {scan_time}",
                  file=sys.stderr)
            continue

        auctions = data.get("auctions", [])
        rows = aggregate(auctions)
        archive = ahdb.archive_path(root, "blizzard", key, scan_time)
        meta = {"source": "blizzard", "scan_time": scan_time,
                "collected_at": utcnow(), "region": region, "realm": key,
                "faction": house_faction, "namespace": ns,
                "connected_realm_id": cr_id, "auction_house_id": ah_id or "",
                "total_auctions": len(auctions), "item_count": len(rows)}
        size = ahdb.write_archive(archive, meta, rows)
        report(archive, size, len(rows), scan_time)
        wrote = True
    return wrote


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default="data", help="archive root (default: data)")
    ap.add_argument("--source", choices=["tsm", "blizzard", "both"], default="tsm")
    ap.add_argument("--game-type", default="classic")
    ap.add_argument("--region", default="us-fresh",
                    help="TSM region slug (us-fresh); Blizzard uses --blizz-region")
    ap.add_argument("--blizz-region", default="us")
    ap.add_argument("--realm", default="dreamscythe-horde", help="TSM realm slug")
    ap.add_argument("--blizz-realm", default="dreamscythe",
                    help="Blizzard realm slug (no faction suffix)")
    ap.add_argument("--faction", default="horde",
                    choices=["alliance", "horde", "neutral", "all"])
    ap.add_argument("--list-realms", action="store_true")
    args = ap.parse_args()

    if args.list_realms:
        bz = ahdb.Blizzard.from_env(args.blizz_region)
        for ns, slug, cr_id in sorted(ahdb.list_realms(bz)):
            print(f"{ns:28} {slug:28} connected_realm_id={cr_id}")
        return 0

    wrote = False
    if args.source in ("tsm", "both"):
        try:
            wrote |= collect_tsm(args.archive, args.game_type, args.region, args.realm)
        except Exception as exc:                      # noqa: BLE001
            # One source failing must never stop the other from collecting.
            print(f"  tsm: FAILED {exc}", file=sys.stderr)
            if args.source == "tsm":
                return 1
    if args.source in ("blizzard", "both"):
        try:
            wrote |= collect_blizzard(args.archive, args.blizz_region,
                                      args.blizz_realm, args.faction)
        except Exception as exc:                      # noqa: BLE001
            print(f"  blizzard: FAILED {exc}", file=sys.stderr)
            if args.source == "blizzard":
                return 1

    print("new data" if wrote else "nothing new", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
