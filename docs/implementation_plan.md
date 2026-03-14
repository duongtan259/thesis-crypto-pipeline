# Implementation Plan
**Thesis: Design and Implementation of a Real-Time ELT Pipeline Using Microsoft Fabric**

---

## Overview

This document is the step-by-step implementation guide. Follow the phases in order. Each phase has prerequisites, instructions, and a verification checklist.

```
Phase 1: Local environment (Docker)
Phase 2: Azure resources (Event Hub)
Phase 3: Data generator (Python)
Phase 4: Microsoft Fabric setup (Eventstream + KQL Database)
Phase 5: Medallion layers (Bronze → Silver → Gold)
Phase 6: Dashboards (RTI Dashboard + Power BI)
Phase 7: Performance testing
```

---

## Phase 1: Local Development Environment (Docker)

### 1.1 Prerequisites

- Docker Desktop installed and running
- Python 3.11+ installed
- Azure CLI installed (`brew install azure-cli`)
- A Microsoft Fabric trial or capacity (sign up at fabric.microsoft.com)
- An Azure subscription (free trial works)

### 1.2 Project folder structure

Create this structure before starting:

```
Pipeline/
├── generator/
│   ├── sources/
│   ├── publisher/
│   └── models/
├── infra/
├── kql/
├── docker/
└── docs/
```

```bash
mkdir -p Pipeline/{generator/{sources,publisher,models},infra,kql,docker,docs}
```

### 1.3 Docker Compose — local services

Create `Pipeline/docker/docker-compose.yml`:

```yaml
version: "3.9"

services:

  # Optional: local Kafka for offline testing before connecting to Event Hub
  zookeeper:
    image: confluentinc/cp-zookeeper:7.6.0
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181
    ports:
      - "2181:2181"

  kafka:
    image: confluentinc/cp-kafka:7.6.0
    depends_on: [zookeeper]
    ports:
      - "9092:9092"
    environment:
      KAFKA_BROKER_ID: 1
      KAFKA_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://localhost:9092
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"

  # Data generator container
  generator:
    build:
      context: ../generator
      dockerfile: ../docker/Dockerfile.generator
    environment:
      - EVENTHUB_CONNECTION_STRING=${EVENTHUB_CONNECTION_STRING}
      - EVENTHUB_NAME=${EVENTHUB_NAME}
      - SYMBOLS=${SYMBOLS:-BTC-USD,ETH-USD,SOL-USD,BNB-USD,XRP-USD}
      - EVENTS_PER_SECOND=${EVENTS_PER_SECOND:-100}
      - USE_LOCAL_KAFKA=${USE_LOCAL_KAFKA:-false}
      - KAFKA_BOOTSTRAP=${KAFKA_BOOTSTRAP:-kafka:9092}
    depends_on:
      - kafka
    restart: unless-stopped
```

Create `Pipeline/docker/Dockerfile.generator`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

Create `Pipeline/generator/requirements.txt`:

```
websockets==12.0
azure-eventhub==5.11.6
aiohttp==3.9.5
pydantic==2.7.1
pydantic-settings==2.3.1
python-dotenv==1.0.1
aiokafka==0.10.0
structlog==24.2.0
```

### 1.4 Environment file

Create `Pipeline/.env` (never commit this):

```env
# Azure Event Hub
EVENTHUB_CONNECTION_STRING=Endpoint=sb://...
EVENTHUB_NAME=crypto-prices

# Generator settings
SYMBOLS=BTC-USD,ETH-USD,SOL-USD,BNB-USD,XRP-USD,ADA-USD,DOGE-USD,AVAX-USD
EVENTS_PER_SECOND=100

# Local dev toggle
USE_LOCAL_KAFKA=false
```

Add to `.gitignore`:

```
.env
__pycache__/
*.pyc
.venv/
```

---

## Phase 2: Azure Resources

### 2.1 Login to Azure

```bash
az login
az account list --output table
az account set --subscription "<your-subscription-id>"
```

### 2.2 Create resources via Azure CLI

```bash
RG="rg-thesis-fabric"
LOCATION="westeurope"
EH_NAMESPACE="thesis-crypto-eh-ns"
EH_NAME="crypto-prices"
KV_NAME="thesis-crypto-kv"
SP_NAME="thesis-crypto-generator-sp"

# Resource group
az group create --name $RG --location $LOCATION

# Key Vault — store all secrets here, never in .env or code
az keyvault create \
  --name $KV_NAME \
  --resource-group $RG \
  --location $LOCATION \
  --sku standard

# Event Hub namespace (Standard tier — required for Kafka protocol)
az eventhubs namespace create \
  --name $EH_NAMESPACE \
  --resource-group $RG \
  --location $LOCATION \
  --sku Standard \
  --capacity 2 \
  --enable-kafka true

# Event Hub with 8 partitions
az eventhubs eventhub create \
  --name $EH_NAME \
  --namespace-name $EH_NAMESPACE \
  --resource-group $RG \
  --partition-count 8 \
  --message-retention 1

# Authorization rule
az eventhubs eventhub authorization-rule create \
  --name generator-policy \
  --eventhub-name $EH_NAME \
  --namespace-name $EH_NAMESPACE \
  --resource-group $RG \
  --rights Send Listen

# Store the connection string in Key Vault — NOT in .env
CONN_STR=$(az eventhubs eventhub authorization-rule keys list \
  --name generator-policy \
  --eventhub-name $EH_NAME \
  --namespace-name $EH_NAMESPACE \
  --resource-group $RG \
  --query primaryConnectionString \
  --output tsv)

az keyvault secret set \
  --vault-name $KV_NAME \
  --name "eventhub-connection-string" \
  --value "$CONN_STR"

echo "Connection string stored in Key Vault — never written to disk"
```

### 2.3 Create Service Principal for the generator

The generator authenticates to Key Vault using a **Service Principal** (an app identity), so no secrets are hard-coded anywhere.

```bash
# Create service principal
SP=$(az ad sp create-for-rbac \
  --name $SP_NAME \
  --skip-assignment \
  --output json)

SP_CLIENT_ID=$(echo $SP | python3 -c "import sys,json; print(json.load(sys.stdin)['appId'])")
SP_CLIENT_SECRET=$(echo $SP | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")
SP_TENANT_ID=$(echo $SP | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant'])")

# Grant the SP read access to Key Vault secrets only
az keyvault set-policy \
  --name $KV_NAME \
  --spn $SP_CLIENT_ID \
  --secret-permissions get list

# Store SP credentials in Key Vault too (bootstrap only — used by ACI)
az keyvault secret set --vault-name $KV_NAME --name "sp-client-id" --value "$SP_CLIENT_ID"
az keyvault secret set --vault-name $KV_NAME --name "sp-tenant-id" --value "$SP_TENANT_ID"

# The SP client secret is the ONLY credential you need to handle manually
# Store it securely — e.g. in your password manager or as an ACI secure env var
echo "SP_CLIENT_SECRET=$SP_CLIENT_SECRET"
echo "Save this securely — it will not be shown again"
```

At runtime, the generator fetches the Event Hub connection string from Key Vault using the SP credentials. Nothing is stored in `.env` except for local Kafka development.

### 2.3 Alternative: Bicep template

Create `Pipeline/infra/main.bicep`:

```bicep
param location string = resourceGroup().location
param eventHubNamespaceName string = 'thesis-crypto-eh-ns'
param eventHubName string = 'crypto-prices'

resource ehNamespace 'Microsoft.EventHub/namespaces@2023-01-01-preview' = {
  name: eventHubNamespaceName
  location: location
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 2
  }
  properties: {
    isAutoInflateEnabled: false
    kafkaEnabled: true
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2023-01-01-preview' = {
  parent: ehNamespace
  name: eventHubName
  properties: {
    partitionCount: 8
    messageRetentionInDays: 1
  }
}

resource authRule 'Microsoft.EventHub/namespaces/eventhubs/authorizationRules@2023-01-01-preview' = {
  parent: eventHub
  name: 'generator-policy'
  properties: {
    rights: ['Send', 'Listen']
  }
}

output connectionString string = listKeys(authRule.id, authRule.apiVersion).primaryConnectionString
```

Deploy:

```bash
az deployment group create \
  --resource-group rg-thesis-fabric \
  --template-file Pipeline/infra/main.bicep
```

### 2.4 Verification checklist

- [ ] Resource group `rg-thesis-fabric` exists
- [ ] Event Hub namespace is in **Active** state
- [ ] Event Hub `crypto-prices` has 8 partitions
- [ ] Connection string copied to `.env`

---

## Phase 3: Data Generator (Python)

### 3.1 Pydantic model

Create `Pipeline/generator/models/price_event.py`:

```python
from datetime import datetime, timezone
from uuid import uuid4
from pydantic import BaseModel, field_validator


class PriceEvent(BaseModel):
    event_id: str = ""
    symbol: str
    price: float
    volume_24h: float
    market_cap: float
    timestamp_utc: datetime
    source: str
    sequence: int = 0

    def model_post_init(self, __context):
        if not self.event_id:
            self.event_id = str(uuid4())

    def to_json_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")
```

### 3.2 Coinbase WebSocket source

Create `Pipeline/generator/sources/coinbase_ws.py`:

```python
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import websockets

from models.price_event import PriceEvent

WS_URL = "wss://advanced-trade-ws.coinbase.com"
logger = logging.getLogger(__name__)


async def stream_prices(symbols: list[str]) -> AsyncIterator[PriceEvent]:
    """Subscribe to Coinbase WebSocket and yield PriceEvent for each ticker update."""
    product_ids = symbols  # e.g. ["BTC-USD", "ETH-USD"]

    subscribe_msg = json.dumps({
        "type": "subscribe",
        "product_ids": product_ids,
        "channel": "ticker",
    })

    sequence = 0

    while True:  # reconnect loop
        try:
            async with websockets.connect(WS_URL, ping_interval=20) as ws:
                await ws.send(subscribe_msg)
                logger.info("Subscribed to Coinbase ticker for %s", product_ids)

                async for raw in ws:
                    msg = json.loads(raw)

                    if msg.get("channel") != "ticker":
                        continue

                    for event in msg.get("events", []):
                        for ticker in event.get("tickers", []):
                            try:
                                sequence += 1
                                yield PriceEvent(
                                    symbol=ticker["product_id"],
                                    price=float(ticker["price"]),
                                    volume_24h=float(ticker.get("volume_24_h", 0)),
                                    market_cap=0.0,  # not in ticker feed
                                    timestamp_utc=datetime.now(timezone.utc),
                                    source="coinbase_ws",
                                    sequence=sequence,
                                )
                            except (KeyError, ValueError) as e:
                                logger.warning("Skipping malformed ticker: %s", e)

        except (websockets.WebSocketException, OSError) as e:
            logger.error("WebSocket error: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)
```

### 3.3 Event Hub publisher

Create `Pipeline/generator/publisher/eventhub.py`:

```python
import asyncio
import logging
from azure.eventhub.aio import EventHubProducerClient
from azure.eventhub import EventData

from models.price_event import PriceEvent

logger = logging.getLogger(__name__)


class EventHubPublisher:
    def __init__(self, connection_string: str, eventhub_name: str, batch_size: int = 100):
        self._connection_string = connection_string
        self._eventhub_name = eventhub_name
        self._batch_size = batch_size
        self._client: EventHubProducerClient | None = None

    async def __aenter__(self):
        self._client = EventHubProducerClient.from_connection_string(
            self._connection_string,
            eventhub_name=self._eventhub_name,
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.close()

    async def send_batch(self, events: list[PriceEvent]) -> None:
        if not self._client:
            raise RuntimeError("Publisher not started — use async context manager")

        batch = await self._client.create_batch()
        for event in events:
            try:
                batch.add(EventData(event.to_json_bytes()))
            except ValueError:
                # batch full — send and start a new one
                await self._client.send_batch(batch)
                logger.debug("Sent batch of %d events", len(batch))
                batch = await self._client.create_batch()
                batch.add(EventData(event.to_json_bytes()))

        if len(batch) > 0:
            await self._client.send_batch(batch)
```

### 3.4 Config

Create `Pipeline/generator/config.py`:

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    eventhub_connection_string: str
    eventhub_name: str = "crypto-prices"
    symbols: str = "BTC-USD,ETH-USD,SOL-USD"
    events_per_second: int = 100
    batch_size: int = 50

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip() for s in self.symbols.split(",")]

    class Config:
        env_file = ".env"


settings = Settings()
```

### 3.5 Main entry point

Create `Pipeline/generator/main.py`:

```python
import asyncio
import logging
import time

from config import settings
from sources.coinbase_ws import stream_prices
from publisher.eventhub import EventHubPublisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def run():
    logger.info("Starting generator | symbols=%s | target_eps=%d",
                settings.symbol_list, settings.events_per_second)

    interval = 1.0 / settings.events_per_second
    buffer: list = []
    sent_total = 0
    start = time.monotonic()

    async with EventHubPublisher(
        settings.eventhub_connection_string,
        settings.eventhub_name,
        batch_size=settings.batch_size,
    ) as publisher:

        async for event in stream_prices(settings.symbol_list):
            buffer.append(event)

            if len(buffer) >= settings.batch_size:
                await publisher.send_batch(buffer)
                sent_total += len(buffer)
                buffer.clear()

                elapsed = time.monotonic() - start
                logger.info("Sent %d events | %.1f eps", sent_total, sent_total / elapsed)

            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(run())
```

### 3.6 Run locally (without Docker)

```bash
cd Pipeline/generator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 3.7 Run with Docker Compose

```bash
cd Pipeline
docker compose -f docker/docker-compose.yml --env-file .env up --build generator
```

### 3.8 Verification checklist

- [ ] Generator connects to Coinbase WebSocket without errors
- [ ] Events visible in Azure Event Hub metrics (incoming messages > 0)
- [ ] No send errors in generator logs

---

## Phase 4: Microsoft Fabric Setup

### 4.1 Create Fabric workspace

1. Go to [app.fabric.microsoft.com](https://app.fabric.microsoft.com)
2. Left sidebar → **Workspaces** → **New workspace**
3. Name: `Thesis-RTI`
4. Assign to a Fabric capacity (F2 minimum for KQL Database)
5. Click **Apply**

### 4.2 Create KQL Database

1. In `Thesis-RTI` workspace → **New** → **KQL Database**
2. Name: `CryptoPriceDB`
3. Wait for provisioning (~2 min)

### 4.3 Create Eventstream

1. In workspace → **New** → **Eventstream**
2. Name: `crypto-price-stream`

**Add source — Azure Event Hub:**
1. Click **Add source** → **Azure Event Hub**
2. Fill in:
   - Connection: create new → paste Event Hub connection string
   - Event Hub: `crypto-prices`
   - Consumer group: `$Default`
   - Data format: `JSON`
3. Click **Save**

**Add destination — KQL Database:**
1. Click **Add destination** → **KQL Database**
2. Select `CryptoPriceDB`
3. Table: type `RawPrices` (will be created by the KQL scripts in Phase 5)
4. Mapping: auto-detect JSON
5. Click **Save**

### 4.4 Verification checklist

- [ ] Workspace `Thesis-RTI` created with Fabric capacity
- [ ] KQL Database `CryptoPriceDB` in **Running** state
- [ ] Eventstream shows data flowing (check the stream preview)

---

## Phase 5: KQL Medallion Layers

Run these scripts in order inside the KQL Database query editor.

### 5.1 Bronze — `Pipeline/kql/01_bronze.kql`

```kql
// ── BRONZE: Raw ingestion table ──────────────────────────────────────
.create-merge table RawPrices (
    event_id:       string,
    symbol:         string,
    price:          real,
    volume_24h:     real,
    market_cap:     real,
    timestamp_utc:  datetime,
    source:         string,
    sequence:       long,
    ingestion_time: datetime,
    raw_payload:    dynamic
)

// Ingestion mapping — maps JSON fields to table columns
.create-or-alter table RawPrices ingestion json mapping 'RawPricesMapping'
```
[
    {"column":"event_id",       "path":"$.event_id"},
    {"column":"symbol",         "path":"$.symbol"},
    {"column":"price",          "path":"$.price"},
    {"column":"volume_24h",     "path":"$.volume_24h"},
    {"column":"market_cap",     "path":"$.market_cap"},
    {"column":"timestamp_utc",  "path":"$.timestamp_utc"},
    {"column":"source",         "path":"$.source"},
    {"column":"sequence",       "path":"$.sequence"},
    {"column":"ingestion_time", "path":"$.ingestion_time"},
    {"column":"raw_payload",    "path":"$"}
]
```

// Retention: keep 30 days in hot cache for performance testing
.alter table RawPrices policy caching hot = 30d
```

### 5.2 Silver — `Pipeline/kql/02_silver.kql`

```kql
// ── SILVER: Cleaned and validated data ───────────────────────────────

// Target table
.create-merge table CleanPrices (
    event_id:       string,
    symbol:         string,
    price:          real,
    volume_24h:     real,
    market_cap:     real,
    timestamp_utc:  datetime,
    source:         string,
    sequence:       long,
    ingestion_time: datetime,
    price_usd:      real,
    is_valid:       bool,
    latency_ms:     long
)

// Transformation function
.create-or-alter function SilverTransform() {
    RawPrices
    | extend
        price_usd   = toreal(price),
        is_valid    = (isnotnull(price) and price > 0.0 and isnotnull(symbol)),
        latency_ms  = datetime_diff('millisecond', ingestion_time, timestamp_utc)
    | where is_valid == true
    | project
        event_id, symbol, price, volume_24h, market_cap,
        timestamp_utc, source, sequence, ingestion_time,
        price_usd, is_valid, latency_ms
}

// Update policy: automatically runs SilverTransform when rows land in RawPrices
.alter table CleanPrices policy update
@'[{
    "IsEnabled": true,
    "Source": "RawPrices",
    "Query": "SilverTransform()",
    "IsTransactional": false,
    "PropagateIngestionProperties": false
}]'

.alter table CleanPrices policy caching hot = 30d
```

### 5.3 Gold — `Pipeline/kql/03_gold.kql`

```kql
// ── GOLD: 1-minute OHLCV aggregates ──────────────────────────────────

.create materialized-view with (backfill=true) PriceAggregates on table CleanPrices {
    CleanPrices
    | where is_valid == true
    | summarize
        open            = minif(price_usd, timestamp_utc == min(timestamp_utc)),
        high            = max(price_usd),
        low             = min(price_usd),
        close           = maxif(price_usd, timestamp_utc == max(timestamp_utc)),
        volume          = sum(volume_24h),
        event_count     = count(),
        avg_latency_ms  = avg(toreal(latency_ms)),
        p95_latency_ms  = percentile(toreal(latency_ms), 95),
        p99_latency_ms  = percentile(toreal(latency_ms), 99)
      by
        symbol,
        window_start = bin(timestamp_utc, 1m)
}

// ── GOLD: Rolling stats via stored function ───────────────────────────
.create-or-alter function GetLatencyStats(lookback: timespan = 5m) {
    CleanPrices
    | where timestamp_utc > ago(lookback)
    | summarize
        p50 = percentile(toreal(latency_ms), 50),
        p95 = percentile(toreal(latency_ms), 95),
        p99 = percentile(toreal(latency_ms), 99),
        max_latency = max(toreal(latency_ms)),
        event_count = count()
      by bin(timestamp_utc, 1m)
    | order by timestamp_utc desc
}
```

### 5.4 Verification — run these queries after setup

```kql
// Check data is flowing into Bronze
RawPrices | count

// Check Silver update policy is working
CleanPrices | take 10

// Check Gold materialized view
PriceAggregates | take 10

// Check latency stats
GetLatencyStats(5m)
```

---

## Phase 6: Dashboards

### 6.1 RTI Dashboard (Fabric native)

1. In workspace → **New** → **Real-Time Dashboard**
2. Name: `Crypto Live Dashboard`
3. Add data source: select `CryptoPriceDB`

**Tiles to create:**

| Tile | Query | Visual |
|------|-------|--------|
| Live BTC price | `CleanPrices \| where symbol == "BTC-USD" \| top 1 by timestamp_utc \| project price_usd` | Stat |
| Price table | `CleanPrices \| where timestamp_utc > ago(1m) \| summarize last_price=max(price_usd) by symbol` | Table |
| Latency p95 | `GetLatencyStats(5m) \| project timestamp_utc, p95` | Line chart |
| Events/min | `CleanPrices \| where timestamp_utc > ago(5m) \| summarize count() by bin(timestamp_utc, 1m)` | Bar chart |
| OHLCV candles | `PriceAggregates \| where symbol == "BTC-USD" \| order by window_start asc` | Table → export to Power BI |

4. Set auto-refresh to **30 seconds**

### 6.2 Power BI report

1. In KQL Database → **Connect to Power BI Desktop** (export .pbids file)
2. Open in Power BI Desktop
3. Use DirectQuery mode (not Import) — critical for real-time
4. Create measures:
   - `Latest Price = LASTNONBLANK(CleanPrices[price_usd], 1)`
   - `Avg Latency = AVERAGE(CleanPrices[latency_ms])`
5. Publish to Fabric workspace
6. Set dataset refresh: **Real-time push** (DirectQuery auto-refreshes)

---

## Phase 5b: Anomaly Detection Layer

Run `kql/05_anomaly_detection.kql` in the KQL Database after completing Phase 5.

This creates:
- `PriceAlerts` table — stores triggered alerts
- `DetectPriceSpikes(threshold_pct, window)` — finds price moves above threshold within a time window
- `DetectVolumeSurges(multiplier, lookback)` — detects volume spikes vs rolling baseline
- `GetVolatility(lookback)` — ranks symbols by log-return standard deviation
- `AlertSummary` materialized view — 5-min spike summary for dashboard

**Verify:**
```kql
DetectPriceSpikes(1.0, 5m)   // spikes > 1% in last 5 min
GetVolatility(1h)             // volatility ranking
AlertSummary | take 20
```

---

## Phase 7: Performance Testing

### 7.1 Test scenarios — use the load_test.py script

```bash
cd Pipeline

# Test 1: Baseline — 50 events/sec for 5 minutes
python scripts/load_test.py --eps 50 --duration 300 --target eventhub

# Test 2: Normal load — 200 events/sec for 5 minutes
python scripts/load_test.py --eps 200 --duration 300 --target eventhub

# Test 3: High load — 500 events/sec for 5 minutes
python scripts/load_test.py --eps 500 --duration 300 --target eventhub

# Test 4: Peak — 1000 events/sec for 2 minutes
python scripts/load_test.py --eps 1000 --duration 120 --target eventhub
```

Results are saved automatically to `scripts/results/load_test_<eps>eps_<timestamp>.json`.

### 7.2 Metrics to collect (via KQL)

```kql
// Throughput — events per minute
CleanPrices
| where timestamp_utc > ago(30m)
| summarize events_per_min = count() by bin(timestamp_utc, 1m)
| order by timestamp_utc asc

// End-to-end latency distribution
CleanPrices
| where timestamp_utc > ago(30m)
| summarize
    p50  = percentile(toreal(latency_ms), 50),
    p95  = percentile(toreal(latency_ms), 95),
    p99  = percentile(toreal(latency_ms), 99),
    max  = max(toreal(latency_ms))
  by bin(timestamp_utc, 5m)

// Data loss check — gap in sequences
RawPrices
| where source == "coinbase_ws"
| order by sequence asc
| extend expected = prev(sequence) + 1
| where sequence != expected
| project symbol, sequence, expected, gap = sequence - expected
```

### 7.3 Results template

Record findings in `docs/test_results.md` using this table:

| Test | Target EPS | Actual EPS | p50 latency (ms) | p95 latency (ms) | p99 latency (ms) | Errors |
|------|-----------|------------|-----------------|-----------------|-----------------|--------|
| Baseline | 50 | | | | | |
| Normal | 200 | | | | | |
| High | 500 | | | | | |
| Peak | 1000 | | | | | |

Read latency from KQL after each test run:
```kql
GetLatencyStats(5m)
```

---

## Phase 8: Deploy Generator to Azure Container Instances

Once testing is complete and the connection string is in `.env`, deploy the generator as a permanent cloud container so it runs without your Mac.

```bash
RG="rg-thesis-fabric"
LOCATION="westeurope"

# Create container registry (one-time)
az acr create --name thesiscryptoacr --resource-group $RG --sku Basic --admin-enabled true

# Build and push Docker image
az acr build \
  --registry thesiscryptoacr \
  --image crypto-generator:latest \
  Pipeline/generator \
  --file Pipeline/docker/Dockerfile.generator

# Deploy to ACI
az container create \
  --resource-group $RG \
  --name crypto-generator \
  --image thesiscryptoacr.azurecr.io/crypto-generator:latest \
  --registry-login-server thesiscryptoacr.azurecr.io \
  --registry-username $(az acr credential show --name thesiscryptoacr --query username -o tsv) \
  --registry-password $(az acr credential show --name thesiscryptoacr --query passwords[0].value -o tsv) \
  --environment-variables \
      USE_LOCAL_KAFKA=false \
      DATA_SOURCE=merged \
      SYMBOLS=BTC-USD,ETH-USD,SOL-USD,BNB-USD,XRP-USD \
      AZURE_KEYVAULT_NAME="thesis-crypto-kv" \
      AZURE_CLIENT_ID="$SP_CLIENT_ID" \
      AZURE_TENANT_ID="$SP_TENANT_ID" \
  --secure-environment-variables \
      AZURE_CLIENT_SECRET="$SP_CLIENT_SECRET" \
  --cpu 1 --memory 1.5 \
  --restart-policy Always \
  --location $LOCATION

# Check it's running
az container logs --resource-group $RG --name crypto-generator --follow
```

### 8.1 Verification checklist

- [ ] Container shows `Running` state in Azure portal
- [ ] Logs show `batch_sent` lines with growing `total`
- [ ] Event Hub incoming messages metric > 0 in Azure Monitor
- [ ] `RawPrices | count` in Fabric KQL grows without your Mac running the generator

---

## Quick-Start Summary

```bash
# 1. Clone / open project
cd "Tan Thesis/Pipeline"

# 2. Set up .env with Event Hub connection string
cp .env.example .env  # then fill in values

# 3. Create Azure resources
az login
bash infra/setup.sh   # or run the az CLI commands in Phase 2

# 4. Start data generator
docker compose -f docker/docker-compose.yml up --build

# 5. Open Fabric → run kql/01_bronze.kql, 02_silver.kql, 03_gold.kql

# 6. Verify: RawPrices | count  (should grow every few seconds)

# 7. Build dashboards in Fabric RTI Dashboard
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Generator exits immediately | Bad connection string | Check `.env` EVENTHUB_CONNECTION_STRING |
| `RawPrices \| count` stays at 0 | Eventstream not running | Check Eventstream status in Fabric |
| Silver table empty | Update policy error | Run `.show table CleanPrices policy update` and check errors |
| High latency (>5s) | Fabric capacity too small | Upgrade to F4 or scale Event Hub TUs |
| WebSocket disconnects | Coinbase rate limits | Reduce symbol count or add reconnect backoff |
| `PriceAggregates` stale | Materialized view backfill running | Wait — large backfills can take several minutes |
