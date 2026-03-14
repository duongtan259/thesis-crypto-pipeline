# Real-Time Crypto Streaming Pipeline with Microsoft Fabric

> Master's Thesis — Tan Phuc Duong | Master's in Data Analytics

A production-grade real-time ELT pipeline that streams live cryptocurrency prices from the Coinbase API, routes them through Azure Event Hub, and implements a **medallion architecture (Bronze → Silver → Gold)** entirely inside Microsoft Fabric's KQL Database using update policies and materialized views.

---

## Architecture

```
┌──────────────────┐     WebSocket      ┌─────────────────────┐
│  Coinbase API    │ ─────────────────► │  Python Generator   │
│  (live ticks)    │                    │  (Docker container) │
└──────────────────┘                    └──────────┬──────────┘
                                                   │ AMQP
┌──────────────────┐     REST (30s)                ▼
│  CoinGecko API   │ ───────────────► market cap enrichment
│  (market cap)    │
└──────────────────┘         ┌─────────────────────────────┐
                             │     Azure Event Hub          │
                             │     (4 partitions)           │
                             └──────────────┬──────────────┘
                                            │ Eventstream
                             ┌──────────────▼──────────────┐
                             │   Microsoft Fabric           │
                             │                              │
                             │  BRONZE  price_raw           │
                             │     ↓ update policy          │
                             │  SILVER  price_silver        │
                             │     ↓ materialized view      │
                             │  GOLD    price_gold          │
                             │     ↓                        │
                             │  ALERTS  DetectPriceSpikes   │
                             │                              │
                             │  RTI Dashboard + Power BI    │
                             └─────────────────────────────┘
```

---

## Key Features

- **Dual-source ingestion** — Coinbase WebSocket for real-time ticks + CoinGecko REST for market cap enrichment, merged into a single event stream
- **Medallion architecture in KQL** — Bronze/Silver/Gold implemented entirely with update policies and materialized views, no Spark or notebooks required
- **Anomaly detection** — Real-time price spike alerts (`DetectPriceSpikes`), volume surge detection, and volatility tracking built as KQL stored functions
- **End-to-end latency tracking** — every event stores `latency_ms = ingestion_time − timestamp_utc`, enabling p50/p95/p99 latency dashboards
- **Load testing** — configurable stress tester that measures maximum sustainable throughput

---

## Results (thesis benchmarks)

| Metric | Value |
|--------|-------|
| Sustained throughput | ~500 events/min (8-9 eps) |
| p50 end-to-end latency | ~2,000 ms |
| p95 end-to-end latency | ~5,000 ms |
| p99 end-to-end latency | ~6,500 ms |
| Symbols tracked | 5 (BTC, ETH, SOL, BNB, XRP) |
| Data source | Coinbase WebSocket |

*Preliminary results from initial pipeline run. Full load test results TBD.*

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Data source | Coinbase Advanced Trade WebSocket API |
| Enrichment | CoinGecko REST API |
| Generator | Python 3.11, asyncio, Pydantic, azure-eventhub |
| Broker | Azure Event Hub (Standard, 4 partitions) |
| Local dev | Docker Compose, Apache Kafka, Kafka UI |
| Ingestion | Microsoft Fabric Eventstream |
| Storage + query | KQL Database (Kusto) |
| Medallion layers | KQL update policies + materialized views |
| Anomaly detection | KQL stored functions |
| Visualisation | Fabric RTI Dashboard + Power BI (DirectQuery) |

---

## Project Structure

```
Pipeline/
├── generator/
│   ├── main.py                    # Entry point
│   ├── config.py                  # Settings (env vars)
│   ├── sources/
│   │   ├── coinbase_ws.py         # Coinbase WebSocket client
│   │   ├── coingecko_rest.py      # CoinGecko REST polling
│   │   └── merged.py              # Combined: WS ticks + REST market cap
│   ├── publisher/
│   │   ├── eventhub.py            # Azure Event Hub publisher
│   │   └── kafka.py               # Local Kafka publisher (dev)
│   └── models/
│       └── price_event.py         # Pydantic event schema
├── kql/
│   ├── 01_bronze.kql              # Raw ingestion table + mapping
│   ├── 02_silver.kql              # Cleaned data + update policy
│   ├── 03_gold.kql                # OHLCV materialized view + functions
│   ├── 04_verify.kql              # Verification queries
│   └── 05_anomaly_detection.kql   # Price spike + volume surge alerts
├── docker/
│   ├── docker-compose.yml         # Local Kafka stack
│   └── Dockerfile.generator       # Generator container
├── scripts/
│   ├── setup_azure.sh             # One-shot Azure resource creation
│   ├── load_test.py               # Throughput stress tester
│   └── results/                   # Load test output (JSON)
└── docs/
    ├── fabric_setup.md
    ├── architecture.md
    ├── thesis_plan.md
    └── implementation_plan.md
```

---

## Quick Start

### Local dev (no Azure needed)

```bash
git clone <repo>
cd Pipeline
cp .env.example .env

# Start Kafka + generator (pulls live Coinbase data)
docker compose -f docker/docker-compose.yml --profile local up

# Browse messages at http://localhost:8080 (Kafka UI)
```

### Azure + Fabric (full pipeline)

```bash
# 1. Deploy Azure infrastructure
az deployment group create \
  --resource-group rg-thesis-fabric \
  --template-file infra/main.bicep

# 2. Set environment variables (.env or GitHub secrets)
#    EVENTHUB_CONNECTION_STRING — from generator-policy (Send)
#    EVENTHUB_NAME=crypto-prices

# 3. Deploy generator to ACI via GitHub Actions (push to main)
#    Or run locally:
docker compose -f docker/docker-compose.yml up
```

**Microsoft Fabric setup** (one-time, in the Fabric portal):

1. Create **Eventhouse** → `crypto_eventhouse` → KQL Database `crypto_db`
2. Create **Eventstream** → `crypto-eventstream`
   - Source: Azure Event Hub (`thesis-crypto-eh-ns`, key: `fabric-listen-policy`, Listen rights)
   - Destination: Eventhouse → `crypto_eventhouse` / table `price_raw`
3. Open KQL Database `crypto_db` and run scripts in order:
   ```
   kql/01_bronze.kql        # adds JSON mapping to price_raw
   kql/02_silver.kql        # creates price_silver + update policy
   kql/03_gold.kql          # creates price_gold materialized view
   kql/05_anomaly_detection.kql  # creates alert functions
   ```
4. Verify data is flowing:
   ```kql
   price_raw | count
   price_silver | take 5
   price_gold | take 5
   DetectPriceSpikes(1.0, 60s)
   ```
5. Build **RTI Dashboard** → `Crypto Live Dashboard` (see `docs/fabric_setup.md`)

### Load testing

```bash
cd Pipeline

# 100 eps for 60s against local Kafka
python scripts/load_test.py --eps 100 --duration 60 --target kafka

# 500 eps for 120s against Azure Event Hub
python scripts/load_test.py --eps 500 --duration 120 --target eventhub

# Results saved to scripts/results/load_test_<timestamp>.json
```

---

## Event Schema

Each event published to Event Hub / Kafka:

```json
{
  "event_id":       "uuid-v4",
  "symbol":         "BTC-USD",
  "price":          71392.58,
  "volume_24h":     12237.61,
  "market_cap":     1412938472000.0,
  "timestamp_utc":  "2026-03-13T20:57:19.631Z",
  "source":         "coinbase_ws",
  "sequence":       4001,
  "ingestion_time": "2026-03-13T20:57:23.670Z",
  "raw_payload":    "{...}"
}
```

`latency_ms = ingestion_time − timestamp_utc` is computed in the Silver layer and stored per-event for performance analysis.

---

## Anomaly Detection

Built as KQL stored functions on top of the Gold layer:

```kql
-- Price spike > 2% in last 60 seconds
DetectPriceSpikes(2.0, 60s)

-- Volume surge > 3x baseline
DetectVolumeSurges(3.0, 10m)

-- Volatility ranking across all symbols
GetVolatility(1h)
```

Alerts are classified as `LOW / MEDIUM / HIGH / CRITICAL` based on the magnitude of the move.

---

## Security Architecture

### Actual thesis setup
```
GitHub Actions (OIDC — no secrets stored)
  └─ Builds Docker image → pushes to ACR
       └─ Deploys to Azure Container Instances (ACI, West Europe)
            └─ Generator reads EVENTHUB_CONNECTION_STRING from secure env var
                 └─ Publishes via AMQP to Event Hub (generator-policy, Send only)
                      └─ Fabric Eventstream (fabric-listen-policy, Listen only)
                           └─ price_raw → price_silver → price_gold (KQL Database)
                                └─ RTI Dashboard (30s refresh)
```

### Production / enterprise equivalent

**Authentication — Managed Identity (no secrets)**
```
Azure Container Instance
  └─ Managed Identity (no password, no .env secrets)
       └─ Azure AD issues JWT token automatically
            └─ Event Hub validates token via OIDC
```
OIDC (OpenID Connect) is the standard protocol for token-based identity. The container proves who it is to Azure AD, which issues a short-lived JWT token — no connection strings stored anywhere.

**Authorization — RBAC (least privilege)**

| Component | Role granted |
|-----------|-------------|
| Generator container | Event Hub Data Sender |
| Fabric Eventstream | Event Hub Data Receiver |
| Analysts | KQL Database Viewer |
| No one | Delete / admin rights |

**Network — VNet + Private Endpoints**
```
Public internet
    │
[Event Hub firewall]  ← whitelist only ACI outbound IP
    │
Event Hub
    │
[Private Endpoint]    ← Fabric ↔ Event Hub over Azure internal network
    │                   never traverses public internet
Fabric KQL
```

**Encryption**
- In transit: TLS 1.2+ on all connections (Kafka protocol, AMQP, REST)
- At rest: KQL Database encrypted AES-256, keys managed by Azure Key Vault

**Protocols**
- **Kafka protocol** — used by the generator (open standard, runs over TLS)
- **AMQP** (Advanced Message Queuing Protocol) — Event Hub's native protocol, used internally by Fabric Eventstream
- Event Hub translates between the two transparently

This thesis uses connection string auth and a public Event Hub endpoint for simplicity. The table above describes the hardening steps required for a production deployment.

---

## Thesis Context

This project is the practical component of a Master's thesis investigating Microsoft Fabric's Real-Time Intelligence capabilities. The thesis evaluates:

- End-to-end latency from Coinbase API → Fabric KQL Dashboard
- Maximum sustainable throughput before lag accumulates
- The medallion architecture pattern within KQL Database (no Spark/notebooks)
- Practical challenges and limitations of the platform

**Research question:** *How can Microsoft Fabric Real-Time Intelligence be used to build a real-time streaming pipeline with medallion architecture?*
