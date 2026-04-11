---
title: "Apache Superset"
---

## Apache Superset

![Apache Superset](https://img.shields.io/badge/Apache_Superset-20A6A4?logo=apachesuperset&logoColor=white)

**Modern data exploration and visualization platform with SQL Lab and interactive dashboards.**

Apache Superset is a modern data exploration and visualization platform. Create interactive dashboards, explore datasets with SQL Lab, build rich visualizations with 40+ chart types, and share insights. Supports PostgreSQL, ClickHouse, Trino, and 30+ database engines with a no-code chart builder and role-based access control.

| Setting | Value |
|---------|-------|
| Default Port | `8089` |
| Suggested Subdomain | `superset` |
| Public Access | No |
| Website | [superset.apache.org](https://superset.apache.org) |
| Source | [GitHub](https://github.com/apache/superset) |

> Auto-configured: Admin user is created automatically during first startup using generated credentials from Infisical.

### Default Credentials

- **Username:** `admin`
- **Password:** See Infisical (`superset` folder > `SUPERSET_PASSWORD`)

### Connecting Data Sources

Superset can connect to databases running in your Nexus Stack. Use the internal Docker network hostnames:

| Database | Connection String |
|----------|------------------|
| PostgreSQL | `postgresql+psycopg2://nexus-postgres:<password>@postgres:5432/nexus` |
| ClickHouse | `clickhousedb://nexus-clickhouse:<password>@clickhouse:8123/default` |
| Trino | `trino://trino@trino:8060/` |

Add connections via **Settings > Database Connections > + Database** in the Superset UI.
