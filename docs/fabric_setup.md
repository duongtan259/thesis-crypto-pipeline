# Microsoft Fabric Setup Guide

Step-by-step instructions for setting up the Fabric Real-Time Intelligence layer.

**Tenant:** `duongphuctan250901gmail.onmicrosoft.com`
**Capacity:** `tandatadev` (F2, North Europe)
**Workspace:** `Realtime Intelligence`

---

## 1. Create Eventhouse

1. Go to Fabric workspace **Realtime Intelligence**
2. **+ New item** → **Eventhouse**
3. Name: `crypto-eventhouse`
4. Click **Create**

An Eventhouse automatically creates a KQL Database with the same name.

---

## 2. Run KQL Setup Scripts

Open the KQL Database (`crypto-eventhouse`) and run each script in order via the query editor.

### Step 1 — Bronze layer
Copy and run `kql/01_bronze.kql`:
- Creates `RawPrices` table
- Creates JSON ingestion mapping `RawPricesMapping`
- Sets 30-day hot cache policy

### Step 2 — Silver layer
Copy and run `kql/02_silver.kql`:
- Creates `CleanPrices` table
- Creates `SilverTransform()` function
- Attaches update policy: auto-runs on every `RawPrices` ingest

### Step 3 — Gold layer
Copy and run `kql/03_gold.kql`:
- Creates `PriceAggregates` materialized view (1-min OHLCV)
- Creates stored functions: `GetLatencyStats`, `GetThroughput`, `GetLatestPrices`, `GetMaterializedViewLag`

### Step 4 — Anomaly detection
Copy and run `kql/05_anomaly_detection.kql`:
- Creates `PriceAlerts` table
- Creates `DetectPriceSpikes`, `DetectVolumeSurges`, `GetVolatility` functions
- Creates `AlertSummary` materialized view

---

## 3. Create Eventstream

1. Workspace → **+ New item** → **Eventstream**
2. Name: `crypto-eventstream`
3. Click **Create**

### Add Event Hub source

1. Click **Add source** → **Azure Event Hubs**
2. Click **New connection**, fill in:

   | Field | Value |
   |-------|-------|
   | Event Hub namespace | `thesis-crypto-eh-ns.servicebus.windows.net` |
   | Event Hub | `crypto-prices` |
   | Shared Access Key Name | `fabric-listen-policy` |
   | Shared Access Key | *(get from `az eventhubs eventhub authorization-rule keys list`)* |

3. Consumer group: `$Default`
4. Data format: `Json`
5. Click **Next** → **Add**

### Add Eventhouse destination

1. Click **Transform events or add destination** → **Eventhouse**
2. Fill in:

   | Field | Value |
   |-------|-------|
   | Eventhouse | `crypto-eventhouse` |
   | KQL Database | `crypto-eventhouse` |
   | Destination table | `RawPrices` |
   | Input data format | `Json` |
   | Mapping | `RawPricesMapping` |

3. Click **Add**
4. Click **Publish** to activate the Eventstream

---

## 4. Verify End-to-End

Run these queries in the KQL Database after ~2 minutes:

```kql
// 1. Bronze — is data arriving?
RawPrices | count

// 2. Silver — is update policy working?
CleanPrices | take 5 | project symbol, price_usd, latency_ms, timestamp_utc

// 3. Gold — is materialized view populating?
PriceAggregates | take 5

// 4. Latency stats
GetLatencyStats(5m)

// 5. Throughput
GetThroughput(10m)

// 6. Latest prices
GetLatestPrices()

// 7. Anomaly detection
DetectPriceSpikes(1.0, 60s)
```

---

## 5. Auth Policies Summary

| Policy Name | Rights | Used By |
|-------------|--------|---------|
| `generator-policy` | Send | Python generator / ACI container |
| `fabric-listen-policy` | Listen | Fabric Eventstream |

Both are hub-level policies on `crypto-prices` (not namespace-level).

---

## 6. Pause Capacity When Not Testing

To avoid charges, pause the F2 capacity when done:

```bash
az fabric capacity update \
  --resource-group rg-thesis-fabric \
  --capacity-name tandatadev \
  --administration '{"members": []}' \
  --no-wait

# Or via Azure Portal:
# portal.azure.com → tandatadev → Pause
```

Resume before next session:
```bash
az fabric capacity resume \
  --resource-group rg-thesis-fabric \
  --capacity-name tandatadev
```
