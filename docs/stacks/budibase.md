---
title: "Budibase"
---

## Budibase

![Budibase](https://img.shields.io/badge/Budibase-9981F5?logo=budibase&logoColor=white)

**Open-source low-code platform for building internal tools, CRUD apps, and dashboards**

Budibase is a low-code development platform for creating internal business applications. Features include:
- Drag-and-drop UI builder for forms, tables, charts, and custom components
- Native data source connectors (PostgreSQL, MySQL, MongoDB, REST/GraphQL APIs)
- Server-side automation with scheduled jobs and webhook triggers
- Built-in user management with role-based access control
- Custom React/JSX components and plugin system
- All-in-one container (CouchDB, Redis, MinIO bundled)

| Setting | Value |
|---------|-------|
| Default Port | `8096` |
| Suggested Subdomain | `budibase` |
| Public Access | No |
| Website | [budibase.com](https://budibase.com) |
| Source | [GitHub](https://github.com/Budibase/budibase) |

### First-Time Setup

On first access, Budibase prompts you to create an admin account. No pre-configured credentials are needed.

### Persistent Data

All data (apps, databases, files) is stored in the `budibase_data` Docker volume and persists across container restarts.
