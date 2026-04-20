---
title: "Firewall"
description: "Open TCP ports directly on the Hetzner server (bypasses Cloudflare Tunnel)"
order: 6
---

# Firewall

Most traffic reaches your stack through the **Cloudflare Tunnel**, which means zero open ports on the server itself. A few services — think a database client tunnel, a Spark worker, an agent connecting over raw TCP — need direct port access. The Firewall page is where you allow that, carefully.

![Firewall rules page](./assets/firewall-rules.png)

## Big red warning first

> Opening a port exposes a service directly to the internet, bypassing Cloudflare Access. Use this only when there's no way to go through the tunnel, and always constrain the source IP range.

The banner at the top of the page says exactly this for a reason.

## Adding a rule

Each rule has:

| Field | Purpose |
|-------|---------|
| **Port** | TCP port to open on the server |
| **Stack** | Friendly label (dropdown of enabled stacks, plus a custom option) |
| **Source** | Allowed source IPs in CIDR (e.g. `203.0.113.0/24`). Use `0.0.0.0/0` only if you really mean "anyone on the internet" |
| **Comment** | Free-text reason; shows up in audit logs |

Click **Add rule** — the backend calls the Hetzner API to update the firewall attached to your server. Changes are live in a few seconds.

## Deleting a rule

Click the trash icon on any row. Confirmation dialog appears; rule is deleted from both the UI and the Hetzner firewall on confirm.

## Rules reset on Teardown

Every Teardown detaches the firewall and resets all custom rules. This is a **safety feature**: if you forgot a wide-open port before tearing down, it won't silently re-appear on the next Spin Up. You have to re-add the rule deliberately.

## What's always allowed

- Outbound: all traffic (so the server can reach Docker Hub, Infisical, CF, etc.)
- Inbound: nothing, except what you add here

The Cloudflare Tunnel does not use inbound TCP — it's an outbound tunnel — so the tunnel keeps working even with zero rules.
