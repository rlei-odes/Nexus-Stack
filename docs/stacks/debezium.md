---
title: "Debezium"
---

## Debezium

![Debezium](https://img.shields.io/badge/Debezium-4E8CBF?logo=debezium&logoColor=white)

**Change data capture platform that streams database changes to Redpanda/Kafka in real time**

Debezium monitors database transaction logs and emits every INSERT, UPDATE, and DELETE as an event to Redpanda/Kafka. Features include:
- CDC for PostgreSQL, MySQL, MongoDB, SQL Server, and more
- Guaranteed event ordering with no changes missed
- Low latency (milliseconds from DB write to Kafka event)
- Runs as Kafka Connect with pre-installed Debezium connectors
- Schema history tracking and snapshot support

| Setting | Value |
|---------|-------|
| Default Port | `8097` (REST API, host) / `8083` (internal) |
| Access | Internal only (no web UI) |
| Manage via | Kafka-UI or AKHQ |
| Website | [debezium.io](https://debezium.io) |
| Source | [GitHub](https://github.com/debezium/debezium) |

### Pre-configured Connection

Debezium is automatically configured to use the Redpanda cluster as its message broker:
- **Bootstrap Servers:** `redpanda:9092`
- **Internal Topics:** `_debezium-configs`, `_debezium-offsets`, `_debezium-status`

### Managing Connectors

No separate UI needed - use the existing tools:
- **Kafka-UI** (`/kafka-connect` tab) - visual connector management
- **AKHQ** (`Connect` section) - visual connector management
- **REST API** - direct API calls

### Example: Create a PostgreSQL CDC Connector

```bash
curl -X POST http://localhost:8097/connectors \
  -H "Content-Type: application/json" \
  -d '{
    "name": "postgres-cdc",
    "config": {
      "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
      "database.hostname": "postgres",
      "database.port": "5432",
      "database.user": "nexus-postgres",
      "database.password": "<from-infisical>",
      "database.dbname": "postgres",
      "topic.prefix": "cdc",
      "plugin.name": "pgoutput"
    }
  }'
```
