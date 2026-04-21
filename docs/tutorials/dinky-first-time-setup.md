---
title: "Dinky first-time setup: register your Flink cluster"
description: "The one-time config step that makes Dinky talk to the Flink JobManager — where everyone gets stuck on first login"
order: 13
---

# Dinky first-time setup: register your Flink cluster

**Dinky** is a web-based SQL IDE for Apache Flink. On a fresh Nexus-Stack deployment, Dinky and Flink are both running — but Dinky doesn't automatically know that the Flink JobManager exists. You have to tell it, once. This tutorial is that one step.

Without this, every SQL query in Dinky fails with "no cluster available" — the most common first-login complaint.

## Prerequisites

- Nexus-Stack with `flink` and `dinky` enabled in the Control Plane → [Stacks](/docs/guides/user-guides/stacks/) page
- Both stacks showing green / deployed

## Log in to Dinky

Navigate to `https://dinky.<your-domain>`. Cloudflare Access sends an OTP on first visit.

Inside Dinky:

- **Username:** `admin`
- **Password:** **on first login**, you're prompted to **set a password**. Choose anything reasonable and write it down — this is a separate credential from Cloudflare Access.

You land on the Dinky home dashboard.

## Register the Flink cluster

Left nav → **Registration Center** → **Cluster** → **Flink Instance**.

Click **Add** (top right). A form opens with ~10 fields. Only two matter:

| Field | Value |
|---|---|
| **Name** | Any descriptive name — `nexus-flink` is a good default |
| **JobManager HA Address** | `http://flink-jobmanager:8081` |

Leave everything else on default. `Enabled` should be on (the default).

Click **Save**.

## Verify

Back in the **Flink Instance** list, your new entry appears with a **Status** column. It should read **Normal** (green check). If it says **Abnormal** (red X), see Troubleshooting below.

Click the entry to see version info — Dinky fetches it from the JobManager on activation.

## Test with a minimal query

To confirm end-to-end wiring, go to **Data Studio** (left nav). Create a new task:

- Click **+** (new task)
- **Task Type:** `FlinkSQL`
- **Name:** `hello-world`

In the task's config panel (top right):
- **Catalog:** `DefaultCatalog`
- **Cluster:** `nexus-flink (standalone)` — pick your registered cluster
- **Mode:** `standalone`

Paste this SQL into the editor:

```sql
SELECT 'hello world' AS greeting;
```

Click **Execute** (the play button, or `Ctrl+Enter`).

Expected: a result panel appears at the bottom with one row, one column, `hello world`.

This proves Dinky successfully submitted a job to Flink, Flink executed it, and the result came back. You're wired.

## Why this step isn't automatic

Dinky supports multiple Flink clusters across different physical hosts — including remote ones. So it doesn't assume "the local one at `flink-jobmanager:8081` is mine". On Nexus-Stack only one cluster exists, but Dinky's config model is the same either way. One-time pain for portability.

## Troubleshooting

### Status shows `Abnormal`

Cluster can't be reached. Walk through:

```bash
# In code-server terminal
curl -sI http://flink-jobmanager:8081/overview
```

Expected: `200 OK`. If you get connection-refused or a DNS error, Flink isn't running or isn't on the same Docker network.

Check:

```bash
docker ps | grep flink-jobmanager
```

If missing → re-enable the Flink stack in the Control Plane and re-spin.

### "No jobs running" after Execute

The query might have succeeded but the result panel hasn't loaded. Look at the **Console** tab at the bottom for error messages. Common causes:
- **Cluster not selected** in the task config (left dropdown "DefaultCatalog" / right dropdown "nexus-flink") — both required
- **Task Type** is something other than `FlinkSQL` (e.g. `DorisSQL`)

### "Address already in use" errors in Flink logs

Flink has a **limited number of task slots** (2 on Nexus-Stack's default Flink config). Each running streaming job (`INSERT INTO`) consumes one slot. If all slots are taken, new jobs queue or fail.

Go to `https://flink.<your-domain>` → **Task Managers** → check the slot count. Kill old jobs via the Flink UI or Dinky's Running Jobs page.

### Password forgotten

Right now, Dinky's password reset flow is awkward — the admin password is stored in the Dinky DB. Easiest recovery:

```bash
# From the server
docker exec dinky <reset-command>
```

Exact command depends on Dinky version; check its docs. For now, **don't forget it**.

## What's next in Dinky

Now that the cluster is registered, you can query any Flink SQL source. The most common starting point on Nexus-Stack is reading from a Redpanda topic — see [Query a Redpanda topic with Flink SQL](/docs/tutorials/dinky-flink-sql-redpanda/).
