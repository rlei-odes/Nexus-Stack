---
title: "Vector"
---

## Vector

![Vector](https://img.shields.io/badge/Vector-3B2F63?logo=vector&logoColor=white)

**High-performance observability pipeline for logs, metrics, and traces collection**

Vector is an open-source observability data pipeline built in Rust. It replaces tools like Fluentd, Logstash, and Filebeat. Features include:
- Collect logs, metrics, and traces from any source
- Transform data with VRL (Vector Remap Language)
- Route to multiple destinations simultaneously
- 80+ sources and sinks (Redpanda, ClickHouse, S3, Loki, Prometheus, etc.)
- 10x less memory than Logstash
- Built-in backpressure and at-least-once delivery

| Setting | Value |
|---------|-------|
| Access | Internal only (agent, no web UI) |
| Website | [vector.dev](https://vector.dev) |
| Source | [GitHub](https://github.com/vectordotdev/vector) |

### Default Configuration

Vector is pre-configured to collect Docker container logs and forward them to Loki. Edit `vector.yaml` on the server to customize:

```bash
# Edit config
ssh nexus "nano /opt/docker-server/stacks/vector/vector.yaml"

# Restart to apply
ssh nexus "cd /opt/docker-server/stacks/vector && docker compose restart"
```

### Example: Add Redpanda Sink

Add to `vector.yaml` to also stream logs to a Redpanda topic:

```yaml
sinks:
  redpanda:
    type: kafka
    inputs:
      - parse_logs
    bootstrap_servers: redpanda:9092
    topic: container-logs
    encoding:
      codec: json
```
