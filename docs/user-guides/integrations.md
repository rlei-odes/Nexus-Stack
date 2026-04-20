---
title: "Integrations"
description: "Connect your stack to third-party platforms (Databricks, …)"
order: 8
---

# Integrations

Integrations let your Nexus-Stack talk to platforms outside Hetzner. Today there's one first-class integration — **Databricks** — with more on the roadmap.

<img src="./assets/integrations-header.png" style="width: 100%; height: auto;" />


## Databricks

Mirrors your Infisical secrets into a Databricks workspace as secret scopes, so notebooks and jobs can read your stack's credentials without copy-pasting.

Don't have a Databricks account yet? [Register for free](https://login.databricks.com/signup).

### What gets synced

Currently only secrets are synced:

- **Secrets → Databricks secret scopes.** Every Infisical secret is mirrored into a scope named `nexus`. Re-synced on every Spin Up, or manually via **Sync Secrets to Databricks**.

### Finding your Workspace URL

Open your Databricks workspace in the browser. The URL in the address bar is your **Workspace URL** — copy everything up to `.cloud.databricks.com`.

<img src="./assets/databricks-workspace-url.png" style="width: 100%; height: auto;" />

### Creating a Personal Access Token (PAT)

Click your avatar (top right), then **Settings**.

<img src="./assets/databricks-user-menu.png" style="width: 60%; height: auto;" />

<img src="./assets/databricks-settings-menu.png" style="width: 40%; height: auto;" />

In Settings, go to **Developer** → **Access tokens** → click **Manage**.

<img src="./assets/databricks-developer-settings.png" style="width: 100%; height: auto;" />

Click **Generate new token**.

<img src="./assets/databricks-access-tokens.png" style="width: 100%; height: auto;" />

Fill in the form:
- **Comment**: `Nexus-Stack` (or any label you recognise)
- **Lifetime**: 90 days (or longer)
- **Scope**: `Other APIs` → `all-apis`

Click **Generate**.

<img src="./assets/databricks-generate-token.png" style="width: 60%; height: auto;" />

Copy the token immediately. You won't be able to see it again.

<img src="./assets/databricks-token-created.png" style="width: 60%; height: auto;" />

### Setup in the Control Plane

Go to **Integrations** in the Control Plane and fill in the two fields:

| Field | Value |
|-------|-------|
| **Workspace URL** | `https://dbc-xxxxx.cloud.databricks.com` |
| **Personal Access Token** | The token you just generated |

<img src="./assets/databricks-integration-form.png" style="width: 100%; height: auto;" />

Click **Save Configuration**, then **Sync Secrets to Databricks**. A "Last sync: success" confirmation appears when the sync completes.

<img src="./assets/databricks-sync-success.png" style="width: 100%; height: auto;" />

### Accessing Secrets in Databricks

Open a new Notebook in Databricks (**New → Notebook**).

<img src="./assets/databricks-new-notebook.png" style="width: 70%; height: auto;" />

List all available secret scopes — you should see `nexus`:

```python
dbutils.secrets.listScopes()
```

<img src="./assets/databricks-list-scopes.png" style="width: 100%; height: auto;" />

List all secrets in the `nexus` scope:

```python
dbutils.secrets.list("nexus")
```

<img src="./assets/databricks-list-secrets.png" style="width: 100%; height: auto;" />

Read a specific secret:

```python
admin_email = dbutils.secrets.get(scope="nexus", key="admin_email")
print(admin_email)
```

<img src="./assets/databricks-get-secret.png" style="width: 100%; height: auto;" />

Secret values are always shown as `[REDACTED]` in Databricks notebook output — this is intentional and means the secret was read successfully.

## Future integrations

Planned: GitHub Codespaces bridge, JupyterHub SSO, Snowflake secret sync. Watch the [Nexus-Stack repo](https://github.com/stefanko-ch/Nexus-Stack) for new tiles on this page.
