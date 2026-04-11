---
title: "RisingWave"
---

## RisingWave

![RisingWave](https://img.shields.io/badge/RisingWave-0065FF?logoColor=white)

**PostgreSQL-compatible streaming database for real-time analytics**

RisingWave is a cloud-native streaming database that uses SQL to process streaming data. It provides a PostgreSQL-compatible interface for creating materialized views that are incrementally maintained as new data arrives. Built-in connectors for Kafka/Redpanda sources and sinks.

| Setting | Value |
|---------|-------|
| Default Port | `5691` (Dashboard UI) |
| PostgreSQL Port | `4566` (wire protocol) |
| Suggested Subdomain | `risingwave` |
| Public Access | No |
| Website | [risingwave.com](https://risingwave.com) |
| Source | [GitHub](https://github.com/risingwavelabs/risingwave) |

### Configuration

- **Default user:** `root` (no password in single-node mode)
- **Default database:** `dev`
- **Dashboard:** Web UI for monitoring streaming jobs at port 5691
- **PostgreSQL port:** 4566 for external clients (psql, Adminer, CloudBeaver, DBeaver, JDBC)
- **Metrics port:** 1250 (Prometheus, scraped by Grafana stack automatically)
- **Security:** In single-node mode, the PostgreSQL endpoint on port 4566 has no authentication. Do not expose this port to the public Internet; if you open firewall access, strictly allowlist trusted source IPs and keep the port closed otherwise.

### Usage

1. Enable the service in Control Plane
2. Access `https://risingwave.YOUR_DOMAIN` for the Dashboard UI
3. Connect via psql: `psql -h YOUR_SERVER -p 4566 -U root -d dev`
4. Connect via Adminer: Select PostgreSQL, server `risingwave:4566`, user `root`, database `dev`

### Monitoring

RisingWave exports Prometheus metrics on port 1250, which are automatically scraped by the Grafana Prometheus instance. The Dashboard UI at port 5691 uses these metrics to display cluster health, throughput, and streaming job performance.

### Redpanda Integration

RisingWave has built-in Kafka source/sink connectors. To consume from Redpanda:

```sql
CREATE SOURCE my_source (
  id INT,
  name VARCHAR,
  timestamp TIMESTAMPTZ
) WITH (
  connector = 'kafka',
  topic = 'my-topic',
  properties.bootstrap.server = 'redpanda:9092',
  scan.startup.mode = 'earliest'
) FORMAT PLAIN ENCODE JSON;

CREATE MATERIALIZED VIEW my_view AS
SELECT * FROM my_source WHERE id > 100;
```
