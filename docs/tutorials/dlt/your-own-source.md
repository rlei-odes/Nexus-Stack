---
title: "Write your own source with an LLM"
description: "Find a public API, point Claude at it, and have a working dlt pipeline running in minutes"
order: 5
---

# Write your own source with an LLM

The previous tutorials built pipelines by hand, which is fine for learning. In practice, dlt pipelines are one of the best things to generate with an LLM: the structure is always the same, the API response tells you everything you need to know about the schema, and the output is verifiable by running it. An LLM with tool access can fetch the endpoint itself, read the response, and write the resource — no manual JSON spelunking required.

This tutorial shows the workflow: where to find APIs, what to tell the LLM, and what to watch for in the generated code.

## Where to find public APIs: BundesAPI

[github.com/bundesAPI](https://github.com/bundesAPI) is a community collection of unofficial wrappers and documentation for German federal government APIs. Many are openly accessible with no authentication. A few worth exploring:

| Repo | What it exposes |
|---|---|
| `autobahn-api` | Road works, closures, and webcams on German motorways |
| `bundestag-api` | Parliamentary proceedings, votes, members |
| `jobsuche-api` | Federal Employment Agency job listings |
| `plz-api` | Postal codes with coordinates |
| `nina-api` | Civil protection warnings (floods, weather) |
| `deutschland` | Umbrella repo with links to many more |

Browse the org, open any repo's README, and look for the base URL and a few example endpoints. That's all you need to hand to an LLM.

## The workflow

### 1. Pick an endpoint and inspect it

Open the API's README and find a concrete endpoint. For the Autobahn API, the roads listing is:

```
GET https://verkehr.autobahn.de/o/autobahn/
```

Paste this URL into a browser or run it with curl to see what the response looks like:

```bash
curl -s https://verkehr.autobahn.de/o/autobahn/ | head -c 500
```

```json
{
  "roads": ["A1", "A2", "A3", "A4", ...]
}
```

The roadworks for a specific road:

```bash
curl -s "https://verkehr.autobahn.de/o/autobahn/A1/services/roadworks" | head -c 1000
```

```json
{
  "roadworks": [
    {
      "identifier": "...",
      "title": "Baustelle A1",
      "startTimestamp": "2025-01-15T06:00:00+01:00",
      "endTimestamp": "2025-08-31T18:00:00+01:00",
      "coordinate": { "lat": "53.5", "long": "9.9" },
      "extent": "..."
    }
  ]
}
```

Two minutes of curl gives you the full picture: field names, types, whether timestamps are ISO strings, whether there are nested objects.

### 2. Write the prompt

Open Claude (or any LLM with web/tool access) and give it all of this in one message. Be specific about what you want:

> I want to build a dlt pipeline that loads current roadworks from the German Autobahn API into a Postgres database.
>
> Base URL: `https://verkehr.autobahn.de/o/autobahn/`
>
> The API first requires a call to `/` to get a list of roads (returns `{"roads": ["A1", "A2", ...]}`). For each road, roadworks are at `/{road}/services/roadworks` (returns `{"roadworks": [{identifier, title, startTimestamp, endTimestamp, coordinate, ...}]}`).
>
> Please write a dlt resource that:
> - Iterates over all roads and fetches their current roadworks
> - Uses `write_disposition="replace"` (roadworks change daily, a full snapshot is fine)
> - Uses `identifier` as the primary key
> - Flattens `coordinate` into `lat` and `lon` columns rather than leaving it nested
>
> Target: `destination="postgres"`, `dataset_name="autobahn"`.
>
> Follow the same pattern as the other resources in this project: `@dlt.resource`, yield dicts, `dlt.sources.helpers.requests` for HTTP.

Key things to include in your prompt:
- The base URL and the shape of 1–2 sample responses
- Which field is the natural primary key
- Whether you want `merge` (incremental) or `replace` (full snapshot)
- Any transformations you want (flatten nested fields, rename columns, add constants)
- The destination and dataset name

### 3. Review the output

The LLM will produce something like:

```python
import dlt
from dlt.sources.helpers import requests
from typing import Iterator

BASE_URL = "https://verkehr.autobahn.de/o/autobahn"
HEADERS = {"accept": "application/json"}


@dlt.resource(
    name="roadworks",
    primary_key="identifier",
    write_disposition="replace",
)
def autobahn_roadworks() -> Iterator[dict]:
    roads_response = requests.get(f"{BASE_URL}/", headers=HEADERS)
    roads_response.raise_for_status()
    roads = roads_response.json().get("roads", [])

    for road in roads:
        rw_response = requests.get(
            f"{BASE_URL}/{road}/services/roadworks", headers=HEADERS
        )
        rw_response.raise_for_status()
        for item in rw_response.json().get("roadworks", []):
            coord = item.pop("coordinate", {})
            yield {
                **item,
                "road": road,
                "lat": coord.get("lat"),
                "lon": coord.get("long"),
            }


if __name__ == "__main__":
    pipeline = dlt.pipeline(
        pipeline_name="autobahn_pipeline",
        destination="postgres",
        dataset_name="autobahn",
    )
    info = pipeline.run(autobahn_roadworks())
    print(info)
```

**What to check before running:**

- Does it handle the case where an endpoint returns an empty list? (`roadworks` may be empty for quiet roads — the generator should just yield nothing and continue.)
- Does it handle HTTP errors gracefully? A single 404 for one road shouldn't crash the whole run. Ask the LLM to wrap the per-road request in a try/except if needed.
- Are timestamps stored as strings or converted? dlt will infer the type from the first record — ISO strings become `text` columns unless you parse them. That's usually fine.
- Does anything need a cursor? If you want only new records on the next run, you need `write_disposition="merge"` and a state cursor (see the [incremental loading tutorial](./wikipedia-incremental.md)).

Run it, check `print(info)` for errors, then inspect the table in CloudBeaver or psql. Iterate from there.

## Tips for agentic LLMs

If you're using Claude Code or another agent with tool access, you can skip the manual curl step entirely. A prompt like:

> Fetch `https://verkehr.autobahn.de/o/autobahn/` and `https://verkehr.autobahn.de/o/autobahn/A1/services/roadworks`, examine the response structure, and write a dlt resource that loads all roadworks into Postgres.

The agent will make the HTTP calls itself, read the response, identify the primary key, spot any nested structures, and write the resource with the right field names. You get a working script without opening a browser.

This works especially well when:
- The API docs are sparse or outdated but the endpoint itself is live
- The response has 20+ fields and you don't want to type them all out
- You want the agent to decide whether nested objects should be flattened or left for dlt to unpack into child tables

## What to do next with your data

You now have real-world data in Postgres. Here's where the rest of Nexus-Stack picks it up.

### Visualize in Grafana

Grafana connects to Postgres as a data source. Once connected, build a **Time series** panel over `wikipedia.pageviews` to plot monthly article views for each article side-by-side, or a **Gauge** panel showing the current Bodensee water level from `pegel_online.measurements`. Time-series data with a timestamp column lands directly in Grafana's native panel type with no transformation needed.

Open `https://grafana.<your-domain>` → **Connections → Add new data source → PostgreSQL**.

### Build dashboards in Superset or Metabase

For exploration and sharing, Superset and Metabase both connect to Postgres and let you build charts without writing SQL. Superset's SQL Lab is particularly useful while iterating — you can query `wikipedia.pageviews`, spot outliers, and turn the query directly into a saved chart.

Open `https://superset.<your-domain>` or `https://metabase.<your-domain>` → add a PostgreSQL database connection → start exploring.

### Model with dbt in Meltano

Raw API data rarely lands in the shape you want for analysis. dbt lets you define SQL transformations as versioned models. For the Pegel Online data, a useful first model joins `measurements` to `stations` and calculates daily averages:

```sql
-- models/pegel_daily_avg.sql
select
    s.shortname as station,
    date_trunc('day', m.timestamp::timestamptz) as day,
    avg(m.value_cm) as avg_water_level_cm,
    min(m.value_cm) as min_water_level_cm,
    max(m.value_cm) as max_water_level_cm
from pegel_online.measurements m
join pegel_online.stations s on s.uuid = m.station_id
group by 1, 2
```

Meltano bundles dbt and connects it to your Nexus Postgres. Open `https://meltano.<your-domain>` to get started.

### Schedule with Kestra

Running pipelines by hand doesn't scale. Kestra can trigger your dlt scripts on a schedule — daily incremental runs for measurements, a weekly stations refresh. Create a flow in the Kestra UI that runs `python pegel_online_pipeline.py` inside code-server via a Script task, or use Kestra's Python task runner directly.

Open `https://kestra.<your-domain>` → **Flows → New flow**.
