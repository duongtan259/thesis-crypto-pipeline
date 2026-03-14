# Microsoft Fabric Setup Guide

Step-by-step instructions for setting up the Fabric Real-Time Intelligence layer.

**Tenant:** `duongphuctan250901gmail.onmicrosoft.com`
**Capacity:** `tandatadev` (F2, North Europe)
**Workspace:** `Realtime Intelligence`

---

## 1. Create Eventhouse

1. Fabric workspace **Realtime Intelligence** → **+ New item** → **Eventhouse**
2. Name: `crypto_eventhouse`
3. Click **Create**

This auto-creates a KQL Database named `crypto_db` inside the Eventhouse.

---

## 2. Create Eventstream

1. Workspace → **+ New item** → **Eventstream**
2. Name: `crypto-eventstream` → **Create**

### Add Event Hub source

Click **Add source** → **Azure Event Hubs** → **New connection**:

| Field | Value |
|-------|-------|
| Event Hub namespace | `thesis-crypto-eh-ns.servicebus.windows.net` |
| Event Hub | `crypto-prices` |
| Shared Access Key Name | `fabric-listen-policy` |
| Shared Access Key | *(get from Azure Portal → Event Hub → fabric-listen-policy → Connection string)* |
| Consumer group | `$Default` |
| Data format | `Json` |

Click **Next** → **Add**.

### Add Eventhouse destination

Click **Transform events or add destination** → **Eventhouse**:

| Field | Value |
|-------|-------|
| Destination name | `price_raw` |
| Workspace | `Realtime Intelligence` |
| Eventhouse | `crypto_eventhouse` |
| KQL Database | `crypto_db` |
| KQL Destination table | `price_raw` (create new) |
| Input data format | `Json` |

Click **Save** → **Publish**.

> Fabric auto-creates the `price_raw` table when the Eventstream starts ingesting.

---

## 3. Run KQL Setup Scripts

Open `crypto_db` in the KQL query editor and run each script in order.

### Step 1 — Bronze mapping
Run `kql/01_bronze.kql`:
- Adds `RawPricesMapping` JSON mapping to `price_raw`
- Sets 30-day hot cache policy

### Step 2 — Silver layer
Run `kql/02_silver.kql`:
- Creates `price_silver` table
- Creates `SilverTransform()` function
- Attaches update policy → auto-runs on every `price_raw` ingest
- Backfill: `.set-or-append price_silver <| SilverTransform()`

### Step 3 — Gold layer
Run `kql/03_gold.kql`:
- Creates `price_gold` materialized view (1-min OHLCV per symbol)
- Creates helper functions: `GetLatencyStats`, `GetThroughput`, `GetLatestPrices`, `GetMaterializedViewLag`

### Step 4 — Anomaly detection
Run `kql/05_anomaly_detection.kql`:
- Creates `price_alerts` table
- Creates `DetectPriceSpikes`, `DetectVolumeSurges`, `GetVolatility` functions

---

## 4. Build RTI Dashboard

Workspace → **+ New item** → **Real-Time Dashboard** → `Crypto Live Dashboard`

Add data source → `crypto_db`. Add tiles:

| Tile | Query | Chart type |
|------|-------|-----------|
| BTC-USD Price (1-min) | `price_gold \| where symbol=="BTC-USD" \| order by window_start asc \| project window_start, close` | Line chart |
| Throughput | `price_silver \| where timestamp_utc > ago(30m) \| summarize events_per_min=count() by bin(timestamp_utc,1m) \| order by timestamp_utc asc` | Line chart |
| End-to-End Latency | `price_silver \| where timestamp_utc > ago(30m) \| summarize p50=percentile(toreal(latency_ms),50), p95=percentile(toreal(latency_ms),95) by bin(timestamp_utc,1m) \| order by timestamp_utc asc` | Line chart |
| Live Prices | `price_silver \| summarize arg_max(timestamp_utc, price_usd) by symbol \| project symbol, price_usd, timestamp_utc \| order by symbol asc` | Table |
| Price Spike Alerts | `DetectPriceSpikes(1.0, 60s)` | Table |
| Volatility Ranking | `GetVolatility(1h)` | Table |

Set **Auto refresh: 30 seconds**.

---

## 5. Verify End-to-End

Run `kql/04_verify.kql` in the KQL editor:

```kql
price_raw | count          // Bronze — data arriving?
price_silver | count       // Silver — update policy working?
price_gold | count         // Gold — materialized view populated?
GetLatencyStats(5m)        // Latency p50/p95/p99
DetectPriceSpikes(1.0, 60s) // Anomaly detection
```

---

## 6. Auth Policies Summary

| Policy | Rights | Used By |
|--------|--------|---------|
| `generator-policy` | Send | Python generator / ACI container |
| `fabric-listen-policy` | Listen | Fabric Eventstream source |

Both are hub-level policies on `crypto-prices` event hub.

---

## 7. Fabric Resources Summary

| Resource | Name | Type |
|----------|------|------|
| Workspace | `Realtime Intelligence` | Fabric workspace |
| Eventhouse | `crypto_eventhouse` | Eventhouse |
| KQL Database | `crypto_db` | KQL Database |
| Eventstream | `crypto-eventstream` | Eventstream |
| Dashboard | `Crypto Live Dashboard` | RTI Dashboard |

---

## 8. Pause Capacity When Not Testing

Via Azure Portal: search `tandatadev` → **Pause**

Or CLI:
```bash
# Pause
az rest --method post \
  --url "https://management.azure.com/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/rg-thesis-fabric/providers/Microsoft.Fabric/capacities/tandatadev/suspend?api-version=2023-11-01"

# Resume
az rest --method post \
  --url "https://management.azure.com/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/rg-thesis-fabric/providers/Microsoft.Fabric/capacities/tandatadev/resume?api-version=2023-11-01"
```
