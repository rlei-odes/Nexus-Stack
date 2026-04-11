---
title: "Website Documentation Sync"
description: "How documentation is synced from this repo to nexus-stack.ch"
order: 7
---

# Website Documentation Sync

Documentation in this repo is the **single source of truth** for [nexus-stack.ch](https://nexus-stack.ch). The website fetches docs at build time — no content is duplicated between repos.

## How It Works

```
Nexus-Stack repo                    nexus-stack.ch repo
┌──────────────────┐                ┌──────────────────┐
│ docs/stacks/*.md  │                │ Astro Content    │
│ docs/*.md         │  ──push to──>  │ Loaders fetch    │
│ docs/tutorials/*  │  ──main────>   │ from GitHub at   │
│ services.yaml     │                │ build time       │
└──────────────────┘                └──────────────────┘
         │                                   │
         │ sync-docs-site.yml                │
         │ (repository_dispatch)             │
         └──────────────────────────────────>┘
                triggers rebuild
```

1. A push to `main` that changes `docs/`, `services.yaml`, or `README.md` triggers the `sync-docs-site.yml` workflow
2. The workflow sends a `repository_dispatch` event to the `stefanko-ch/nexus-stack.ch` repo
3. The website repo rebuilds, fetching fresh content from `raw.githubusercontent.com`
4. Cloudflare Pages deploys the updated site

## Content Mapping

| Content | Source | Website renders as |
|---------|--------|-------------------|
| `docs/stacks/*.md` | Stack documentation | `/docs/stacks/[slug]` pages |
| `docs/*.md` | General guides (setup, debugging, SSH) | `/docs/[slug]` pages |
| `docs/tutorials/*.md` | Tutorials and walkthroughs | `/tutorials/[slug]` pages |
| `services.yaml` | Service metadata (ports, categories, descriptions) | Stack list, navigation, metadata |

## Writing Documentation

### Stack Docs (`docs/stacks/`)

Each stack has a markdown file with a `title` frontmatter field:

```markdown
---
title: "Service Name"
---

## Service Name

(content)
```

The `description`, `category`, `port`, and other metadata come from `services.yaml` — don't duplicate them in frontmatter.

### General Docs (`docs/`)

General docs have `title`, `description`, and `order` fields:

```markdown
---
title: "Setup Guide"
description: "Complete installation and configuration guide"
order: 1
---

(content)
```

The `order` field controls the navigation order on the website.

### Tutorials (`docs/tutorials/`)

Same format as general docs:

```markdown
---
title: "Stream Processing with RisingWave"
description: "End-to-end tutorial for real-time streaming"
order: 1
---

(content)
```

## Setup (Maintainer Only)

This section is only relevant for the repository owner. Forks do not need this setup — the sync workflow is skipped automatically.

### 1. Create a GitHub PAT

1. Go to [GitHub Settings > Developer settings > Fine-grained tokens](https://github.com/settings/tokens?type=beta)
2. Create a new token with:
   - **Repository access**: Only `stefanko-ch/nexus-stack.ch`
   - **Permissions**: Contents → Read and Write
3. Copy the token

### 2. Add the Secret

1. Go to [Nexus-Stack repo settings > Secrets > Actions](https://github.com/stefanko-ch/Nexus-Stack/settings/secrets/actions)
2. Add a new secret:
   - **Name**: `WEBSITE_DISPATCH_TOKEN`
   - **Value**: The PAT from step 1

### 3. Enable Website Sync

1. Go to [Nexus-Stack repo settings > Secrets and variables > Actions > Variables](https://github.com/stefanko-ch/Nexus-Stack/settings/variables/actions)
2. Add a new repository variable:
   - **Name**: `WEBSITE_SYNC_ENABLED`
   - **Value**: `true`

The sync workflow is gated on this variable. If it is missing or set to any other value, the job will be skipped even if `WEBSITE_DISPATCH_TOKEN` is configured.

### 4. Website Repo Setup

In the `nexus-stack.ch` repo, add a `repository_dispatch` trigger to the build workflow:

```yaml
on:
  push:
    branches: [main]
  repository_dispatch:
    types: [docs-updated]
```

The Astro Content Loaders in the website repo handle fetching and rendering the docs.

## Fork Safety

The sync workflow has three independent protection layers:

| Layer | How it works |
|-------|-------------|
| Repo check | `if: github.repository == 'stefanko-ch/Nexus-Stack'` skips on any fork |
| Missing secret | Forks don't have `WEBSITE_DISPATCH_TOKEN`, dispatch fails silently |
| PAT scope | Token only has access to the specific target repo |

Forks can safely ignore the `sync-docs-site.yml` workflow. It will never run and never fail.
