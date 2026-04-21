---
title: "Read a Redpanda topic from Spark Structured Streaming"
description: "Connect Databricks Spark to Redpanda running on Nexus-Stack — SASL auth, public bootstrap URL, and the first readStream"
order: 2
---

# Read a Redpanda topic from Spark Structured Streaming

Your Nexus-Stack runs Redpanda. Your Spark runs in Databricks. This tutorial connects them — Spark reads events from a Redpanda topic the same way it would from any Kafka cluster. The new piece compared to [Spark Streaming 101](/docs/tutorials/spark/streaming-101/) is **authentication**: Nexus-Stack exposes Redpanda publicly with SASL, so Spark needs credentials to connect.

## Prerequisites

- Nexus-Stack deployed with `redpanda`, `redpanda-console`, `infisical` enabled
- **Public Redpanda access enabled** — in the Control Plane → [Firewall](/docs/guides/user-guides/firewall/) page, open the Redpanda `kafka-public` TCP port. This exposes Redpanda's Kafka API on a public endpoint with SASL auth.
- A Databricks workspace (Free Edition works)
- A running notebook attached to a cluster

## Where credentials come from

When Nexus-Stack provisions Redpanda with public access, it auto-generates a SASL user and stores everything in Infisical. Open `https://infisical.<your-domain>` → project `nexus` → folder `/redpanda` to find:

| Key | What it is |
|---|---|
| `REDPANDA_KAFKA_PUBLIC_URL` | Public bootstrap URL (`host:port`) |
| `REDPANDA_SASL_USERNAME` | SASL user (auto-generated, not `admin`) |
| `REDPANDA_SASL_PASSWORD` | SASL password |

Copy these three values — you'll paste them into the notebook below.

## Option A: Notebook widgets (simple)

For exploration, put the credentials in notebook widgets. Not secure for production (widgets appear in run history), but easiest for learning.

```python
dbutils.widgets.text("BOOTSTRAP", "", "Bootstrap URL (host:port)")
dbutils.widgets.text("USERNAME",  "", "SASL Username")
dbutils.widgets.text("PASSWORD",  "", "SASL Password")
```

Run the cell. Three input fields appear at the top of the notebook — paste your values from Infisical into them.

```python
BOOTSTRAP = dbutils.widgets.get("BOOTSTRAP").strip()
USERNAME  = dbutils.widgets.get("USERNAME").strip()
PASSWORD  = dbutils.widgets.get("PASSWORD").strip()

assert BOOTSTRAP and USERNAME and PASSWORD, "Fill all three widgets first."
```

## Option B: Databricks Secret Scope (proper)

For anything you'd run unattended, use a secret scope instead. Create it once from your local terminal:

```bash
databricks secrets create-scope nexus

# Run each put-secret call without --string-value. The CLI opens your
# $EDITOR for secret entry — the value is never on the command line,
# never in your shell history, and never in /proc/<pid>/cmdline.
databricks secrets put-secret nexus REDPANDA_BOOTSTRAP
databricks secrets put-secret nexus REDPANDA_USERNAME
databricks secrets put-secret nexus REDPANDA_PASSWORD
```

> **Why not `--string-value`?** That flag puts the secret value on the command line, which ends up in shell history (`~/.bash_history`, `~/.zsh_history`) and in `ps`/process-listing output. Fine for throwaway test values, not for real credentials. The editor-based flow above is the safest option; alternatives are `--string-value-file path/to/file` if you've already written the secret to a file.

Then in the notebook:

```python
BOOTSTRAP = dbutils.secrets.get("nexus", "REDPANDA_BOOTSTRAP")
USERNAME  = dbutils.secrets.get("nexus", "REDPANDA_USERNAME")
PASSWORD  = dbutils.secrets.get("nexus", "REDPANDA_PASSWORD")
```

Values are never printed in output, never in run history. Use this for anything beyond experimentation.

## Verify connectivity first

Before the full streaming read, confirm Spark can even reach Redpanda. Smallest possible test.

**Install `confluent-kafka` as the very first cell in the notebook — before anything else.** `dbutils.library.restartPython()` wipes every Python variable in the notebook state, so it must run before you set `BOOTSTRAP` / `USERNAME` / `PASSWORD`:

```python
%pip install -q confluent-kafka
dbutils.library.restartPython()
```

After the restart, go back and re-run the credentials cells (widgets or `dbutils.secrets.get` — whichever Option you picked) so the three variables exist again. Then run the connectivity test:

```python
from confluent_kafka.admin import AdminClient

admin = AdminClient({
    "bootstrap.servers": BOOTSTRAP,
    "security.protocol": "SASL_PLAINTEXT",
    "sasl.mechanism":    "SCRAM-SHA-256",
    "sasl.username":     USERNAME,
    "sasl.password":     PASSWORD,
})
md = admin.list_topics(timeout=10)
print(f"Connected to: {md.orig_broker_name}")
print(f"Topics:       {sorted(md.topics.keys())}")
```

If this lists your topics, Spark will too. If it times out or auth-fails, debug here (easier error messages) before touching Spark.

> **Ordering tip:** the cleanest notebook layout is (1) `%pip install + restartPython`, (2) credentials cell, (3) connectivity test, (4) streaming read. After the restart in step 1 you never need to worry about variable loss again. The Kafka connector for Spark itself is built into Databricks Runtime and needs no pip install.

## First readStream

Now the actual Spark query. The Kafka connector is built into Databricks Runtime — no `%pip install` needed for this.

```python
kafka_options = {
    "kafka.bootstrap.servers":        BOOTSTRAP,
    "kafka.security.protocol":        "SASL_PLAINTEXT",
    "kafka.sasl.mechanism":           "SCRAM-SHA-256",
    "kafka.sasl.jaas.config":
        f'org.apache.kafka.common.security.scram.ScramLoginModule required '
        f'username="{USERNAME}" password="{PASSWORD}";',
    "subscribe":                      "sensors",      # or any topic you have
    "startingOffsets":                "latest",       # or "earliest" to replay
}

raw = (
    spark.readStream
      .format("kafka")
      .options(**kafka_options)
      .load()
)

raw.printSchema()
```

Expected schema output:

```
root
 |-- key: binary (nullable = true)
 |-- value: binary (nullable = true)
 |-- topic: string (nullable = true)
 |-- partition: integer (nullable = true)
 |-- offset: long (nullable = true)
 |-- timestamp: timestamp (nullable = true)
 |-- timestampType: integer (nullable = true)
```

**Everything is binary at this stage.** `key` and `value` are the raw bytes Kafka stored. To see them as text or parsed JSON, you transform — covered in [Parse JSON from a Kafka topic with a schema](/docs/tutorials/spark/parse-json-schema/).

## See some data

Cast the value to string and dump to a memory sink so you can SELECT it:

```python
from pyspark.sql.functions import col, expr

events = raw.select(
    col("timestamp"),
    col("partition"),
    col("offset"),
    expr("cast(key as string)")   .alias("key"),
    expr("cast(value as string)") .alias("value"),
)

query = (
    events.writeStream
      .format("memory")
      .queryName("redpanda_events")
      .outputMode("append")
      .start()
)
```

Produce some events into `sensors` (from code-server — see [Send your first event with a Python producer](/docs/tutorials/redpanda/first-producer/) — or any other source). Then:

```sql
%sql
SELECT timestamp, partition, offset, key, value
FROM redpanda_events
ORDER BY timestamp DESC
LIMIT 20;
```

New rows appear as events arrive.

## Stop and clean up

```python
query.stop()
```

Spark's Kafka source commits offsets to a Kafka consumer group named `spark-kafka-source-<uuid>` (random per query). That group shows up in Redpanda Console → Consumer Groups. It's normal; it's how Spark tracks what it's read.

## The config keys that matter

- **`kafka.bootstrap.servers`** — public Redpanda URL (with port)
- **`kafka.security.protocol = SASL_PLAINTEXT`** — SASL authentication over plain TCP. **No transport encryption**: all Kafka traffic, including message payloads, goes over the network in plaintext. SCRAM-SHA-256 protects the password itself (the password never crosses the wire — only a salted hash challenge does), but not the messages you send afterward. For encryption in transit use `SASL_SSL`, which requires additional certificate setup on the broker and the client.
- **`kafka.sasl.mechanism = SCRAM-SHA-256`** — matches how Nexus-Stack provisions the user. Don't change this.
- **`kafka.sasl.jaas.config`** — Java-style config string embedding username and password. The exact form is important; copy the template above.
- **`subscribe`** — topic name(s). Comma-separated for multiple topics. Use `subscribePattern` for regex.
- **`startingOffsets`** — `"earliest"` (replay everything) or `"latest"` (only new). Has no effect after the first run unless you change `checkpointLocation`.

## Common errors

**`TimeoutException: Failed to construct kafka consumer`** — Spark can't reach Redpanda. Check the bootstrap URL, check the Firewall page is actually open, check public access is enabled.

**`SaslAuthenticationException: Authentication failed`** — username or password wrong. Test via `confluent-kafka`'s `AdminClient` first (above).

**`OffsetOutOfRangeException`** — your checkpoint has offsets for messages that have been deleted due to retention. Either increase retention, or drop the checkpoint and start fresh (use a new `checkpointLocation`).

**Reading works, but output is empty** — `startingOffsets=latest` and nothing new has been produced since the query started. Produce some events.

## Next steps

- [Parse JSON from a Kafka topic with a schema](/docs/tutorials/spark/parse-json-schema/) — turn `value` bytes into typed columns
- [Write a Kafka stream to a Bronze Delta table](/docs/tutorials/spark/bronze-delta/) — persist what you're reading
