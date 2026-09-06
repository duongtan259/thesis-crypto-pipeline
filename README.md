# Microsoft Fabric RTI thesis artefact

This repository contains the implementation and evaluation package for Phuc Tan Duong's master's thesis on a real-time ELT pipeline using Microsoft Fabric Real-Time Intelligence.

The thesis manuscript itself is not published here. This repository holds the code, infrastructure, KQL, tests and machine-readable evidence only.

The demonstrated path is:

```text
Coinbase WebSocket -> Python generator -> Azure Event Hub -> Fabric Eventstream
    -> KQL Bronze (price_raw) -> Silver (price_silver) -> Gold (price_gold)
```

Gold is a deterministic one-minute price-candle and operational-metrics layer. The Coinbase ticker field `volume_24h` is a rolling snapshot, so the implementation carries the latest snapshot per window; it does not claim trade-volume OHLCV.

## Evidence-backed results

- Controlled batch experiment: one shared market stream, 18,000 common event identifiers in each arm. At batch size 50, pre-broker generator latency was p50 2,330 ms, p95 5,529 ms, and p99 6,973 ms. Batch size 1 reduced p50 to 66 ms.
- A model calibrated on the batch-size-one arm predicted the held-out mean latencies for batch sizes 10, 50, and 100 within 0.7%. Reported mean intervals use Newey–West HAC errors over batch means.
- Nine corrected 60-second local Kafka load runs acknowledged all 288,000 scheduled events at mean rates of 99.9, 499.4, and 998.9 events/s. This proves broker acknowledgement only, not Eventstream or Fabric delivery.
- Controlled Azure-to-Fabric validation reconciled all 16,000 scheduled event identifiers in both Bronze and Silver across 100, 500, and 1,000 events/s runs, with zero missing sequences. At 1,000 events/s the combined serialisation-to-Bronze-commit boundary was p50 1,170 ms, p95 1,811 ms, and p99 2,007 ms. See [`cloud_e2e_validation_20260905.json`](scripts/results/cloud_e2e_validation_20260905.json).
- Kusto reproduction: 18,089 distinct event identifiers reconciled exactly between Bronze and Silver, with zero anti-join gaps.
- Repaired Gold view: open, high, low, close, and latest rolling-volume snapshot were correct in all 155 windows; a rebuild from unchanged Silver input produced the same SHA-256 projection.
- The prior `take_any` Gold design and older load-test reports are retained as superseded evidence, not current conclusions.

Component-by-component Fabric latency, Gold/dashboard refresh latency, and the Fabric capacity ceiling were not measured. The thesis does not present the generator metric as end-to-end Fabric latency or the local Kafka test as Fabric scalability.

## Repository map

```text
generator/                 Coinbase source, event model, Kafka/Event Hub publishers
kql/                       Bronze, Silver, deterministic Gold, verification, alerts
infra/main.bicep           Azure VNet, identity, Event Hub, Key Vault, private endpoints
docker/                    Local Kafka development stack and generator image
scripts/                   Capture, load, analysis and Kusto reproduction programs
scripts/results/           Machine-readable evidence: capture manifests, experiment
                           reports, load-test runs, Kusto reproductions, and the
                           5 and 6 September cloud validation records
tests/                     Regression and contract tests
docs/architecture.md       Pipeline architecture notes
docs/fabric_setup.md       Manual Fabric control-plane steps
```

## Local setup

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r generator/requirements.txt pytest ruff numpy matplotlib
docker compose -f docker/docker-compose.yml up -d zookeeper kafka
.venv/bin/pytest -q
.venv/bin/ruff check generator scripts tests
```

Run the live generator locally:

```bash
cp .env.example .env
docker compose -f docker/docker-compose.yml --profile local up generator-local
```

## Reproduce the measurements

Capture one stream into concurrent batch-size arms:

```bash
.venv/bin/python scripts/capture_latency.py --duration 1800 \
  --batch-size 1 --batch-size 10 --batch-size 50 --batch-size 100
.venv/bin/python scripts/analyse_experiment.py
```

Run a broker-acknowledgement load test:

```bash
.venv/bin/python scripts/load_test.py --eps 1000 --duration 60 --target kafka
```

Reproduce the KQL logic after starting Kustainer on port 8080:

```bash
.venv/bin/python scripts/kql_reproduction.py \
  --capture scripts/results/capture_batch50_20260905_094315.jsonl
```

The large raw JSON-lines captures are retained outside Git. Canonical reports contain their SHA-256 values; a third party without those files can repeat the method on a new live capture but cannot reconstruct the exact historical sample.

## Azure and Fabric deployment boundary

`infra/main.bicep` defines the Azure network, managed identity, Event Hub, Key Vault, private endpoints, and DNS links. The deployment workflow expects an existing Azure Container Registry, `AcrPull` assignment, and GitHub OIDC federation. It is manual-only so that pushing code cannot restart the always-running ACI resource after a planned cost shutdown.

[`docs/fabric_setup.md`](docs/fabric_setup.md) records the demonstration's temporary public-endpoint/SAS connection and dashboard steps. A private Eventstream source additionally requires a Fabric managed private endpoint and Azure approval as described in [Microsoft's Eventstream guidance](https://learn.microsoft.com/en-us/fabric/real-time-intelligence/event-streams/set-up-private-endpoint); that Fabric-side path was not deployed in this study.

The repaired source uses managed identity for the generator and private Azure networking. That path has static contract tests but was not deployed live during the final repair; it must be validated in an Azure/Fabric environment before production use.

## Event and alert semantics

`latency_ms = ingestion_time - timestamp_utc` is stamped before broker publication. It covers exchange-to-generator transit, parsing, buffer wait, and serialization—not broker acknowledgement or Fabric ingestion.

```kql
DetectPriceSpikes(0.5, 60s)
DetectVolume24hSnapshotChange(2.0, 10m)
GetVolatility(1h)
```

`DetectPriceSpikes` uses its supplied live lookback. The reproduction harness validates 60-second and 120-second behavior with controlled recent events. These functions are monitoring examples, not trading advice.
