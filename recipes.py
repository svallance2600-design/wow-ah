#!/usr/bin/env python3
"""
recipes.py - read the TBC recipe database out of the ProfessionMaster addon.

ProfessionMaster ships a full skill table in
  Interface/AddOns/ProfessionMaster/models/skills/bcc.lua
shaped as:

    [28558] = {                                   -- spell id
        ["p"] = 171,                              -- profession id
        ["itemId"] = 22835,                       -- what it makes
        ["itemAmount"] = 2,                       -- how many (default 1)
        ["reagents"] = {[22790] = 1, [3371] = 1}, -- itemId -> quantity
        ["d"] = {350, 365, 372, 380},             -- skill colours
        ["r"] = 22910                             -- the recipe item, if any
    },

That is everything needed to price a craft: product, yield, and inputs. It is
read-only reference data shipped with the addon, so parsing it needs no game
client and never changes under us.

  python recipes.py <path to bcc.lua> --profession alchemy
"""
from __future__ import annotations

import argparse
import re

PROFESSIONS = {
    164: "Blacksmithing", 165: "Leatherworking", 171: "Alchemy",
    182: "Herbalism", 185: "Cooking", 186: "Mining", 197: "Tailoring",
    202: "Engineering", 333: "Enchanting", 393: "Skinning", 755: "Jewelcrafting",
    773: "Inscription", 129: "First Aid",
}
BY_NAME = {v.lower(): k for k, v in PROFESSIONS.items()}

ENTRY = re.compile(r"\[(\d+)\]\s*=\s*\{(.*?)\n    \}", re.S)
FIELD = re.compile(r'\["(\w+)"\]\s*=\s*(\{[^}]*\}|\d+)')
PAIR = re.compile(r"\[(\d+)\]\s*=\s*(\d+)")


def parse(path: str) -> dict[int, dict]:
    """spell id -> {profession_id, item_id, amount, reagents{item_id: qty}}"""
    text = open(path, encoding="utf-8", errors="replace").read()
    out: dict[int, dict] = {}
    for m in ENTRY.finditer(text):
        spell, body = int(m.group(1)), m.group(2)
        rec: dict = {}
        for f in FIELD.finditer(body):
            key, raw = f.group(1), f.group(2)
            if raw.startswith("{"):
                if key == "reagents":
                    rec["reagents"] = {int(a): int(b) for a, b in PAIR.findall(raw)}
            else:
                rec[key] = int(raw)
        if "itemId" not in rec or not rec.get("reagents"):
            continue
        # itemId 0 means the craft has no item output (enchants applied directly)
        if rec["itemId"] == 0:
            continue
        out[spell] = {
            "profession_id": rec.get("p"),
            "profession": PROFESSIONS.get(rec.get("p"), str(rec.get("p"))),
            "item_id": rec["itemId"],
            "amount": rec.get("itemAmount", 1),
            "reagents": rec["reagents"],
            "recipe_item_id": rec.get("r"),
        }
    return out


def margins(recipes: dict[int, dict], price, names=None, profession=None):
    """Cost each craft from a price lookup: price(item_id) -> gold or None.

    Returns rows sorted by margin. Crafts with any unpriceable reagent are
    skipped rather than costed as free - a missing price is not a zero.
    """
    rows = []
    for spell, r in recipes.items():
        if profession and r["profession_id"] != profession:
            continue
        out_price = price(r["item_id"])
        if not out_price:
            continue
        cost = 0.0
        missing = []
        for item_id, qty in r["reagents"].items():
            p = price(item_id)
            if p is None:
                missing.append(item_id)
                break
            cost += p * qty
        if missing:
            continue
        revenue = out_price * r["amount"]
        rows.append({
            "spell": spell,
            "profession": r["profession"],
            "product": (names or {}).get(r["item_id"], str(r["item_id"])),
            "product_id": r["item_id"],
            "amount": r["amount"],
            "revenue": revenue,
            "cost": cost,
            "margin": revenue - cost,
            "margin_pct": (revenue - cost) / cost * 100 if cost else None,
            "reagents": {(names or {}).get(i, str(i)): q
                         for i, q in r["reagents"].items()},
        })
    rows.sort(key=lambda d: -d["margin"])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="path to ProfessionMaster models/skills/bcc.lua")
    ap.add_argument("--profession")
    args = ap.parse_args()
    recs = parse(args.path)
    print(f"{len(recs):,} craftable recipes parsed")
    import collections
    for pid, n in collections.Counter(r["profession"] for r in recs.values()).most_common():
        print(f"  {pid:16} {n:5}")
    if args.profession:
        pid = BY_NAME.get(args.profession.lower())
        for spell, r in list(recs.items())[:200]:
            if r["profession_id"] == pid:
                print(f"  {spell}: item {r['item_id']} x{r['amount']} "
                      f"<- {r['reagents']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
