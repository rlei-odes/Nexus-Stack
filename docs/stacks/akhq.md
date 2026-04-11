---
title: "AKHQ"
---

## AKHQ

![AKHQ](https://img.shields.io/badge/AKHQ-000000?logo=apachekafka&logoColor=white)

**Kafka/Redpanda management GUI for topics, consumer groups, schema registry, and Connect**

AKHQ (formerly KafkaHQ) is a Kafka GUI for Apache Kafka and Redpanda that allows you to manage and monitor your clusters. Features include:
- Topic management (create, configure, delete)
- Real-time message browsing with search and filtering
- Consumer group monitoring with lag tracking
- Schema Registry browsing (Avro, JSON Schema, Protobuf)
- Kafka Connect management
- Node and ACL management
- Multi-cluster support

| Setting | Value |
|---------|-------|
| Default Port | `8094` |
| Suggested Subdomain | `akhq` |
| Public Access | No (cluster management) |
| Website | [akhq.io](https://akhq.io) |
| Source | [GitHub](https://github.com/tchiotludo/akhq) |

### Pre-configured Connection

AKHQ is automatically configured to connect to the Redpanda cluster:
- **Bootstrap Servers:** `redpanda:9092`
- **Schema Registry:** `http://redpanda:8081`

> Additional clusters can be added via the `AKHQ_CONFIGURATION` environment variable in the Docker Compose file.
