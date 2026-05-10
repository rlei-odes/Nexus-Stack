---
title: "dlt"
description: "Ingest data from public APIs into Postgres using dlt"
order: 1
---

# dlt tutorials

**dlt** (Data Load Tool) is an open-source Python library for loading data from public APIs, databases, and files into Postgres. You run it directly inside `code-server` — no extra container, no deployment step.

## Tutorials

1. **[Setup: environment and dependencies](./setup.md)** — Create a `uv` virtualenv, install `dlt[postgres]`, and wire up your Nexus Postgres credentials.
2. **[Wikipedia pageviews pipeline](./wikipedia-pipeline.md)** — Your first pipeline: a `@dlt.resource` generator, `pipeline.run()`, and automatic schema inference.
3. **[Incremental loading with state](./wikipedia-incremental.md)** — Add a cursor so each run only fetches months that aren't in Postgres yet.
4. **[Two resources, one pipeline: Pegel Online](./pegel-online.md)** — Chunked pagination, mixed write strategies, and automatic nested-JSON unpacking into a child table.
5. **[Write your own source](./your-own-source.md)** — Find a public API, point an LLM at it, and generate a working dlt pipeline in minutes.
