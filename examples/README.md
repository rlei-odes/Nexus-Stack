# `examples/`

Sample code that ships with Nexus-Stack and lands automatically in every freshly-provisioned user stack. The convention is intentionally narrow so contributors don't need to learn a new system every time they add a new starter file.

## What lives here

| Subtree | Purpose | Auto-seeded? |
|---|---|---|
| [`workspace-seeds/`](./workspace-seeds/) | Files that get committed into the user's Gitea workspace repo on every spin-up | **Yes** — copied 1:1 by `scripts/deploy.sh` after the workspace repo is created |

For now there is only `workspace-seeds/`. If we ever ship reference material that is *not* meant to land in the workspace repo (e.g. contributor recipes for adding a new stack), it gets a sibling directory like `examples/contributing/` and an explicit "**not** auto-seeded" note.

## How `workspace-seeds/` maps to the workspace repo

The directory layout under `workspace-seeds/` mirrors the workspace Gitea repo's root **1:1**. Whatever path a file has under `workspace-seeds/`, that's the path it lands at in the workspace repo:

```
nexus-stack/                                                  workspace Gitea repo (after spin-up):
└── examples/                                                 nexus-<slug>-gitea/
    └── workspace-seeds/                                      ├── kestra/
        ├── kestra/                ─────── seed ───►          │   ├── flows/
        │   ├── flows/                                        │   │   └── tutorials/
        │   │   └── tutorials/                                │   │       └── r2-taxi-pipeline.yaml
        │   │       └── r2-taxi-pipeline.yaml                 │   └── workflows/   (helper files)
        │   └── workflows/                                    ├── notebooks/       (when added)
        ├── notebooks/                                        ├── scripts/         (when added)
        ├── scripts/                                          ├── dbt/             (when added)
        ├── dbt/                                              └── sql/             (when added)
        └── sql/
```

This means any file you drop under `workspace-seeds/<dir>/<name>` will appear in every user's workspace at the same `<dir>/<name>` after the next Initial Setup. No `deploy.sh` edit, no new code path.

## Subdirectory conventions

Top-level folders under `workspace-seeds/` are split in two:

- **Per-stack folders** (e.g. `kestra/`) — used when the seeded material is unambiguously tied to one Nexus-Stack and that stack expects to find it at a stack-specific path. Today only `kestra/` qualifies; other stacks are added in the same shape if/when the same need arises.
- **Per-consumer-type folders** (e.g. `notebooks/`, `dbt/`, `sql/`) — used when the same files are consumed by *several* stacks (notebooks are read by Jupyter + Marimo + code-server; SQL is run by DuckDB + Trino + ClickHouse). Promoting these into a single owning-stack folder would force a misleading attribution.

Stick to these names so the various services pick the files up correctly:

| Folder | Consumed by | What goes here |
|---|---|---|
| `kestra/flows/` | Kestra (via `system.flow-sync`, registered by `deploy.sh`) | Flow definitions in YAML. `kestra/flows/<namespace>/<id>.yaml` lands in Kestra namespace `<namespace>`. |
| `kestra/workflows/` | Kestra (via `system.git-sync`, registered by `deploy.sh`) | Helper files referenced by flows: Python scripts, SQL templates, configs. **Not** flow definitions. |
| `notebooks/` | Jupyter, Marimo, code-server (cloned from the workspace repo) | `.ipynb` notebooks or `.py` scripts. |
| `scripts/` | code-server, ad-hoc execution | Shell or Python helpers reused across notebooks. |
| `dbt/` | code-server, manual `dbt` invocation | A normal dbt project tree (`dbt_project.yml`, `models/`, etc.). |
| `sql/` | DuckDB, Trino, ClickHouse — anywhere SQL gets pasted | Stand-alone SQL files. |

If a new stack needs its own per-stack folder, add it under `workspace-seeds/<stack>/` and list it here. If a new consumer-type folder is needed (multiple stacks share it), add it as a top-level peer of `notebooks/` / `scripts/`.

## How seeding works

`scripts/deploy.sh`, after the workspace repo exists, walks every file under `examples/workspace-seeds/`, base64-encodes it, and POSTs it to the internal Gitea API (`http://localhost:3200/api/v1/repos/<admin>/<repo>/contents/<path>`, accessed via SSH from the runner) with the relative path.

- HTTP **201/200** → file created. Counted as `SEEDED`.
- HTTP **422** → file already exists. Counted as `SKIPPED`. **Existing files are never overwritten** — student edits persist across re-deploys.
- Anything else → `FAILED`, logged as a warning.

Because seeds use `POST` (create-only) instead of `PUT` (upsert), the seed step is safe to re-run after every spin-up. The trade-off: when you publish a new version of a seed file in a Nexus-Stack release, students who already have the file get the old version. If you need to push an updated example, give it a new filename or version-suffix it.

## Rules for seeded files

### 1. No schedule triggers in seeded flows

Seeded Kestra flows under `workspace-seeds/kestra/flows/` **must not** declare `triggers:` blocks of type `Schedule` (cron) or any other auto-firing trigger.

**Why:**
- A seeded flow lands on every user stack.
- A schedule trigger then fires on N user stacks, multiplying upstream load (CloudFront downloads, Databricks Free-Edition quota burn, Redpanda traffic, R2 egress) by the cohort size.
- Examples are *teaching artifacts*. Users press **Execute** in the Kestra UI to run them; they shouldn't run silently in the background.

What's allowed instead:
- A `Webhook` trigger that requires explicit invocation. Fine.
- No `triggers:` block at all. Run manually from the UI.

If you genuinely need a system-level scheduled flow (e.g. a periodic data refresh that the platform itself depends on), don't put it under `workspace-seeds/`. Register it directly in `deploy.sh` via the Kestra API the way `system.flow-sync` is registered today — that's infrastructure, not a learning sample, and lives outside this directory.

### 2. Reference Infisical-managed secrets only via `{{ secret('NAME') }}`

`scripts/deploy.sh` syncs every Infisical secret into Kestra's secret store on each spin-up (the block that does this is the one tagged "Push every Infisical secret into Kestra's secret store" in `deploy.sh`, sitting next to the existing `GITEA_TOKEN` PUT). Reference them in flows as `{{ secret('R2_ACCESS_KEY') }}`, `{{ secret('GITEA_TOKEN') }}`, etc. Never hardcode credentials in seed files — this directory is public on GitHub.

### 3. Idempotent if executed multiple times

A user may hit **Execute** on a seeded flow more than once. Ensure the flow either: detects pre-existing state and skips work (`if exists then continue`), or is naturally repeatable (overwriting outputs is fine). Don't accumulate side-effects on each run.

### 4. Filename suggests intent

Use kebab-case file names that convey what the example does at a glance: `r2-taxi-pipeline.yaml`, `databricks-warehouse-query.yaml`, `bluesky-firehose-ingest.yaml`. The flow's `id` field can match the filename minus `.yaml` for consistency.

## Adding a new example

1. Decide which subdirectory under `workspace-seeds/` it belongs in (table above). Stack-specific Kestra material → `kestra/flows/<namespace>/`. Multi-consumer material → top-level `notebooks/` / `scripts/` / etc.
2. Add the file at the right relative path, e.g. `workspace-seeds/kestra/flows/tutorials/redpanda-produce-consume.yaml` or `workspace-seeds/notebooks/exploring-r2.ipynb`.
3. Read the rules above.
4. Open a PR. CI doesn't validate the seeds at build time — but `.github/copilot-instructions.md` carries the no-schedule-trigger rule, so Copilot will flag PRs that violate it.
5. After merge, the next spin-up will land the file in every user's workspace repo. Existing users who already have files in the same path keep their version.

## What this directory is *not*

- **Not a place for one-off experiments.** Anything here ships to every user forever (until they delete it). Use a personal branch or your own Gitea repo for throwaway experiments.
- **Not a substitute for documentation.** The companion docs at `docs/tutorials/` explain *why* and *how*; the examples are *what* you actually run. Keep both in sync when you add either.
- **Not a place for production-style infrastructure flows.** Those live in `deploy.sh` (registered directly) or in a future `stacks/` extension.
