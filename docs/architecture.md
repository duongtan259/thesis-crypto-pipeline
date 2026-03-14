# Architecture & Implementation Design
**Thesis: Design and Implementation of a Real-Time ELT Pipeline Using Microsoft Fabric**
**Student:** Tan Phuc Duong | **Program:** Master's in Data Analytics

---

## 1. System Overview

### Data Source
Real-time cryptocurrency price data from public APIs (CoinGecko / Coinbase).

**Why crypto data:**
- Continuous 24/7 streaming with no special permissions or infrastructure required
- High volume: hundreds of events per second across hundreds of trading pairs
- Simple, well-defined schema (timestamp, symbol, price, volume)
- Real business value — financial data monitoring is a canonical enterprise streaming use case

**API details:**
| Provider | Endpoint | Rate Limit | Protocol |
|----------|----------|------------|----------|
| CoinGecko | `/coins/markets` | 30 req/min (free) | REST polling |
| Coinbase Advanced Trade | `wss://advanced-trade-ws.coinbase.com` | Unlimited | WebSocket |

The data generator will connect to the Coinbase WebSocket feed to receive true push-based streaming (no polling latency), falling back to CoinGecko REST polling for additional pairs.

---

## 2. Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA SOURCE                                                         │
│  Coinbase WebSocket / CoinGecko REST API                            │
│  (BTC, ETH, SOL, ... — hundreds of pairs)                           │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ JSON events
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DATA GENERATOR  (Python)                                            │
│  • Connects to WebSocket feed                                        │
│  • Normalises payload to canonical schema                            │
│  • Publishes to Azure Event Hub via AMQP                             │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ AMQP / Kafka protocol
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  AZURE EVENT HUB                                                     │
│  • Namespace: thesis-crypto-eh-ns                                    │
│  • Event Hub: crypto-prices                                          │
│  • Partitions: 8 (for throughput testing)                            │
│  • Retention: 1 day                                                  │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ Eventstream connector
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  MICROSOFT FABRIC — Real-Time Intelligence                           │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  EVENTSTREAM  (crypto-price-stream)                          │   │
│  │  • Ingests from Event Hub                                    │   │
│  │  • Routes to KQL Database destination                        │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                             │                                        │
│                             ▼                                        │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  KQL DATABASE  (CryptoPriceDB)                               │   │
│  │                                                              │   │
│  │  BRONZE  RawPrices                                           │   │
│  │  ├── Raw ingested records (append-only)                      │   │
│  │  ├── All fields preserved including raw JSON                 │   │
│  │  └── Ingestion metadata (timestamp, partition, offset)       │   │
│  │                                                              │   │
│  │  SILVER  CleanPrices  [update policy on RawPrices]           │   │
│  │  ├── Validated and typed records                             │   │
│  │  ├── Nulls / outliers filtered                               │   │
│  │  └── Enriched with derived fields (price_change_pct, etc.)  │   │
│  │                                                              │   │
│  │  GOLD  PriceAggregates  [materialized view on CleanPrices]   │   │
│  │  ├── 1-min OHLCV candles per symbol                          │   │
│  │  ├── Rolling 5-min / 1-hr volume-weighted average price      │   │
│  │  └── Alert triggers (price spike > X%)                       │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                             │                                        │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  REAL-TIME DASHBOARD  (Fabric RTI Dashboard + Power BI)      │   │
│  │  • Live price tiles (auto-refresh)                           │   │
│  │  • OHLCV candlestick charts                                  │   │
│  │  • Volume heatmap                                            │   │
│  │  • Latency monitoring panel                                  │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Schema

### Canonical Event (published to Event Hub)

```json
{
  "event_id":       "uuid-v4",
  "symbol":         "BTC-USD",
  "price":          68432.15,
  "volume_24h":     29847234.50,
  "market_cap":     1348293847234.00,
  "timestamp_utc":  "2026-01-15T10:23:45.123Z",
  "source":         "coinbase_ws",
  "sequence":       8472934
}
```

### Bronze Table — `RawPrices`

| Column | Type | Description |
|--------|------|-------------|
| `event_id` | string | UUID from producer |
| `symbol` | string | Trading pair (e.g. BTC-USD) |
| `price` | real | Trade price |
| `volume_24h` | real | 24-hour rolling volume |
| `market_cap` | real | Market capitalisation |
| `timestamp_utc` | datetime | Event timestamp from source |
| `source` | string | API source identifier |
| `sequence` | long | Source sequence number |
| `ingestion_time` | datetime | Fabric ingestion timestamp |
| `raw_payload` | dynamic | Full original JSON |

### Silver Table — `CleanPrices` (via update policy)

Adds derived fields, removes invalid rows:

| Column | Type | Description |
|--------|------|-------------|
| *(all Bronze columns except raw_payload)* | | |
| `price_usd` | real | Normalised price in USD |
| `is_valid` | bool | Passed validation rules |
| `latency_ms` | long | `ingestion_time - timestamp_utc` in ms |

### Gold Table — `PriceAggregates` (materialized view)

| Column | Type | Description |
|--------|------|-------------|
| `symbol` | string | Trading pair |
| `window_start` | datetime | 1-minute bucket start |
| `open` | real | First price in window |
| `high` | real | Max price in window |
| `low` | real | Min price in window |
| `close` | real | Last price in window |
| `volume` | real | Total volume in window |
| `event_count` | long | Number of events in window |
| `avg_latency_ms` | real | Average end-to-end latency |

---

## 4. Azure Resources

| Resource | Name | SKU / Config |
|----------|------|-------------|
| Resource Group | `rg-thesis-fabric` | — |
| Event Hub Namespace | `thesis-crypto-eh-ns` | Standard, 8 TUs |
| Event Hub | `crypto-prices` | 8 partitions, 1-day retention |
| (Optional) Azure SQL | n/a | Not required — using public API |

---

## 5. Fabric Workspace Resources

| Resource | Name | Type |
|----------|------|------|
| Workspace | `Thesis-RTI` | Fabric workspace |
| Eventstream | `crypto-price-stream` | Eventstream |
| KQL Database | `CryptoPriceDB` | KQL Database |
| Dashboard | `Crypto Live Dashboard` | RTI Dashboard |
| Power BI report | `Crypto Analytics` | Power BI |

---

## 6. KQL Implementation

### Bronze — Raw ingestion table
```kql
.create table RawPrices (
    event_id: string,
    symbol: string,
    price: real,
    volume_24h: real,
    market_cap: real,
    timestamp_utc: datetime,
    source: string,
    sequence: long,
    ingestion_time: datetime,
    raw_payload: dynamic
)
```

### Silver — Update policy function
```kql
.create function SilverTransform() {
    RawPrices
    | where isnotnull(price) and price > 0
    | where isnotnull(symbol) and symbol != ""
    | extend
        price_usd = price,
        is_valid = (price > 0 and isnotnull(volume_24h)),
        latency_ms = datetime_diff('millisecond', ingestion_time, timestamp_utc)
    | project-away raw_payload
}

.create table CleanPrices (
    event_id: string,
    symbol: string,
    price: real,
    volume_24h: real,
    market_cap: real,
    timestamp_utc: datetime,
    source: string,
    sequence: long,
    ingestion_time: datetime,
    price_usd: real,
    is_valid: bool,
    latency_ms: long
)

.alter table CleanPrices policy update
@'[{"IsEnabled": true, "Source": "RawPrices", "Query": "SilverTransform()", "IsTransactional": false, "PropagateIngestionProperties": false}]'
```

### Gold — Materialized view (1-min OHLCV)
```kql
.create materialized-view with (backfill=true) PriceAggregates on table CleanPrices {
    CleanPrices
    | where is_valid == true
    | summarize
        open  = take_any(price_usd),
        high  = max(price_usd),
        low   = min(price_usd),
        close = arg_max(timestamp_utc, price_usd),
        volume = sum(volume_24h),
        event_count = count(),
        avg_latency_ms = avg(latency_ms)
      by symbol, window_start = bin(timestamp_utc, 1m)
}
```

---

## 7. Data Generator

**Language:** Python 3.11+
**Key dependencies:** `websockets`, `azure-eventhub`, `aiohttp`, `pydantic`

```
Pipeline/
├── generator/
│   ├── main.py               # Entry point
│   ├── sources/
│   │   ├── coinbase_ws.py    # WebSocket client
│   │   └── coingecko_rest.py # REST polling fallback
│   ├── publisher/
│   │   └── eventhub.py       # Azure Event Hub publisher
│   ├── models/
│   │   └── price_event.py    # Pydantic schema
│   └── config.py             # Settings from env vars
├── infra/
│   ├── main.bicep            # Azure resources
│   └── parameters.json
├── kql/
│   ├── 01_bronze.kql
│   ├── 02_silver.kql
│   └── 03_gold.kql
└── docs/
    ├── thesis_plan.md
    └── architecture.md       # ← this file
```

---

## 8. Performance Testing Plan

| Test | Target | Metric |
|------|--------|--------|
| Baseline latency | Single symbol, low volume | p50/p95/p99 ms end-to-end |
| Throughput ramp | 100 → 1000 events/sec | Max sustainable TPS without lag |
| Burst handling | 10x spike for 60s | Recovery time, dropped events |
| Sustained load | 500 events/sec for 1 hour | Drift, error rate |
| Partition scaling | 1 → 8 partitions | Throughput vs partition count |

Latency is measured as: `ingestion_time (Fabric) − timestamp_utc (source API)` stored per-event in `CleanPrices.latency_ms`.

---

## 9. Architectural Decisions (Resolved)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary data source | Coinbase WebSocket | True push-based streaming, no polling latency, 24/7 |
| Secondary source | CoinGecko REST (30s poll) | Market cap enrichment — lower frequency is sufficient |
| Symbols tracked | 5 core (BTC/ETH/SOL/BNB/XRP) | Sufficient event volume without API rate limit pressure |
| Event Hub SKU | Standard, 8 TUs | Kafka protocol support required; 8 TUs handles 800 events/sec |
| Fabric capacity | F2 minimum (F4 recommended) | F2 sufficient for dev; F4 for sustained 500+ eps load tests |
| Dashboard refresh | RTI Dashboard 30s, Power BI DirectQuery | Balances real-time appearance with Fabric query costs |
| Anomaly detection | KQL stored functions | No external service needed; runs directly on ingested data |

---

## 10. Deduplication and Idempotency

Each event carries a UUID `event_id` generated at the producer. This enables deduplication at the Silver layer if events are replayed (e.g. after Event Hub retention recovery):

```kql
// Deduplicate on ingest — use in Silver if needed
CleanPrices
| summarize arg_max(ingestion_time, *) by event_id
```

The Bronze table is append-only (by design) — deduplication happens at Silver, not Bronze. This follows the medallion principle: Bronze is a faithful record of what arrived, Silver is what is trusted.

---

## 11. Alternative Architecture Comparison

| Approach | Latency | Complexity | Cost | Fabric lock-in |
|----------|---------|------------|------|----------------|
| **This thesis: Fabric RTI (KQL + Eventstream)** | Low (seconds) | Medium | Low–Medium | High |
| Databricks Structured Streaming | Low (seconds) | High | High | Low |
| Azure Stream Analytics | Medium (seconds) | Low | Medium | Medium |
| Pure Kafka + ksqlDB | Very low (ms) | Very high | High (infra) | None |
| Synapse Analytics (batch) | High (minutes) | Medium | Medium | Medium |

**Why Fabric RTI for this thesis:**
- Unified platform — no separate orchestration between broker, processor, and storage
- KQL update policies replace Spark jobs for medallion transformations — significantly simpler
- Native Power BI integration with DirectQuery and real-time push
- Relevant research gap — insufficient academic evaluation exists for this platform

---

## 12. Cost Estimate (thesis setup)

| Resource | SKU | Est. monthly cost |
|----------|-----|-------------------|
| Event Hub Namespace | Standard, 2 TUs | ~€20 |
| Event Hub throughput (100 eps, 1 month) | ~26M events | ~€3 |
| Microsoft Fabric | F2 capacity (trial or paid) | €0 (60-day trial) / ~€260 |
| Azure Container Instances | 1 vCPU, 1.5GB, always-on | ~€30 |
| **Total (trial period)** | | **~€50** |
| **Total (post-trial, F2)** | | **~€310/month** |

For a thesis with a defined testing window (3 weeks), total Azure cost is approximately **€30–50** if using the Fabric free trial.
