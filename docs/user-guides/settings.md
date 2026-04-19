---
title: "Settings"
description: "Server info, auto-teardown schedule, and notification preferences"
order: 7
---

# Settings

The Settings page is split into two blocks: **Infrastructure Information** (read-only, tells you what's deployed) and **Scheduled Teardown** (read-write, controls the auto-shutdown worker).

![Settings page](/docs-images/user-guides/settings-overview.png)

## Infrastructure Information

Read-only facts about the current deployment:

| Field | Where it comes from |
|-------|---------------------|
| **Server Type** | `config.tfvars` — e.g. `cax11`, `cax21`, `cax31` |
| **Location** | Hetzner datacenter code (`fsn1`, `nbg1`, `hel1`) |
| **Base Domain** | Your root domain |
| **Template Version** | Currently deployed Nexus-Stack release |
| **Subdomain Separator** | `.` (dotted) or `-` (flat) — determines whether stacks live at `grafana.you.example.com` or `grafana-you.example.com` |

To change any of these you edit the repo and re-deploy — the Control Plane can't change server type on the fly.

## Scheduled Teardown

The Cron worker can auto-teardown your stack on a schedule so you don't burn money on an idle server overnight.

![Scheduled teardown panel](/docs-images/user-guides/settings-teardown.png)

### Schedule fields

- **Teardown cron** — standard 5-field cron in server-local time. Default `0 23 * * *` (23:00 every day).
- **Notification cron** — when to email you a heads-up that teardown is approaching. Default is 30 minutes before teardown.
- **Delay / Skip button** — one-click "not tonight" that pushes the next scheduled teardown back. Useful if you're mid-session.

### Disabling auto-teardown

Set **Allow disable** in your repo's `config.tfvars` to `true` and a toggle appears here to switch auto-teardown off entirely. By default this is locked so a cost-conscious admin can't forget to re-enable it.

### Max delay hours

If you keep delaying teardown every evening, eventually the worker will force a teardown no matter what. That cap is `max_delay_hours` in config. The Settings page shows how many delays you have left.

## Notifications

You receive two types of email from the Control Plane:

- **Teardown imminent** (sent by the Notification cron)
- **Credentials** (on demand — the **Email Credentials** button on the Dashboard)

Both are sent via Resend using the API key configured during Setup. If emails aren't arriving, check the Secrets page — `RESEND_API_KEY` should be under the global folder.
