---
title: "Control Plane"
description: "Web interface for managing your Nexus-Stack infrastructure and services"
order: 1
---

# Control Plane

The **Control Plane** is your web dashboard for managing a Nexus-Stack deployment: spin the stack up, tear it down, manage services, view secrets, and configure integrations — all behind Cloudflare Access authentication so only you (and anyone you've explicitly allow-listed) can reach it.

![Control Plane dashboard](/docs-images/user-guides/dashboard-overview.png)

## Accessing the Control Plane

Your Control Plane lives at:

```
https://control.<your-domain>
```

For flat-subdomain deployments it's the dashed form:

```
https://control-<user>.<base-domain>
```

On first visit Cloudflare Access redirects you to an email-OTP challenge. Enter the email address the stack was deployed for, check your inbox, click the one-time code — and you're in. The session lasts 24 hours.

## Navigation

The top nav has seven sections:

| Page | What you do there |
|------|-------------------|
| [Dashboard](./dashboard) | See infrastructure status, spin up / tear down the stack |
| [Stacks](./stacks) | Enable, disable, and open individual Docker services |
| [Monitoring](./monitoring) | Inspect workflow logs, config, and runtime state |
| [Secrets](./secrets) | Read-only view of Infisical secrets |
| [Firewall](./firewall) | Open TCP ports for services that need direct access |
| [Settings](./settings) | Server info, teardown schedule, notifications |
| [Integrations](./integrations) | Databricks sync and other third-party hookups |

Each page is covered in its own short guide — linked above. Start with the [Dashboard](./dashboard) guide if you're brand new.
