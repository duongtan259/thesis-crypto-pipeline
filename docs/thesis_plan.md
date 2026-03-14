# Thesis Plan
## Design and Implementation of a Real-Time ELT Pipeline Using Microsoft Fabric

| Field   | Details                          |
|---------|----------------------------------|
| Student | Tan Phuc Duong                   |
| Program | Master's Degree in Data Analytics |
| Date    | 18th of January 2026             |

---

## 1. Background and Motivation

Modern businesses need to analyze data as it's generated, not hours or days later. Traditional batch processing is insufficient when detecting fraud, monitoring equipment failures, or responding to customer behavior in real time. This has created demand for streaming data pipelines capable of handling continuous data flows from operational systems to analytics platforms.

Microsoft Fabric launched in late 2023 as a unified analytics platform combining data engineering, real-time analytics, and business intelligence. Unlike traditional approaches requiring multiple cloud services, Fabric provides an integrated environment built around a shared data lake called OneLake. The platform includes specific features for real-time data processing: Eventstream for stream ingestion, KQL Database for fast queries, and built-in integration with Power BI.

There is very little academic research on Fabric's real-time capabilities, mainly because the platform is so new. Companies are starting to adopt it — especially those already using Microsoft technologies — but there is limited guidance on building production-ready streaming pipelines with it.

---

## 2. Research Questions

### Main Research Question

> How can Microsoft Fabric Real-Time Intelligence be used to build a real-time streaming pipeline with medallion architecture for continuous, high-volume event data?

### Supporting Questions

- What is the best way to architect a multi-source streaming pipeline entirely within Fabric's Real-Time Intelligence feature?
- What end-to-end performance can realistically be achieved — in terms of latency, throughput, and reliability — under sustained load?
- What are the main technical challenges and how can they be solved?
- How does this approach compare with alternative real-time architectures (Databricks Structured Streaming, pure Kafka, Azure Stream Analytics)?
- What security and operational considerations are required to move from a prototype to a production-grade deployment?

---

## 3. System to Be Built

The thesis will produce a working end-to-end system implementing the following pipeline:

1. **Dual-source ingestion** — real-time cryptocurrency price ticks from the Coinbase Advanced Trade WebSocket API, enriched with market capitalisation data polled from the CoinGecko REST API every 30 seconds
2. **Stream transport** — events published to Azure Event Hub using the Kafka-compatible endpoint, with a Python generator running in Docker (locally) or Azure Container Instances (deployed)
3. **Real-time ingestion** — Fabric Eventstream connects Event Hub to the KQL Database with sub-second delivery
4. **Medallion architecture in KQL** — Bronze (raw append-only), Silver (validated + latency-tagged via update policy), Gold (1-minute OHLCV aggregates via materialized view) — no Spark or notebooks required
5. **Anomaly detection** — KQL stored functions detecting price spikes (>2% in 60s), volume surges (>3x baseline), and rolling volatility; classified LOW/MEDIUM/HIGH/CRITICAL
6. **Observability** — every event stores `latency_ms = ingestion_time − source_timestamp`, enabling p50/p95/p99 latency dashboards
7. **Load testing** — configurable stress tester measures maximum sustainable throughput at 100, 500, and 1000 events/sec
8. **Visualisation** — Fabric RTI Dashboard (sub-second refresh) and Power BI (DirectQuery, real-time push)

The system uses cryptocurrency data because it provides genuine 24/7 high-frequency streaming without requiring special permissions or enterprise infrastructure — while being directly analogous to financial monitoring pipelines common in enterprise settings.

---

## 4. Methodology and Timeline

The project is divided into four phases over approximately 20 weeks.

| Phase          | Duration | Tasks |
|----------------|----------|-------|
| **Setup**          | 4 weeks  | Configure Fabric workspace and Azure resources (Event Hub, Container Instances); build dual-source data generator (Coinbase WS + CoinGecko REST); verify local pipeline with Docker Kafka |
| **Implementation** | 6 weeks  | Build end-to-end streaming pipeline; implement Bronze/Silver/Gold medallion layers in KQL Database using update policies and materialized views; build anomaly detection functions; create RTI Dashboard and Power BI report |
| **Testing**        | 3 weeks  | Measure end-to-end latency (p50/p95/p99) at baseline, normal, high, and peak loads; run burst and sustained load tests; document findings in structured results tables |
| **Writing**        | 7 weeks  | Document architecture and design decisions (including security model and alternative comparisons); present test results and analysis; discuss practical implications and production hardening |

---

## 5. Significance and Expected Contributions

### Academic Contribution
This thesis will provide the first proper academic evaluation of Fabric's Real-Time Intelligence capabilities. The platform is too new for substantial research to exist yet, especially regarding the medallion architecture pattern within KQL Database.

### Industry Contribution
The thesis will deliver practical guidance for organisations considering Fabric for real-time analytics, including real performance benchmarks, architecture patterns using update policies, and an honest assessment of where the approach works well and where it struggles.

### Technical Contribution
A working reference implementation showing how to build real-time pipelines entirely within Fabric's Real-Time Intelligence feature, together with documented best practices discovered through the build process.

---

## 6. Expected Outcomes

Upon completion, the thesis will deliver:

- Complete architecture documentation showing the KQL Database medallion pattern with dual-source enrichment
- Working, reproducible code — Python generator, KQL scripts, Docker Compose, Azure deployment scripts
- Performance benchmarks: p50/p95/p99 latency and max throughput at multiple load levels
- Anomaly detection system demonstrating real business value beyond basic ingestion
- Comparison of Fabric's RTI approach against alternative architectures (Databricks, Azure Stream Analytics)
- Security architecture analysis: prototype vs production-grade hardening (OIDC, RBAC, VNet)
- Honest assessment of Fabric RTI limitations and when the approach is or is not appropriate

---

## 7b. Limitations and Scope Boundaries

The following are acknowledged scope limitations that will be discussed in Chapter 6:

| Limitation | Reason | Implication |
|------------|--------|-------------|
| Simplified auth (connection strings) | Azure account cost/complexity during development | Production would use Managed Identity + RBAC |
| Public API as data source, not on-premises DB | Avoids SQL Server licensing and CDC setup complexity | Functionally equivalent streaming pattern |
| Single Fabric capacity tier tested | Cost constraints of thesis budget | Results may differ on F8+ capacities |
| No multi-region failover | Out of scope for MVP | Noted as future work |
| CoinGecko free tier (30 req/min) | Sufficient for market cap enrichment only | Not suitable for high-frequency trading data |

---

## 7. Preliminary Chapter Outline

| Chapter | Title          | Content |
|---------|----------------|---------|
| 1       | Introduction   | Problem statement, objectives, and scope |
| 2       | Background     | Stream processing concepts and Microsoft Fabric platform overview |
| 3       | Design         | Architecture, component selection, and key design decisions — including security model (OIDC/Managed Identity, RBAC, VNet/Private Endpoints, protocol stack) |
| 4       | Implementation | Build process and technical challenges encountered |
| 5       | Evaluation     | Performance testing, results, and analysis |
| 6       | Discussion     | Interpretation, recommendations, and limitations — including production hardening steps vs. thesis prototype tradeoffs |
| 7       | Conclusions    | Summary, contributions, and future work |

---

## 8. Security and Enterprise Considerations

The thesis prototype uses simplified auth (connection strings, public endpoints) to focus on pipeline architecture. A production deployment would apply the following hardening, which will be documented in Chapter 3 and discussed in Chapter 6:

| Concern | Thesis implementation | Production |
|---------|----------------------|------------|
| Authentication | Service Principal + Key Vault | Azure Managed Identity + OIDC (no credentials at all) |
| Secrets storage | Azure Key Vault (no `.env` in Azure) | Key Vault with customer-managed keys |
| Authorization | RBAC per component | RBAC — least privilege per component |
| Network | Public Event Hub endpoint | VNet + Private Endpoints, firewall IP whitelist |
| Protocol (generator → Event Hub) | Kafka protocol over TLS | Same, or AMQP for native Azure clients |
| Encryption at rest | Azure default AES-256 | AES-256, customer-managed keys via Key Vault |

**Key concepts relevant to the thesis design chapter:**
- **OIDC (OpenID Connect)** — token-based identity standard; Azure AD issues short-lived JWTs to containerised workloads via Managed Identity, replacing passwords and connection strings
- **AMQP (Advanced Message Queuing Protocol)** — Event Hub's native wire protocol; Fabric Eventstream uses this internally; the generator uses the Kafka-compatible endpoint as a design choice that keeps the code portable
- **Private Endpoint** — Azure network feature that gives a service a private IP inside a VNet, so traffic between components never traverses the public internet
- **RBAC** — each component is granted only the minimum role needed (e.g. generator = Sender only, Fabric = Receiver only)
