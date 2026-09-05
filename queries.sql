-- Starter queries.  sqlite3 auctions.db < queries.sql
--
-- Prices are COPPER in the tables; the `prices` view adds *_gold columns and
-- joins item names, so query the view unless you need something it drops.
-- `taken_at` is the UPSTREAM scan time, not when the collector ran.

-- ---------------------------------------------------------------
-- 1. Coverage: what history do I actually have?
-- ---------------------------------------------------------------
SELECT source,
       COUNT(*)                        AS scans,
       MIN(scan_time)                  AS first_scan,
       MAX(scan_time)                  AS last_scan,
       COUNT(DISTINCT DATE(scan_time)) AS days
FROM snapshots
GROUP BY source;


-- ---------------------------------------------------------------
-- 2. Time series for one item. This is the thing you plot.
-- ---------------------------------------------------------------
SELECT taken_at, market_value_gold, min_buyout_gold, quantity
FROM prices
WHERE item_id = 13446
ORDER BY taken_at;


-- ---------------------------------------------------------------
-- 3. Daily high/low/average per watched item.
-- ---------------------------------------------------------------
SELECT obs_date AS day,
       item_name,
       ROUND(MIN(min_buyout_gold), 2)   AS low,
       ROUND(MAX(min_buyout_gold), 2)   AS high,
       ROUND(AVG(market_value_gold), 2) AS avg_market
FROM prices
WHERE item_id IN (SELECT item_id FROM watchlist)
GROUP BY day, item_key
ORDER BY day DESC, item_name;


-- ---------------------------------------------------------------
-- 4. Biggest movers, last 24h vs the 24h before.
--    Requires >= 2 days of history before it returns anything useful.
-- ---------------------------------------------------------------
WITH bounds AS (SELECT MAX(scan_time) AS latest FROM snapshots),
windows AS (
  SELECT item_id, item_name,
         AVG(CASE WHEN taken_at >= DATETIME(latest, '-1 day')
                  THEN market_value_gold END) AS now_price,
         AVG(CASE WHEN taken_at <  DATETIME(latest, '-1 day')
                  AND taken_at >= DATETIME(latest, '-2 day')
                  THEN market_value_gold END) AS prev_price,
         COUNT(*) AS samples
  FROM prices, bounds
  GROUP BY item_id
)
SELECT item_name,
       ROUND(prev_price, 2) AS was,
       ROUND(now_price, 2)  AS now,
       ROUND(100.0 * (now_price - prev_price) / prev_price, 1) AS pct_change
FROM windows
WHERE prev_price > 0.5 AND now_price IS NOT NULL AND samples >= 4
ORDER BY ABS(100.0 * (now_price - prev_price) / prev_price) DESC
LIMIT 25;


-- ---------------------------------------------------------------
-- 5. Best hour of day to buy.
--    Scan times are UTC; Dreamscythe server time is US Central (UTC-5).
-- ---------------------------------------------------------------
SELECT item_name,
       (CAST(STRFTIME('%H', taken_at) AS INTEGER) + 19) % 24 AS server_hour,
       ROUND(AVG(min_buyout_gold), 2) AS avg_min_buyout,
       COUNT(*) AS samples
FROM prices
WHERE item_id IN (SELECT item_id FROM watchlist)
  AND source <> 'auctionator'   -- daily buckets have no hour of day
GROUP BY item_id, server_hour
HAVING samples >= 2
ORDER BY item_name, avg_min_buyout;


-- ---------------------------------------------------------------
-- 6. Where the two sources disagree. Blizzard is raw listings, TSM is a
--    smoothed model, so a large gap usually means a thin, gappy market.
--    (Needs both sources collected.)
-- ---------------------------------------------------------------
SELECT b.item_name,
       ROUND(AVG(b.min_buyout_gold), 2) AS blizzard_min,
       ROUND(AVG(t.min_buyout_gold), 2) AS tsm_min,
       ROUND(AVG(b.quantity))           AS avg_supply
FROM prices b
JOIN prices t ON t.item_id = b.item_id
             AND t.source = 'tsm'
             AND DATE(t.taken_at) = DATE(b.taken_at)
WHERE b.source = 'blizzard'
GROUP BY b.item_id
HAVING avg_supply > 20
ORDER BY ABS(blizzard_min - tsm_min) DESC
LIMIT 25;


-- ---------------------------------------------------------------
-- 7. Whitelist candidates: what is actually traded here?
--    Ranked by how consistently an item is posted AND how much of it there is.
--    Run this after a few days, then feed the ids to `watch.py add`.
-- ---------------------------------------------------------------
SELECT item_id,
       item_name,
       COUNT(*)                         AS times_seen,
       ROUND(AVG(COALESCE(quantity, 0))) AS avg_supply,
       ROUND(AVG(market_value_gold), 2) AS avg_price,
       ROUND(100.0 * (MAX(market_value_gold) - MIN(market_value_gold))
             / NULLIF(MIN(market_value_gold), 0), 0) AS pct_range
FROM prices
GROUP BY item_id
HAVING times_seen >= (SELECT COUNT(*) * 0.8 FROM snapshots)
ORDER BY avg_supply DESC, pct_range DESC
LIMIT 50;


-- ---------------------------------------------------------------
-- 8. Volatility: which items are worth watching because they move?
-- ---------------------------------------------------------------
SELECT item_name,
       COUNT(*) AS samples,
       ROUND(AVG(market_value_gold), 2) AS avg_price,
       ROUND(MIN(market_value_gold), 2) AS cheapest,
       ROUND(MAX(market_value_gold), 2) AS dearest,
       ROUND(100.0 * (MAX(market_value_gold) - MIN(market_value_gold))
             / NULLIF(AVG(market_value_gold), 0), 0) AS swing_pct
FROM prices
WHERE market_value_gold > 1
GROUP BY item_id
HAVING samples >= 8
ORDER BY swing_pct DESC
LIMIT 30;


-- ---------------------------------------------------------------
-- 9. Supply vs price, from your own Auctionator scans.
--    The one question the cloud feed cannot answer: how much of a
--    thing actually exists, and is it getting scarcer?
-- ---------------------------------------------------------------
SELECT item_name,
       obs_date,
       quantity                        AS units_posted,
       ROUND(day_low_gold, 2)          AS cheapest,
       ROUND(day_high_gold, 2)         AS dearest
FROM prices
WHERE source = 'auctionator' AND quantity IS NOT NULL
ORDER BY obs_date DESC, quantity DESC
LIMIT 40;


-- ---------------------------------------------------------------
-- 10. Thin markets: high value, low supply. Where one seller sets
--     the price, and where a spike is noise rather than signal.
-- ---------------------------------------------------------------
SELECT item_name,
       quantity                   AS units_posted,
       ROUND(day_low_gold, 2)     AS price,
       ROUND(day_low_gold * quantity, 0) AS market_depth_gold
FROM prices
WHERE source = 'auctionator'
  AND obs_date = (SELECT MAX(obs_date) FROM prices WHERE source = 'auctionator')
  AND day_low_gold > 1
  AND quantity BETWEEN 1 AND 10
ORDER BY day_low_gold DESC
LIMIT 30;
