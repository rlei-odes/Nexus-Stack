---
title: "Integrations"
description: "Connect your stack to third-party platforms (Databricks, …)"
order: 8
---

# Integrations

Integrations let your Nexus-Stack talk to platforms outside Hetzner. Today there's one first-class integration — **Databricks** — with more on the roadmap.

![Integrations page header with tiles for connecting the stack to third-party platforms such as Databricks](./assets/integrations-header.png)


## Databricks

Mirrors your Infisical secrets into a Databricks workspace as secret scopes, so notebooks and jobs can read your stack's credentials without copy-pasting.

Don't have a Databricks account yet? [Register for free](https://login.databricks.com/signup).

### What gets synced

Currently only secrets are synced:

- **Secrets → Databricks secret scopes.** Every Infisical secret is mirrored into a scope named `nexus`. Re-synced on every Spin Up, or manually via **Sync Secrets to Databricks**.

### Finding your Workspace URL

Open your Databricks workspace in the browser. The URL in the address bar is your **Workspace URL** — copy everything up to `.cloud.databricks.com`.

![Databricks workspace browser address bar showing the Workspace URL](./assets/databricks-workspace-url.png)

### Creating a Personal Access Token (PAT)

Click your avatar (top right), then **Settings**.

![Databricks top-right avatar menu expanded, with the Settings option highlighted as the next step](./assets/databricks-user-menu.png)

![Databricks Settings page sidebar with the Developer section visible as the destination for access tokens](./assets/databricks-settings-menu.png)

In Settings, go to **Developer** → **Access tokens** → click **Manage**.

![Databricks Developer settings page with the Access tokens section and its Manage button](./assets/databricks-developer-settings.png)

Click **Generate new token**.

![Databricks Access tokens management page with a Generate new token button](./assets/databricks-access-tokens.png)

Fill in the form:
- **Comment**: `Nexus-Stack` (or any label you recognise)
- **Lifetime**: 90 days (or longer)
- **Scope**: `Other APIs` → `all-apis`

Click **Generate**.

![Generate new token form with Comment, Lifetime, and Scope fields filled in ready to submit](./assets/databricks-generate-token.png)

Copy the token immediately. You won't be able to see it again.

![Token generated dialog displaying the one-time-visible token string to copy before closing](./assets/databricks-token-created.png)

### Setup in the Control Plane

Go to **Integrations** in the Control Plane and fill in the two fields:

| Field | Value |
|-------|-------|
| **Workspace URL** | `https://dbc-xxxxx.cloud.databricks.com` |
| **Personal Access Token** | The token you just generated |

![Control Plane Databricks integration form with Workspace URL and Personal Access Token fields ready to be saved](./assets/databricks-integration-form.png)

Click **Save Configuration**, then **Sync Secrets to Databricks**. A "Last sync: success" confirmation appears when the sync completes.

![Databricks integration tile showing a "Last sync: success" confirmation after mirroring secrets](./assets/databricks-sync-success.png)

### Accessing Secrets in Databricks

Open a new Notebook in Databricks (**New → Notebook**).

![Databricks workspace New menu with the Notebook option highlighted to create a new notebook](./assets/databricks-new-notebook.png)

List all available secret scopes — you should see `nexus`:

```python
dbutils.secrets.listScopes()
```

![Databricks notebook cell output from dbutils.secrets.listScopes() with the nexus scope visible in the list](./assets/databricks-list-scopes.png)

List all secrets in the `nexus` scope:

```python
dbutils.secrets.list("nexus")
```

![Databricks notebook cell output from dbutils.secrets.list("nexus") listing every key inside the nexus scope](./assets/databricks-list-secrets.png)

Read a specific secret:

```python
admin_email = dbutils.secrets.get(scope="nexus", key="admin_email")
print(admin_email)
```

![Databricks notebook reading a secret with dbutils.secrets.get() and printing [REDACTED] as the value — the expected successful output](./assets/databricks-get-secret.png)

Secret values are always shown as `[REDACTED]` in Databricks notebook output — this is intentional and means the secret was read successfully.

## Future integrations

Planned: GitHub Codespaces bridge, JupyterHub SSO, Snowflake secret sync. Watch the [Nexus-Stack repo](https://github.com/stefanko-ch/Nexus-Stack) for new tiles on this page.
