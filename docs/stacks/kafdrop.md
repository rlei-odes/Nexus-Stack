---
title: "Kafdrop"
---

## Kafdrop

![Kafdrop](https://img.shields.io/badge/Kafdrop-000000?logo=apachekafka&logoColor=white)

**Lightweight Kafka/Redpanda web UI for browsing topics, consumer groups, and cluster health**

Kafdrop is a simple, fast web UI for monitoring Apache Kafka and Redpanda clusters. Features include:
- Topic listing with partition and replica details
- Message browsing with key/value deserialization
- Consumer group monitoring with lag tracking
- Schema Registry support (Avro, JSON Schema, Protobuf)
- Broker and cluster health overview
- Lightweight footprint (low memory usage)

| Setting | Value |
|---------|-------|
| Default Port | `8095` |
| Suggested Subdomain | `kafdrop` |
| Public Access | No (cluster management) |
| Source | [GitHub](https://github.com/obsidiandynamics/kafdrop) |

### Pre-configured Connection

Kafdrop is automatically configured to connect to the Redpanda cluster:
- **Bootstrap Servers:** `redpanda:9092`
- **Schema Registry:** `http://redpanda:8081`
