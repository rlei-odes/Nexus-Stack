---
title: "Code-Server"
description: "Terminal and Python setup inside the browser-based VS Code that ships with Nexus-Stack"
order: 0
---

# Code-Server tutorials

**Code-server** is VS Code running in your browser, served by your Nexus-Stack server. It's the easiest way to run code that needs to reach internal services like Redpanda, Redpanda Connect, or Flink without exposing those services to the public internet.

Most other tutorials on this site assume code-server. These two short pieces get you from "I just enabled the stack" to "I can run Python that talks to internal APIs".

## Start here

1. **[Run curl in the code-server terminal](./terminal-curl)** — open a terminal, understand Docker service names, and hit internal admin APIs. 5 minutes.
2. **[Set up an isolated Python environment with uv](./python-uv)** — `.venv` with `uv`, package installs, and the Jupyter kernel picker. 10 minutes.

After this you're ready for any of the [Redpanda tutorials](/docs/tutorials/redpanda/) that use code-server as the execution environment.
