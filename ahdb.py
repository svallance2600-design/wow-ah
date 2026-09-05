"""
ahdb.py - shared bits: archive layout, Blizzard API client, SQLite schema.

Storage model
-------------
Collection and querying are deliberately separate.

  ARCHIVE   data/<source>/<YYYY-MM>/<source>-<scantime>.csv.gz
            Append-only, immutable, one small file per scan. Nothing is ever
            rewritten, so git stores each new scan as a new small blob rather
            than re-storing a growing binary every hour.

  DATABASE  auctions.db, built FROM the archive by build_db.py.
            Derived, disposable, never committed. Rebuild it filtered to a
            watchlist, or unfiltered, as often as you like.

That split is what lets you "collect everything now, decide what matters
later" without ever having to re-collect.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import re
import sqlite3
import time
from dataclasses import dataclass

import requests

OAUTH_URL = "https://oauth.battle.net/token"

NAMESPACE_CANDIDATES = [
    "dynamic-classicann-{region}",  # Anniversary / "Fresh" realms
    "dynamic-classic-{region}",     # Classic progression line
    "dynamic-classic1x-{region}",   # Classic Era / HC / SoD (older naming)
]

STATIC_CANDIDATES = [
    "static-classicann-{region}",
    "static-classic-{region}",
    "static-classic1x-{region}",
    "static-{region}",
]

AUCTION_HOUSES = {1: "alliance", 2: "alliance", 6: "horde", 7: "neutral"}

# TSM public data. Slugs are exactly the ones in your realm's URL on
# tradeskillmaster.com:
#   https://tradeskillmaster.com/classic/us-fresh/dreamscythe-horde
TSM_BASE = "https://public-data.tradeskillmaster.com"

ARCHIVE_COLUMNS = [
    "item_id", "min_buyout", "market_value", "mean_buyout",
    "recent", "historical", "quantity", "num_auctions",
]


def tsm_url(game_type: str, region: str, realm: str | None, kind: str = "items") -> str:
    if realm:
        return f"{TSM_BASE}/{game_type}/{region}/realm/{realm}/{kind}.csv"
    return f"{TSM_BASE}/{game_type}/{region}/region/{kind}.csv"


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", text or "unknown")


def archive_path(root: str, source: str, realm: str, scan_time: str) -> str:
    month = (scan_time or "0000-00")[:7]
    name = f"{safe(source)}-{safe(realm)}-{safe(scan_time)}.csv.gz"
    return os.path.join(root, safe(source), month, name)


def write_archive(path: str, meta: dict, rows: list[dict]) -> int:
    """Write one immutable scan file. Metadata rides in a header comment so the
    file is self-describing and needs no sidecar."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf = io.StringIO()
    for key in sorted(meta):
        buf.write(f"# {key}={meta[key]}\n")
    writer = csv.DictWriter(buf, fieldnames=ARCHIVE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    payload = buf.getvalue().encode()
    tmp = path + ".tmp"
    # mtime=0 so an identical scan compresses to identical bytes - git then
    # sees no change at all rather than a spurious one.
    with open(tmp, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(payload)
    os.replace(tmp, path)
    return os.path.getsize(path)


def read_archive(path: str) -> tuple[dict, list[dict]]:
    meta: dict[str, str] = {}
    lines: list[str] = []
    with gzip.open(path, "rt", newline="") as fh:
        for line in fh:
            if line.startswith("#"):
                key, _, value = line[1:].strip().partition("=")
                meta[key.strip()] = value.strip()
            else:
                lines.append(line)
    return meta, list(csv.DictReader(io.StringIO("".join(lines))))


def iter_archive(root: str, source: str | None = None):
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            if not name.endswith(".csv.gz"):
                continue
            if source and not name.startswith(source + "-"):
                continue
            yield os.path.join(dirpath, name)


def load_watchlist(path: str = "watchlist.txt") -> list[int]:
    """One item id per line. Anything after # is a comment, so you can label them."""
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        token = line.split("#")[0].strip()
        if token.isdigit():
            out.append(int(token))
    return out


# ---------------------------------------------------------------------------
# Query database (derived from the archive)
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT    NOT NULL,   -- 'tsm' or 'blizzard'
    scan_time         TEXT    NOT NULL,   -- upstream scan time: the real clock
    collected_at      TEXT,               -- when we fetched it
    region            TEXT,
    realm_slug        TEXT,
    faction           TEXT,
    item_count        INTEGER,
    total_auctions    INTEGER,
    UNIQUE (source, realm_slug, scan_time)
);

-- Which columns are populated depends on the source:
--   tsm      -> min_buyout, market_value, recent, historical
--   blizzard -> min_buyout, market_value, mean_buyout, quantity, num_auctions
CREATE TABLE IF NOT EXISTS item_prices (
    snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL,
    min_buyout    INTEGER,
    market_value  INTEGER,
    mean_buyout   INTEGER,
    recent        INTEGER,
    historical    INTEGER,
    quantity      INTEGER,
    num_auctions  INTEGER,
    PRIMARY KEY (snapshot_id, item_id)
);

CREATE TABLE IF NOT EXISTS items (
    item_id     INTEGER PRIMARY KEY,
    name        TEXT,
    quality     TEXT,
    item_class  TEXT,
    item_subclass TEXT,
    level       INTEGER
);

CREATE TABLE IF NOT EXISTS watchlist (item_id INTEGER PRIMARY KEY);

CREATE INDEX IF NOT EXISTS idx_prices_item ON item_prices(item_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(scan_time);

-- The view you actually query. Prices are copper in the tables; this adds gold.
CREATE VIEW IF NOT EXISTS prices AS
SELECT
    s.scan_time                  AS taken_at,
    s.source,
    s.realm_slug,
    s.faction,
    p.item_id,
    COALESCE(i.name, 'item:' || p.item_id) AS item_name,
    p.min_buyout                 AS min_buyout_copper,
    p.market_value               AS market_value_copper,
    p.min_buyout   / 10000.0     AS min_buyout_gold,
    p.market_value / 10000.0     AS market_value_gold,
    p.mean_buyout  / 10000.0     AS mean_buyout_gold,
    p.recent       / 10000.0     AS recent_gold,
    p.historical   / 10000.0     AS historical_gold,
    p.quantity,
    p.num_auctions,
    s.snapshot_id
FROM item_prices p
JOIN snapshots s USING (snapshot_id)
LEFT JOIN items i USING (item_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Blizzard Game Data API
# ---------------------------------------------------------------------------

@dataclass
class Blizzard:
    client_id: str
    client_secret: str
    region: str = "us"
    _token: str | None = None
    _expires: float = 0.0

    @classmethod
    def from_env(cls, region: str = "us") -> "Blizzard":
        cid = os.environ.get("BLIZZARD_CLIENT_ID")
        secret = os.environ.get("BLIZZARD_CLIENT_SECRET")
        if not cid or not secret:
            raise SystemExit(
                "Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET.\n"
                "Free, instant: https://develop.battle.net/access/clients")
        return cls(cid, secret, region)

    @property
    def host(self) -> str:
        return f"https://{self.region}.api.blizzard.com"

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = requests.post(OAUTH_URL, data={"grant_type": "client_credentials"},
                          auth=(self.client_id, self.client_secret), timeout=30)
        r.raise_for_status()
        payload = r.json()
        self._token = payload["access_token"]
        self._expires = time.time() + payload.get("expires_in", 3600)
        return self._token

    def get(self, path: str, namespace: str, **params):
        params = {"namespace": namespace, "locale": "en_US", **params}
        for attempt in range(4):
            r = requests.get(self.host + path, params=params,
                             headers={"Authorization": f"Bearer {self.token()}",
                                      "Accept-Encoding": "gzip"},
                             timeout=300)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return r, None
            r.raise_for_status()
            return r, r.json()
        r.raise_for_status()
        return r, None


def resolve_realm(bz: Blizzard, realm_slug: str) -> tuple[str, int]:
    tried = []
    for template in NAMESPACE_CANDIDATES:
        ns = template.format(region=bz.region)
        tried.append(ns)
        _, index = bz.get("/data/wow/connected-realm/index", ns)
        if not index:
            continue
        for entry in index.get("connected_realms", []):
            cr_id = int(entry["href"].rstrip("/").split("/")[-1].split("?")[0])
            _, detail = bz.get(f"/data/wow/connected-realm/{cr_id}", ns)
            if not detail:
                continue
            for realm in detail.get("realms", []):
                if realm.get("slug") == realm_slug:
                    return ns, cr_id
    raise SystemExit(f"Realm '{realm_slug}' not found in: {', '.join(tried)}. "
                     "Try `python collect.py --list-realms`.")


def list_realms(bz: Blizzard) -> list[tuple[str, str, int]]:
    out = []
    for template in NAMESPACE_CANDIDATES:
        ns = template.format(region=bz.region)
        _, index = bz.get("/data/wow/connected-realm/index", ns)
        if not index:
            continue
        for entry in index.get("connected_realms", []):
            cr_id = int(entry["href"].rstrip("/").split("/")[-1].split("?")[0])
            _, detail = bz.get(f"/data/wow/connected-realm/{cr_id}", ns)
            if not detail:
                continue
            for realm in detail.get("realms", []):
                out.append((ns, realm.get("slug", "?"), cr_id))
    return out


def fetch_item(bz: Blizzard, item_id: int) -> dict | None:
    for template in STATIC_CANDIDATES:
        _, data = bz.get(f"/data/wow/item/{item_id}", template.format(region=bz.region))
        if data:
            return data
    return None
