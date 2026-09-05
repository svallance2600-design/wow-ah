# WoW auction price history — Dreamscythe-Horde

Collects **every item** the sources publish, on a schedule, in the cloud —
so it keeps running while your machine is off. You whitelist later, from data
you already have.

No source publishes auction *history*. TSM's CSVs, Blizzard's API, the in-game
addon: all of them are "right now" snapshots. So the history has to be
accumulated, and that is the whole job here.

## The one idea worth understanding

Collection and querying are separate, and that is deliberate.

```
ARCHIVE                              DATABASE
data/<source>/<month>/*.csv.gz  -->  auctions.db
append-only, immutable               derived, disposable, rebuilt on demand
every item, forever                  filtered however you like today
committed to git                     never committed
```

**Nothing filters on the way in.** The watchlist only affects what
`build_db.py` puts in the database, so changing your mind is a 20-second
rebuild against history you already collected, never a re-collection you can't
do. That is what "collect everything, whitelist later" needs to actually work.

## Files

| | |
|---|---|
| `collect.py` | fetch one scan, write one archive file |
| `build_db.py` | archive → SQLite, optionally filtered |
| `watch.py` | manage `watchlist.txt` (a view filter, never a collection filter) |
| `queries.sql` | starter SQL, including whitelist-candidate finders |
| `plot.py` | chart items over time |
| `compact.py` | roll a month of scans into one file |
| `ahdb.py` | archive format, schema, Blizzard client |

## Quick start

```bash
pip install -r requirements.txt

python collect.py                    # one scan into data/
python build_db.py                   # data/ -> auctions.db
sqlite3 auctions.db "SELECT COUNT(*) FROM prices"
```

Then once it has been running a few days:

```bash
python watch.py search "healing potion"
python watch.py add 13446 13444 8846
python build_db.py --watchlist --db watched.db
python plot.py --db watched.db --days 30
```

Query 7 in `queries.sql` ranks every item by how consistently it is posted and
how much supply it carries — that is the list to pick a whitelist from, rather
than guessing up front.

## The two sources

Run both. They answer different questions and write into the same tables with
a `source` column, so query 6 in `queries.sql` can compare them.

| | `tsm` | `blizzard` |
|---|---|---|
| Setup | none | free client id/secret |
| Refresh | every few hours | hourly |
| Gives | marketValue, minBuyout, recent, historical | min buyout, **supply**, posting counts |
| Item names | included | separate lookups |

TSM is a smoothed model; Blizzard is raw listings. Blizzard is the higher
resolution feed **and** the only one that tells you how much of a thing exists
— which on a fresh realm is often the more interesting variable than price.

Getting Blizzard credentials takes about two minutes at
<https://develop.battle.net/access/clients>: log in with your Battle.net
account, create a client, and copy the id and secret. Then:

```bash
export BLIZZARD_CLIENT_ID=...
export BLIZZARD_CLIENT_SECRET=...
python collect.py --source both
```

Without them, `--source both` quietly collects TSM only.

## Running it 24/7

`.github/workflows/collect.yml` runs hourly on GitHub Actions and commits new
scans. Push this folder to GitHub and it works immediately — the default
source needs no secrets.

- **Use a public repo.** Actions minutes are unlimited on public repos; a
  private one gets 2,000 minutes/month and hourly collection would eat most of
  it. There is nothing sensitive here — it is auction prices.
- Add `BLIZZARD_CLIENT_ID` / `BLIZZARD_CLIENT_SECRET` under Settings →
  Secrets and variables → Actions → **Secrets** to turn on the Blizzard feed.
- Override the realm with repo **Variables**: `GAME_TYPE`, `REGION`, `REALM`,
  `BLIZZ_REALM`, `FACTION`.
- Scheduled Actions get disabled after 60 days with no repo activity, but this
  workflow commits, which counts as activity.

Then to work with it locally: `git pull && python build_db.py --watchlist`.

## Storage, honestly

Measured at 15,000 items per scan (~243 KB per gzipped scan file):

| cadence | per day | per year |
|---|---|---|
| TSM alone (~6 new scans/day) | 1.5 MB | **0.54 GB** |
| Blizzard hourly (24/day) | 6.0 MB | **2.2 GB** |
| Both | 7.5 MB | **2.7 GB** |

TSM alone is fine forever. Both is fine for about a year, then wants attention
— GitHub starts warning past ~1 GB and pushes back near 5 GB.

Levers when it gets there, cheapest first:

1. `python compact.py --all --apply` — merges each past month into one file.
   The real win is file count (8,700 files/year → 12), not bytes: measured
   byte savings on worst-case data were ~1%, and it prints the true number for
   your data before changing anything.
2. Drop the Blizzard feed to every 3 hours — cuts its 2.2 GB to ~0.7 GB and
   you lose very little, since AH prices do not move meaningfully hourly.
3. Move old months to GitHub Releases as attachments, which do not count
   against repo size the way git history does.

None of this is urgent on day one. It is here so the decision is informed
rather than a surprise in eight months.

## Notes

- Prices are **copper** integers in the tables. The `prices` view adds `*_gold`
  columns and joins names — query the view.
- `taken_at` is the *upstream scan time*, not when the collector ran. Two
  collector runs that see the same scan produce one row, not two.
- Archive writes are byte-reproducible (gzip mtime pinned to 0), so an
  unchanged scan is a genuine no-op in git rather than a spurious diff.
- TSM's `historical` is TSM's own long-run average, not derived from your
  history. Your `taken_at` series is the real time axis.
- On thin markets `recent` and `marketValue` can diverge sharply. That is
  usually one person reposting, not a real move — check `quantity` from the
  Blizzard feed before believing a spike.
