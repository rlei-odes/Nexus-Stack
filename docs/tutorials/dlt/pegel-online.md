---
title: "Two resources, one pipeline: Pegel Online"
description: "Load water-level measurements and station metadata from a public REST API into Postgres — two resources, two write strategies, chunked pagination, and automatic nested-JSON unpacking"
order: 4
---

# Two resources, one pipeline: Pegel Online

The Wikipedia tutorials used a single resource fetching from a single endpoint. Real-world pipelines usually need more: a primary time-series resource plus a metadata resource, pagination to handle large date ranges, and APIs that return nested JSON. This tutorial covers all of that with [Pegel Online](https://www.pegelonline.wsv.de/), a public German water-level monitoring service with a clean, no-auth REST API.

## What you'll build

Two resources in one pipeline:

| Resource | Strategy | Why |
|---|---|---|
| `measurements` | `merge` — incremental | High-frequency time-series; only fetch what's new |
| `stations` | `replace` — full snapshot | 700+ station records; stable reference data |

dlt loads them into the same Postgres schema. The stations resource returns nested JSON that dlt automatically unpacks into a child table — no extra code.

## Prerequisites

- [Setup complete](./setup.md)
- [Incremental loading tutorial](./wikipedia-incremental.md) completed — you understand `dlt.current.resource_state()` and `write_disposition="merge"`

## The script

Create `pegel_online_pipeline.py`:

```python
import dlt
from dlt.sources.helpers import requests
from datetime import datetime, timedelta, timezone
from typing import Iterator

STATION_ID = "aa9179c1-17ef-4c61-a48a-74193fa7bfdf"  # Lake Constance (Bodensee)
TIMESERIES = "W"                                        # Wasserstand (water level, cm)
INITIAL_START = "2024-01-01T00:00:00+00:00"
CHUNK_DAYS = 30

BASE_URL = "https://www.pegelonline.wsv.de/webservices/rest-api/v2"
HEADERS = {"accept": "application/json"}

_metrics = {"api_calls": 0, "records_received": 0, "start": "", "end": ""}


@dlt.resource(
    name="measurements",
    primary_key=["station_id", "timestamp"],
    write_disposition="merge",
)
def pegel_measurements(
    end: str = None,
    full_refresh: bool = False,
) -> Iterator[dict]:
    _metrics["api_calls"] = 0
    _metrics["records_received"] = 0

    if end is None:
        end = datetime.now(timezone.utc).isoformat()

    state = dlt.current.resource_state()
    last_timestamp = state.get("last_timestamp", INITIAL_START)

    start = INITIAL_START if full_refresh else last_timestamp
    _metrics["start"] = start
    _metrics["end"] = end

    start_dt = datetime.fromisoformat(start).astimezone(timezone.utc)
    end_dt = datetime.fromisoformat(end).astimezone(timezone.utc)
    max_ts_dt = start_dt

    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end_dt)
        url = (
            f"{BASE_URL}/stations/{STATION_ID}/{TIMESERIES}/measurements.json"
            f"?start={chunk_start.isoformat()}&end={chunk_end.isoformat()}"
        )
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        items = response.json()
        _metrics["api_calls"] += 1
        _metrics["records_received"] += len(items)

        for item in items:
            item_ts_dt = datetime.fromisoformat(item["timestamp"]).astimezone(timezone.utc)
            if item_ts_dt > max_ts_dt:
                max_ts_dt = item_ts_dt
            yield {
                "station_id": STATION_ID,
                "timeseries": TIMESERIES,
                "timestamp": item["timestamp"],
                "value_cm": item["value"],
            }

        chunk_start = chunk_end

    state["last_timestamp"] = max_ts_dt.isoformat()


@dlt.resource(
    name="stations",
    primary_key="uuid",
    write_disposition="replace",
)
def pegel_stations() -> Iterator[dict]:
    url = (
        f"{BASE_URL}/stations.json"
        "?includeTimeseries=true"
        "&includeCurrentMeasurement=false"
        "&includeCharacteristicValues=false"
    )
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    yield from response.json()


if __name__ == "__main__":
    import sys
    full_refresh = "--full-refresh" in sys.argv
    run_stations = "--stations" in sys.argv

    pipeline = dlt.pipeline(
        pipeline_name="pegel_online_pipeline",
        destination="postgres",
        dataset_name="pegel_online",
    )

    if run_stations:
        info = pipeline.run(pegel_stations())
        status = info.load_packages[0].state if info.load_packages else "N/A"
        print(f"\n{'='*42}")
        print(f"  Pegel Online — Stations Refresh")
        print(f"{'='*42}")
        print(f"  Load status:      {status}")
        print(f"{'='*42}\n")
    else:
        info = pipeline.run(pegel_measurements(full_refresh=full_refresh))

        load_id = info.load_packages[0].load_id if info.load_packages else None
        records_written = 0
        if load_id:
            with pipeline.sql_client() as client:
                with client.execute_query(
                    "SELECT count(*) FROM pegel_online.measurements WHERE _dlt_load_id = %s",
                    load_id,
                ) as cursor:
                    records_written = cursor.fetchone()[0]

        status = info.load_packages[0].state if info.load_packages else "N/A"
        print(f"\n{'='*42}")
        print(f"  Pegel Online — Run Summary")
        print(f"{'='*42}")
        print(f"  Date range:       {_metrics['start'][:10]} → {_metrics['end'][:10]}")
        print(f"  API calls made:   {_metrics['api_calls']}")
        print(f"  Records received: {_metrics['records_received']}")
        print(f"  Records written:  {records_written}")
        print(f"  Load status:      {status}")
        print(f"{'='*42}\n")
```

## Run it

**Step 1 — full refresh of measurements** (loads all 15-minute readings since January 2024):

```bash
python pegel_online_pipeline.py --full-refresh
```

```
==========================================
  Pegel Online — Run Summary
==========================================
  Date range:       2024-01-01 → 2026-05-10
  API calls made:   29
  Records received: 3047
  Records written:  3046
  Load status:      loaded
==========================================
```

29 API calls for 29 chunks of 30 days each. The API has a per-request size limit — chunking is what makes a 16-month backfill possible without timeouts.

**Step 2 — incremental run** (fetches only since the last measurement):

```bash
python pegel_online_pipeline.py
```

```
==========================================
  Pegel Online — Run Summary
==========================================
  Date range:       2026-05-10 → 2026-05-10
  API calls made:   1
  Records received: 2
  Records written:  2
  Load status:      loaded
==========================================
```

One call, a handful of new 15-minute readings. Run it again a few minutes later and you'll see a new record arrive — the cursor advances with each run.

**Step 3 — load station metadata** (one-off or periodic refresh):

```bash
python pegel_online_pipeline.py --stations
```

```
==========================================
  Pegel Online — Stations Refresh
==========================================
  Load status:      loaded
==========================================
```

## Verify the tables

Check what dlt created in the `pegel_online` schema:

```bash
psql -h postgres -U nexus-postgres -d postgres -c "\dt pegel_online.*"
```

```
    Schema    |         Name         | Type  |     Owner
--------------+----------------------+-------+----------------
 pegel_online | _dlt_loads           | table | nexus-postgres
 pegel_online | _dlt_pipeline_state  | table | nexus-postgres
 pegel_online | _dlt_version         | table | nexus-postgres
 pegel_online | measurements         | table | nexus-postgres
 pegel_online | stations             | table | nexus-postgres
 pegel_online | stations__timeseries | table | nexus-postgres
```

Six tables — note `stations__timeseries`. The stations API response looks like this (abbreviated):

```json
{
  "uuid": "aa9179c1-...",
  "shortname": "BODMAN-LUDWIGSHAFEN",
  "timeseries": [
    { "shortname": "W", "unit": "cm" },
    { "shortname": "WT", "unit": "°C" }
  ]
}
```

dlt sees the nested `timeseries` array and automatically creates a child table — `stations__timeseries` — with a `_dlt_parent_id` column that links back to the parent row in `stations`. No extra code, no flattening logic.

Row counts:

```bash
psql -h postgres -U nexus-postgres -d postgres \
  -c "SELECT count(*) FROM pegel_online.stations;" \
  -c "SELECT count(*) FROM pegel_online.stations__timeseries;"
```

```
 count
-------
   785

 count
-------
  1186
```

785 gauging stations across Germany, 1186 individual timeseries (each station can have water level, water temperature, flow rate, etc.).

## What's new in this pipeline

**Chunked pagination** — the measurements API returns at most a few hundred records per request and rejects very long date ranges. The while loop splits the full window into 30-day chunks and advances `chunk_start` until it reaches `end_dt`. A full refresh from January 2024 takes 29 requests instead of one huge one that would time out.

**Timezone-aware cursor** — the API returns CET/CEST timestamps that shift between `+01:00` and `+02:00` depending on the season. Each `item["timestamp"]` is parsed into a `datetime` and converted to UTC via `astimezone(timezone.utc)` before being compared to `max_ts_dt`, so the comparison stays in a stable timezone and remains monotonic across DST changes. The stored cursor (`state["last_timestamp"]`) is also written as a UTC ISO string for the same reason. The yielded `timestamp` field keeps the original API representation — only the internal bookkeeping is normalized, not the payload.

**Two resources, two strategies** — `pegel_measurements` uses `merge` because new readings arrive constantly and you only want to fetch what's new. `pegel_stations` uses `replace` because station metadata rarely changes and a full refresh is cheaper than tracking diffs for 785 stations. Both resources live in the same pipeline and write to the same schema.

**Schema enrichment** — the measurements endpoint returns only `{"timestamp": "...", "value": 47.0}`. The station ID and timeseries type aren't in the response, so the resource adds them during the yield: `"station_id": STATION_ID, "timeseries": TIMESERIES`. This is the standard pattern when the API puts context in the URL rather than the response body.

**Automatic nested JSON unpacking** — `pegel_stations` is six lines of code. It yields raw dicts straight from the API. dlt inspects the first record, finds the nested `timeseries` array, and creates `stations__timeseries` automatically. The double underscore (`__`) is dlt's naming convention for nested tables.

## Explore in CloudBeaver

Open `https://cloudbeaver.<your-domain>` and run a query that joins measurements to their station:

```sql
SELECT
    s.shortname,
    s.longname,
    m.timestamp,
    m.value_cm
FROM pegel_online.measurements m
JOIN pegel_online.stations s ON s.uuid = m.station_id
ORDER BY m.timestamp DESC
LIMIT 20;
```

Or browse all available timeseries for a station:

```sql
SELECT s.shortname, t.shortname AS timeseries, t.unit
FROM pegel_online.stations s
JOIN pegel_online.stations__timeseries t ON t._dlt_parent_id = s._dlt_id
ORDER BY s.shortname, t.shortname;
```

## What's next

Both pipelines so far were written by hand with a known API. The next tutorial — [Write your own source](./your-own-source.md) — shows a faster path: point an LLM at a public API endpoint, let it inspect the response, and generate the dlt resource for you.
