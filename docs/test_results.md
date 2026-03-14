# Performance Test Results
**Thesis: Design and Implementation of a Real-Time ELT Pipeline Using Microsoft Fabric**
**Student:** Tan Phuc Duong | **Date:** _(fill in after testing)_

---

## Test Environment

| Component | Configuration |
|-----------|--------------|
| Generator | Azure Container Instances — 1 vCPU, 1.5 GB |
| Event Hub | Standard tier, 8 partitions, 2 TUs |
| Fabric capacity | F___ (fill in) |
| KQL Database | CryptoPriceDB |
| Data source | Coinbase WebSocket + CoinGecko REST (merged) |
| Symbols | BTC-USD, ETH-USD, SOL-USD, BNB-USD, XRP-USD |
| Test duration | 5 minutes per scenario (300s) |

---

## Throughput Results

| Test | Target EPS | Actual EPS | Batch send (ms) | Errors | Success rate |
|------|-----------|------------|-----------------|--------|-------------|
| Baseline | 50 | | | | |
| Normal | 200 | | | | |
| High | 500 | | | | |
| Peak | 1000 | | | | |

*EPS = events per second. Actual EPS measured by `load_test.py`. Results saved in `scripts/results/`.*

---

## End-to-End Latency Results

Latency = `ingestion_time (Fabric KQL) − timestamp_utc (Coinbase source)`. Read from KQL using `GetLatencyStats(5m)` after each test.

| Test | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) | Event count |
|------|----------|----------|----------|----------|-------------|
| Baseline (50 eps) | | | | | |
| Normal (200 eps) | | | | | |
| High (500 eps) | | | | | |
| Peak (1000 eps) | | | | | |

---

## Burst Test Results

A burst test sends 10x normal load for 60 seconds, then returns to baseline.

```kql
-- Run after burst test to measure recovery
CleanPrices
| where timestamp_utc > ago(30m)
| summarize events = count(), avg_latency = avg(toreal(latency_ms))
  by bin(timestamp_utc, 1m)
| order by timestamp_utc asc
```

| Metric | Value |
|--------|-------|
| Time to first latency spike | |
| Peak latency during burst | |
| Time to recover to baseline latency | |
| Events dropped (gap in sequence) | |

---

## Data Quality Results

```kql
-- Invalid event rate
RawPrices
| join kind=leftouter CleanPrices on event_id
| summarize
    total_raw = count(),
    passed_silver = countif(isnotnull(CleanPrices_event_id)),
    rejected = countif(isnull(CleanPrices_event_id))
| extend rejection_rate_pct = round(toreal(rejected) / toreal(total_raw) * 100, 2)
```

| Metric | Value |
|--------|-------|
| Total events ingested (Bronze) | |
| Events passed Silver validation | |
| Events rejected (invalid) | |
| Rejection rate % | |
| Duplicate event_ids detected | |

---

## Anomaly Detection Results

```kql
DetectPriceSpikes(2.0, 60s)   -- spikes > 2% in 60 seconds
GetVolatility(1h)              -- volatility ranking
AlertSummary | order by window_5m desc | take 20
```

| Metric | Value |
|--------|-------|
| Price spikes detected (>2%, 60s window) | |
| Most volatile symbol | |
| Highest single spike % | |
| Volume surges detected (>3x baseline) | |

---

## Observations and Notes

_(Fill in after testing — record anything unexpected, e.g. latency spikes, Event Hub throttling, Fabric materialized view backfill lag)_

-
-
-

---

## KQL Queries Used for Measurement

```kql
-- Throughput per minute
CleanPrices
| where timestamp_utc > ago(30m)
| summarize events_per_min = count() by bin(timestamp_utc, 1m)
| order by timestamp_utc asc

-- Latency percentiles
GetLatencyStats(10m)

-- Sequence gap check (data loss)
RawPrices
| where source == "coinbase_ws"
| order by sequence asc
| extend expected = prev(sequence) + 1
| where sequence != expected and isnotnull(prev(sequence))
| project symbol, sequence, expected, gap = sequence - expected

-- Materialized view freshness
.show materialized-view PriceAggregates
```
