#!/usr/bin/env python3
"""
auctionator.py - read Auctionator's price database out of SavedVariables.

Auctionator stores AUCTIONATOR_PRICE_DATABASE["<Realm> <Faction>"] as a
CBOR-encoded byte string (it uses LibCBOR-1.0). Per item key, per day, it keeps:

    l = lowest low price that day      (copper)
    h = highest low price that day     (copper)
    a = highest quantity seen that day (supply)
    m = last seen minimum price

Days are counted from Auctionator's SCAN_DAY_0 epoch.

Includes a minimal CBOR reader so this has no third-party dependencies.

  python auctionator.py "path/to/Auctionator.lua" --realm "Dreamscythe Horde"
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import struct
import sys

# Auctionator.Constants.SCAN_DAY_0 - verified against the addon source.
SCAN_DAY_0 = 1577836800  # 2020-01-01T00:00:00Z, from Auctionator Constants


# --------------------------------------------------------------------------
# Minimal CBOR (RFC 8949) reader - only the types LibCBOR emits.
# --------------------------------------------------------------------------

def _key(k):
    """LibCBOR emits map keys as byte strings; make them usable."""
    if isinstance(k, bytes):
        return k.decode("utf-8", "replace")
    if isinstance(k, (str, int)):
        return k
    return str(k)


class Cbor:
    def __init__(self, buf: bytes):
        self.b = buf
        self.i = 0

    def byte(self) -> int:
        v = self.b[self.i]
        self.i += 1
        return v

    def take(self, n: int) -> bytes:
        v = self.b[self.i:self.i + n]
        self.i += n
        return v

    def arg(self, ai: int):
        if ai < 24:
            return ai
        if ai == 24:
            return self.byte()
        if ai == 25:
            return struct.unpack(">H", self.take(2))[0]
        if ai == 26:
            return struct.unpack(">I", self.take(4))[0]
        if ai == 27:
            return struct.unpack(">Q", self.take(8))[0]
        if ai == 31:
            return None          # indefinite length
        raise ValueError(f"bad additional info {ai} at {self.i}")

    def load(self):
        ib = self.byte()
        major, ai = ib >> 5, ib & 0x1F

        if major == 0:
            return self.arg(ai)
        if major == 1:
            return -1 - self.arg(ai)
        if major in (2, 3):
            n = self.arg(ai)
            if n is None:                      # indefinite chunks
                parts = []
                while self.b[self.i] != 0xFF:
                    parts.append(self.load())
                self.i += 1
                if major == 3:
                    return "".join(parts)
                return b"".join(parts)
            raw = self.take(n)
            return raw.decode("utf-8", "replace") if major == 3 else raw
        if major == 4:
            n = self.arg(ai)
            if n is None:
                out = []
                while self.b[self.i] != 0xFF:
                    out.append(self.load())
                self.i += 1
                return out
            return [self.load() for _ in range(n)]
        if major == 5:
            n = self.arg(ai)
            out = {}
            if n is None:
                while self.b[self.i] != 0xFF:
                    k = self.load()
                    out[_key(k)] = self.load()
                self.i += 1
                return out
            for _ in range(n):
                k = self.load()
                out[_key(k)] = self.load()
            return out
        if major == 6:
            self.arg(ai)                       # tag: ignore, decode content
            return self.load()
        if major == 7:
            if ai == 20: return False
            if ai == 21: return True
            if ai == 22: return None
            if ai == 23: return None
            if ai == 25:
                return struct.unpack(">e", self.take(2))[0]
            if ai == 26:
                return struct.unpack(">f", self.take(4))[0]
            if ai == 27:
                return struct.unpack(">d", self.take(8))[0]
            if ai == 31: return "__break__"
            return None
        raise ValueError(f"bad major type {major}")


# --------------------------------------------------------------------------
# Lua string literal -> bytes
# --------------------------------------------------------------------------

LUA_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
               "\\": 92, '"': 34, "'": 39, "\n": 10}


def lua_unescape(s: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c != 0x5C:                     # not a backslash
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        nxt = chr(s[i])
        if nxt.isdigit():                 # \ddd decimal, up to 3 digits
            digits = ""
            while i < len(s) and chr(s[i]).isdigit() and len(digits) < 3:
                digits += chr(s[i])
                i += 1
            out.append(int(digits) & 0xFF)
        elif nxt in LUA_ESCAPES:
            out.append(LUA_ESCAPES[nxt])
            i += 1
        elif nxt == "x":                  # \xHH
            out.append(int(s[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(s[i])
            i += 1
    return bytes(out)


def extract(path: str, realm: str | None):
    raw = open(path, "rb").read()
    idx = raw.find(b"AUCTIONATOR_PRICE_DATABASE")
    if idx < 0:
        sys.exit("AUCTIONATOR_PRICE_DATABASE not found in that file")
    out = {}
    # entries look like:  ["Dreamscythe Horde"] = "....",
    for m in re.finditer(rb'\["([^"]+)"\]\s*=\s*"', raw[idx:idx + 200_000_000]):
        key = m.group(1).decode("utf-8", "replace")
        start = idx + m.end()
        # walk to the closing quote, honouring backslash escapes
        j = start
        while j < len(raw):
            if raw[j] == 0x5C:
                j += 2
                continue
            if raw[j] == 0x22:
                break
            j += 1
        if realm and key != realm:
            continue
        out[key] = lua_unescape(raw[start:j])
        if raw[j:j + 200].find(b"AUCTIONATOR_") >= 0:
            break
    return out


def day_to_date(day: int) -> dt.date:
    return (dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
            + dt.timedelta(seconds=SCAN_DAY_0 + day * 86400)).date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--realm")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    blobs = extract(args.path, args.realm)
    for key, blob in blobs.items():
        print(f"\n=== {key}: {len(blob):,} bytes ===")
        try:
            data = Cbor(blob).load()
        except Exception as exc:                       # noqa: BLE001
            print(f"  CBOR decode failed: {exc}")
            print(f"  first bytes: {blob[:32].hex(' ')}")
            continue
        if not isinstance(data, dict):
            print(f"  decoded to {type(data).__name__}, not a map")
            continue
        print(f"  items: {len(data):,}")
        days = set()
        for v in data.values():
            if isinstance(v, dict):
                days.update(int(d) for d in (v.get("h") or {}))
        if days:
            print(f"  day range: {day_to_date(min(days))} .. {day_to_date(max(days))} "
                  f"({len(days)} distinct days)")
        shown = 0
        for k, v in data.items():
            if not isinstance(v, dict) or not v.get("h"):
                continue
            print(f"  {k}: m={v.get('m')} h={dict(list((v.get('h') or {}).items())[:3])} "
                  f"a={dict(list((v.get('a') or {}).items())[:3])}")
            shown += 1
            if shown >= args.limit:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
