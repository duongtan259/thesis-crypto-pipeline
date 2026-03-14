# Architecture & Implementation Design
**Thesis: Design and Implementation of a Real-Time ELT Pipeline Using Microsoft Fabric**
**Student:** Tan Phuc Duong | **Program:** Master's in Data Analytics

---

## 1. System Overview

Real-time cryptocurrency price data from the Coinbase WebSocket API is streamed through Azure Event Hub into Microsoft Fabric, where it is processed through a medallion architecture (Bronze → Silver → Gold) using KQL update policies and materialized views. The full pipeline runs 24/7 with no manual intervention.

**Why crypto data:**
- Continuous 24/7 streaming with no special permissions
- High volume: 8–10 events/sec across 5 trading pairs
- Simple, well-defined schema (timestamp, symbol, price, volume)
- Canonical enterprise streaming use case

---

## 2. Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA SOURCE                                                         │
│  Coinbase Advanced Trade WebSocket API                               │
│  wss://advanced-trade-ws.coinbase.com                                │
│  Symbols: BTC-USD, ETH-USD, SOL-USD, BNB-USD, XRP-USD               │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ JSON push (true WebSocket, no polling)
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DATA GENERATOR  (Python 3.11, asyncio)                              │
│  • Deployed to Azure Container Instances (ACI, West Europe)          │
│  • Deployed via GitHub Actions CI/CD (OIDC — no stored secrets)      │
│  • Normalises payload → canonical PriceEvent schema (Pydantic)       │
│  • Publishes batches to Azure Event Hub via AMQP                     │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ AMQP (generator-policy — Send only)
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  AZURE EVENT HUB                                                     │
│  Namespace: thesis-crypto-eh-ns (Standard, 2 TUs)                   │
│  Event Hub: crypto-prices (4 partitions, 7-day retention)            │
│  Auth: generator-policy (Send) | fabric-listen-policy (Listen)       │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ fabric-listen-policy (Listen only)
                        ▼
┌─────────────────────────────────────────────────────────────────────┐
│  MICROSOFT FABRIC — Real-Time Intelligence                           │
│  Workspace: Realtime Intelligence (tandatadev F2, North Europe)      │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  EVENTSTREAM  crypto-eventstream                             │   │
│  │  • Ingests from Event Hub via fabric-listen-policy           │   │
│  │  • Destination: price_raw table in crypto_db                 │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                             │                                        │
│                             ▼                                        │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  KQL DATABASE  crypto_db  (inside Eventhouse crypto_eventhouse)  │
│  │                                                              │   │
│  │  BRONZE  price_raw  (auto-created by Eventstream)            │   │
│  │  ├── Append-only, raw JSON preserved in raw_payload          │   │
│  │  └── Faithful record of everything that arrived              │   │
│  │                                                              │   │
│  │  SILVER  price_silver  [update policy → SilverTransform()]   │   │
│  │  ├── Auto-populated on every price_raw ingest                │   │
│  │  ├── Invalid rows filtered (null price, empty symbol)        │   │
│  │  └── latency_ms = ingestion_time − timestamp_utc             │   │
│  │                                                              │   │
│  │  GOLD  price_gold  [materialized view on price_silver]       │   │
│  │  ├── 1-min OHLCV candles per symbol (auto-updated)           │   │
│  │  ├── avg/p95/p99 latency per window                          │   │
│  │  └── event_count per window (throughput metric)              │   │
│  │                                                              │   │
│  │  ALERTS  price_alerts + DetectPriceSpikes()                  │   │
│  │  ├── KQL function: spikes > threshold% in time window        │   │
│  │  ├── KQL function: volume surges > Nx baseline               │   │
│  │  └── KQL function: log-return volatility ranking             │   │
│  └──────────────────────────┬───────────────────────────────────┘   │
│                             │                                        │
│  ┌──────────────────────────▼───────────────────────────────────┐   │
│  │  RTI DASHBOARD  Crypto Live Dashboard (30s auto-refresh)     │   │
│  │  • BTC-USD price line chart (price_gold)                     │   │
│  │  • Live prices table — all symbols (price_silver)            │   │
│  │  • End-to-end latency p50/p95 (price_silver)                 │   │
│  │  • Throughput events/min (price_silver)                      │   │
│  │  • Price spike alerts (DetectPriceSpikes)                    │   │
│  │  • Volatility ranking (GetVolatility)                        │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Medallion Architecture in KQL

The key architectural contribution of this thesis: the entire Bronze → Silver → Gold transformation is implemented **inside the KQL Database** using native Fabric features — no Spark, no notebooks, no external orchestration.

### Bronze — `price_raw`
Auto-created by Eventstream when the destination is configured. Append-only table. Every field from the generator is stored as-is, including `raw_payload` (full original JSON as `dynamic`). This is the audit trail — never modified.

### Silver — `price_silver`
Created via `.create-merge table`. An **update policy** triggers `SilverTransform()` on every ingest into `price_raw`. The function:
1. Validates each row (non-null price > 0, non-empty symbol)
2. Casts all types explicitly
3. Computes `latency_ms = ingestion_time − timestamp_utc` — the core thesis metric
4. Filters invalid rows — they stay in Bronze but never reach Silver

### Gold — `price_gold`
A **materialized view** on `price_silver`. Fabric automatically maintains it as new data arrives. Groups into 1-minute windows per symbol, computes OHLCV + latency percentiles. `backfill=true` processes all historical data on creation.

---

## 4. Data Schema

### Event (published to Event Hub by generator)

```json
{
  "event_id":       "uuid-v4",
  "symbol":         "BTC-USD",
  "price":          70655.23,
  "volume_24h":     3997.09,
  "market_cap":     0.0,
  "timestamp_utc":  "2026-03-14T19:05:03.461537+00:00",
  "source":         "coinbase_ws",
  "sequence":       46702,
  "ingestion_time": "2026-03-14T19:05:08.813343+00:00",
  "raw_payload":    "{...full json...}"
}
```

### Bronze — `price_raw`

| Column | Type | Description |
|--------|------|-------------|
| `event_id` | string | UUID from producer |
| `symbol` | string | Trading pair (BTC-USD etc.) |
| `price` | real | Trade price |
| `volume_24h` | real | 24-hour rolling volume |
| `market_cap` | real | Market capitalisation |
| `timestamp_utc` | datetime | Event timestamp from Coinbase |
| `source` | string | `coinbase_ws` |
| `sequence` | long | Producer sequence number |
| `ingestion_time` | datetime | Fabric ingest timestamp |
| `raw_payload` | dynamic | Full original JSON |

### Silver — `price_silver`

All Bronze columns except `raw_payload`, plus:

| Column | Type | Description |
|--------|------|-------------|
| `price_usd` | real | Normalised price in USD |
| `is_valid` | bool | Passed all validation rules |
| `latency_ms` | long | `ingestion_time − timestamp_utc` in ms |

### Gold — `price_gold` (materialized view)

| Column | Type | Description |
|--------|------|-------------|
| `symbol` | string | Trading pair |
| `window_start` | datetime | 1-minute bucket start |
| `open` | real | First price in window |
| `high` | real | Max price in window |
| `low` | real | Min price in window |
| `close` | real | Last price in window |
| `volume` | real | Total volume_24h sum |
| `event_count` | long | Events in window (throughput) |
| `avg_latency_ms` | real | Mean latency |
| `p95_latency_ms` | real | 95th percentile latency |
| `p99_latency_ms` | real | 99th percentile latency |

---

## 5. CI/CD & Deployment

```
Developer pushes to main
        │
        ▼
GitHub Actions CI (ci.yml)
  • Python lint (ruff)
  • Pydantic schema validation
  • KQL file existence check

If generator/** changed:
        │
        ▼
GitHub Actions Deploy (deploy.yml)
  • OIDC login to Azure (no stored client secrets)
  • docker buildx → linux/amd64 image
  • Push to ACR (thesiscryptoacr.azurecr.io)
  • Delete + recreate ACI container group
  • Verify container reaches Running state
```

**OIDC authentication:** GitHub Actions uses OpenID Connect to get a short-lived JWT from Azure AD. No `AZURE_CLIENT_SECRET` stored anywhere — only `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

---

## 6. Azure Resources

| Resource | Name | SKU |
|----------|------|-----|
| Resource Group | `rg-thesis-fabric` | — |
| Container Registry | `thesiscryptoacr` | Basic |
| Container Instance | `crypto-generator` | 1 vCPU, 1.5GB, West Europe |
| Event Hub Namespace | `thesis-crypto-eh-ns` | Standard, 2 TUs |
| Event Hub | `crypto-prices` | 4 partitions, 7-day retention |

## 7. Fabric Resources

| Resource | Name | Type |
|----------|------|------|
| Capacity | `tandatadev` | F2, North Europe |
| Workspace | `Realtime Intelligence` | Fabric workspace |
| Eventhouse | `crypto_eventhouse` | Eventhouse |
| KQL Database | `crypto_db` | KQL Database |
| Eventstream | `crypto-eventstream` | Eventstream |
| Dashboard | `Crypto Live Dashboard` | RTI Dashboard |

---

## 8. Performance Metrics

Latency is measured per-event as `latency_ms = ingestion_time − timestamp_utc` (Fabric ingest time minus Coinbase source timestamp). This captures end-to-end latency from the exchange tick to data being queryable in KQL.

| Metric | Query |
|--------|-------|
| p50/p95/p99 latency | `price_silver \| summarize percentile(toreal(latency_ms), 50/95/99)` |
| Throughput (eps) | `price_silver \| summarize count() by bin(timestamp_utc, 1m)` |
| OHLCV | `price_gold \| where symbol == "BTC-USD"` |
| Spike detection | `DetectPriceSpikes(2.0, 60s)` |

---

## 9. Architectural Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Data source | Coinbase WebSocket | True push, no polling latency, 24/7 |
| Symbols | 5 (BTC/ETH/SOL/BNB/XRP) | Sufficient volume, no rate limit pressure |
| Event Hub SKU | Standard, 2 TUs | Kafka protocol support; handles ~200 eps |
| Medallion implementation | KQL update policy + materialized view | No Spark/notebooks needed |
| Fabric capacity | F2 | Sufficient for thesis testing window |
| Dashboard refresh | 30 seconds | Minimum supported on F2 |
| Anomaly detection | KQL stored functions | Runs on ingested data, no external service |
| CI/CD auth | OIDC (no client secrets) | Industry best practice, no secret rotation |

---

## 10. Alternative Architecture Comparison

| Approach | Latency | Complexity | Cost | Notes |
|----------|---------|------------|------|-------|
| **This thesis: Fabric RTI** | seconds | Medium | Low–Medium | Unified platform, KQL-native medallion |
| Databricks Structured Streaming | seconds | High | High | More flexible, heavier ops overhead |
| Azure Stream Analytics | seconds | Low | Medium | Limited transformation expressiveness |
| Pure Kafka + ksqlDB | ms | Very high | High | Best latency, highest infra cost |
| Synapse Analytics (batch) | minutes | Medium | Medium | Not suitable for real-time |
