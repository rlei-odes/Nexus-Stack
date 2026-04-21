---
title: "Parse JSON from a Kafka topic with a schema"
description: "Turn raw Kafka bytes into typed Spark columns — the step everyone needs but no one teaches clearly"
order: 3
---

# Parse JSON from a Kafka topic with a schema

When Spark reads from Kafka/Redpanda, `value` comes in as raw bytes. You have to parse it yourself. For JSON payloads — by far the most common format — this means defining a schema and using `from_json`. This tutorial covers the exact pattern.

About 10 minutes. After this, your Spark streaming DataFrames have real columns you can filter, aggregate, and join on.

## Prerequisites

- A Spark streaming read from a Kafka topic — see [Read a Redpanda topic from Spark](/docs/tutorials/spark/read-redpanda/)
- Events in the topic have a known JSON shape. Example used below: `{"sensor": "sensor-a", "reading": 42.5, "timestamp": 1713456789}`

## Why a schema is required

Spark's `from_json` needs to know the structure of the JSON at DataFrame-definition time (query-planning time), not at row-processing time. Two reasons:

- **Performance** — pre-knowing the schema lets Spark generate efficient parse code. Schema-on-read with `inferSchema` would mean parsing every row twice.
- **Correctness** — streaming queries can't change their output schema mid-run. Fixing the schema upfront makes that explicit.

Cost: a change in event shape requires a schema update in your code. Benefit: parsing errors surface fast, not as mysterious NULLs downstream.

## Define the schema

Use `StructType` for anything beyond a single field. The Python API is verbose but readable:

```python
from pyspark.sql.types import StructType, StructField, StringType, DoubleType, LongType

sensor_schema = StructType([
    StructField("sensor",    StringType()),
    StructField("reading",   DoubleType()),
    StructField("timestamp", LongType()),     # epoch seconds — producer emits `int(time.time())`
                                              # (a float from `time.time()` would parse to NULL under PERMISSIVE mode)
])
```

Alternatively, the DDL shortcut:

```python
sensor_schema = "sensor STRING, reading DOUBLE, `timestamp` BIGINT"
```

Both produce equivalent schemas. DDL is terser; `StructType` plays nicer with IDE autocomplete and nested types.

## Parse the Kafka value

Starting from the raw stream:

```python
raw = (
    spark.readStream
      .format("kafka")
      .options(**kafka_options)      # see the Read Redpanda tutorial
      .option("subscribe", "sensors")
      .load()
)
```

Parse:

```python
from pyspark.sql.functions import col, from_json

parsed = raw.select(
    col("timestamp").alias("kafka_ts"),           # when Kafka got the message
    col("partition"),
    col("offset"),
    from_json(col("value").cast("string"), sensor_schema).alias("data")
).select(
    "kafka_ts", "partition", "offset",
    "data.sensor",                                # lift nested fields to top level
    "data.reading",
    col("data.timestamp").alias("event_ts"),      # avoid name collision with kafka_ts
)

parsed.printSchema()
```

Expected:
```
root
 |-- kafka_ts: timestamp (nullable = true)
 |-- partition: integer (nullable = true)
 |-- offset: long (nullable = true)
 |-- sensor: string (nullable = true)
 |-- reading: double (nullable = true)
 |-- event_ts: long (nullable = true)
```

Now `parsed` is a streaming DataFrame with typed columns. Filter, groupBy, join — all the batch DataFrame API works.

## Sanity check with the memory sink

```python
query = (
    parsed.writeStream
      .format("memory")
      .queryName("sensors_parsed")
      .outputMode("append")
      .start()
)
```

```sql
%sql
SELECT kafka_ts, sensor, reading, event_ts
FROM sensors_parsed
ORDER BY kafka_ts DESC
LIMIT 10;
```

Rows with real columns. This is the whole payoff.

## Filtering and simple transformations

Because `parsed` is a proper DataFrame, filtering and projection are trivial:

```python
from pyspark.sql.functions import col

high_readings = (
    parsed
      .filter(col("reading") > 28.0)
      .select("kafka_ts", "sensor", "reading")
)

(high_readings.writeStream
  .format("memory")
  .queryName("high_only")
  .outputMode("append")
  .start())
```

## Windowed aggregation with a watermark

Now you have a real event-time column (`event_ts`). Use it for correct windowing that tolerates late arrivals:

```python
from pyspark.sql.functions import col, window, avg, count, timestamp_seconds

windowed = (
    parsed
      .withColumn("event_time", timestamp_seconds(col("event_ts")))  # epoch-seconds long → timestamp
      .withWatermark("event_time", "30 seconds")                     # accept events up to 30s late
      .groupBy(
          window("event_time", "1 minute"),
          col("sensor"),
      )
      .agg(
          count("*").alias("n"),
          avg("reading").alias("avg_reading"),
      )
)

(windowed.writeStream
  .format("memory")
  .queryName("per_minute")
  .outputMode("append")
  .start())
```

The **`withWatermark`** is the key new thing. It tells Spark: "any event with `event_time` more than 30 seconds behind the max seen so far is too late — drop it." This is what makes event-time aggregations finite in a streaming world.

Without a watermark, Spark can't emit `append`-mode aggregation output: it doesn't know when a window is "done". With a watermark, once the watermark crosses the end of a window + the allowed lateness, the window closes and emits.

### Watermark rule of thumb

- **Short watermark (few seconds):** tight, minimal memory, but drops events that genuinely arrive late.
- **Long watermark (minutes–hours):** tolerates long delays, but state memory grows and output emits later.

Pick based on your data's realistic arrival behavior. "How often do events arrive >N seconds after their event time?" → set watermark a bit beyond the 99th percentile of N.

## Malformed JSON

`from_json` returns **all-NULL** for rows that fail to parse (wrong shape, bad syntax, encoding issues). Easy to miss. Check:

```python
from pyspark.sql.functions import col

bad_rows = parsed.filter(col("sensor").isNull())

(bad_rows.writeStream.format("memory").queryName("bad").outputMode("append").start())
```

Any accumulation in `bad` means upstream producers are writing messages that don't match your schema. Fix producers or relax the schema.

For more granular error handling, `from_json` accepts an `options` dict with `mode`:
- `PERMISSIVE` (default) — malformed rows → NULL
- `FAILFAST` — malformed rows crash the query
- `DROPMALFORMED` — malformed rows silently dropped (rarely what you want)

Example:
```python
from_json(col("value").cast("string"), sensor_schema, {"mode": "FAILFAST"})
```

## Handling schema evolution

Schemas aren't static — eventually producers add a field, rename one, change a type. Strategies, worst to best:

- **Hard break:** bump the schema, restart all consumers. Smallest-brain-power, but you lose old data until restart.
- **Make new fields nullable:** `StructField("new_field", StringType(), nullable=True)`. `from_json` handles missing fields gracefully (emits NULL). Adding fields to the schema first, then the producer — makes roll-out forward-compatible.
- **Schema Registry (Avro/Protobuf):** proper schema versioning, compatibility checks. Out of scope for this tutorial but the right answer for production.

For the kind of prototyping you do on Nexus-Stack, **"make new fields nullable"** covers 95% of cases.

## Next steps

- [Write a Kafka stream to a Bronze Delta table](/docs/tutorials/spark/bronze-delta/) — persist the parsed rows
- [Spark Streaming 101](/docs/tutorials/spark/streaming-101/) — review the fundamentals
