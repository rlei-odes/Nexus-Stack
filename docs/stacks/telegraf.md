---
title: "Telegraf"
---

## Telegraf

![Telegraf](https://img.shields.io/badge/Telegraf-22ADF6?logo=influxdb&logoColor=white)

**Metrics collection agent with 300+ plugins for system, database, and application monitoring**

Telegraf is an open-source server agent for collecting, processing, and writing metrics. Features include:
- 300+ input plugins (system stats, Docker, databases, SNMP, APIs, etc.)
- Output to Prometheus, ClickHouse, Kafka/Redpanda, PostgreSQL, S3, and more
- Processor plugins for filtering, aggregating, and transforming data
- Minimal memory footprint (single Go binary)
- Plugin-driven architecture

| Setting | Value |
|---------|-------|
| Access | Internal only (agent, no web UI) |
| Prometheus Metrics | `http://telegraf:9273/metrics` (internal) |
| Website | [influxdata.com/telegraf](https://www.influxdata.com/time-series-platform/telegraf/) |
| Source | [GitHub](https://github.com/influxdata/telegraf) |

### Default Configuration

Telegraf is pre-configured to collect system metrics (CPU, memory, disk, network) and Docker container metrics, exposed as Prometheus metrics on port 9273. Edit `telegraf.conf` on the server to customize:

```bash
# Edit config
ssh nexus "nano /opt/docker-server/stacks/telegraf/telegraf.conf"

# Restart to apply
ssh nexus "cd /opt/docker-server/stacks/telegraf && docker compose restart"
```

### Grafana Integration

Add Telegraf as a Prometheus data source in Grafana to visualize collected metrics. The Prometheus endpoint is available at `http://telegraf:9273/metrics` within the Docker network.
