---
title: "Integrations"
description: "Connect your stack to third-party platforms (Databricks, …)"
order: 8
---

# Integrations

Integrations let your Nexus-Stack talk to platforms outside Hetzner. Today there's one first-class integration — **Databricks** — with more on the roadmap.

![Integrations page](./assets/integrations-overview.png)

## Databricks

Links your stack's Gitea repos and Infisical secrets into a Databricks workspace so you can run notebooks and jobs against your local services without copy-pasting credentials.

### What gets synced

- **Secrets → Databricks secret scopes.** Every Infisical secret is mirrored into a scope named after its folder (e.g. `nexus/postgres`, `nexus/keycloak`). Re-synced on every Spin Up.
- **Gitea repos → Databricks Repos.** Each Gitea repo registers as a Databricks Repo, so a notebook in Databricks can import modules from your local code.

### Setup

Three fields:

| Field | Value |
|-------|-------|
| **Workspace URL** | `https://dbc-xxxxx.cloud.databricks.com` or your workspace's region-specific URL |
| **Personal Access Token** | Generated in Databricks under User Settings → Access tokens. Needs "Workspace admin" scope |
| **User email** | The Databricks user to own the synced scopes (usually the same email as your Control Plane login) |

Click **Save & Sync** — credentials are stored in the Control Plane's KV (encrypted) and the first sync runs immediately. Subsequent syncs run automatically on every Spin Up.

### Disconnecting

Click **Disconnect**. The stored PAT is deleted from KV; Databricks-side state (scopes, Repos) is NOT cleaned up — do that in the Databricks UI if you want a full wipe.

## Future integrations

Planned: GitHub Codespaces bridge, JupyterHub SSO, Snowflake secret sync. Watch the [Nexus-Stack repo](https://github.com/stefanko-ch/Nexus-Stack) for new tiles on this page.
