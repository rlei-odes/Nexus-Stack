## Appsmith

![Appsmith](https://img.shields.io/badge/Appsmith-F86A2E?logo=appsmith&logoColor=white)

**Open-source low-code platform for building admin panels, dashboards, and internal tools**

Appsmith is an open-source framework for building internal tools and custom UIs. Features include:
- Drag-and-drop widget library (tables, forms, charts, maps, and more)
- 18+ native data source connectors (PostgreSQL, MySQL, MongoDB, REST APIs, GraphQL, S3, and more)
- JavaScript editor for writing business logic directly in the UI
- Git-based version control for application source
- Role-based access control with granular permissions
- All-in-one container with bundled MongoDB, PostgreSQL, and Redis

| Setting | Value |
|---------|-------|
| Default Port | `8098` |
| Suggested Subdomain | `appsmith` |
| Public Access | No |
| Website | [appsmith.com](https://appsmith.com) |
| Source | [GitHub](https://github.com/appsmithorg/appsmith) |

### First-Time Setup

On first access, Appsmith shows a registration screen. The first user to register becomes the admin. Complete this setup before sharing access with others.

### Persistent Data

All application data (apps, datasources, configurations) is stored in the `appsmith_data` Docker volume mounted at `/appsmith-stacks` inside the container. Data persists across container restarts.

### Encryption Keys

Appsmith uses `APPSMITH_ENCRYPTION_PASSWORD` and `APPSMITH_ENCRYPTION_SALT` to encrypt datasource credentials at rest. These are auto-generated during deployment and stored in Infisical. **Do not change these after the first run** — doing so will render all saved datasource credentials unreadable.
