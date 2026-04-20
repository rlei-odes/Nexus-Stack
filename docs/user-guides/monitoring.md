---
title: "Monitoring"
description: "Workflow logs, configuration state, and runtime diagnostics"
order: 4
---

# Monitoring

The Monitoring page aggregates everything you'd look at when something isn't behaving: workflow logs from GitHub Actions, the current template configuration, and live workflow state.

![Monitoring overview](./assets/monitoring-overview.png)

## Quick stats

Four cards at the top:

| Card | What it counts |
|------|----------------|
| **Logs** | Archived workflow and system log bundles |
| **Config** | Template config files currently applied (services.yaml, config.tfvars) |
| **Workflows** | Past GitHub Actions runs for this stack |
| **Health** | Latest health check results |

Click any card to drill into the full list.

## Workflow logs

Per-workflow rows showing the last run's status, duration, and a link to the GitHub Actions page. The three you'll look at most:

- **initial-setup.yaml** — ran once at first deploy; should show `success`
- **spin-up.yml** — runs every time you click Spin Up
- **teardown.yml** — runs every time you click Teardown

A red status here is usually the first sign something broke during a Spin Up or Teardown.

## Config view

Shows the current `services.yaml` and `config.tfvars` that OpenTofu is using. Read-only — to change values, edit the repo and re-run Spin Up.

![Monitoring config panel](./assets/monitoring-config.png)

## Typical workflows

- **A stack won't start** — check its docker-compose in Config, then look at the latest spin-up log
- **Domain not resolving** — check the latest spin-up log for the Cloudflare Tunnel ingress step
- **Credentials missing** — check that `initial-setup.yaml` completed successfully (Infisical is seeded there)
