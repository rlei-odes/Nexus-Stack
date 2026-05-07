# nexus_deploy

Python package that orchestrates Nexus-Stack deployment. Replaced
the legacy ~5.5k-line `scripts/deploy.sh` over the course of issue
[#505](https://github.com/stefanko-ch/Nexus-Stack/issues/505)
(Phase 0 → Phase 4c, completed in
[PR #535](https://github.com/stefanko-ch/Nexus-Stack/pull/535)).

The single entrypoint is `python -m nexus_deploy run-pipeline`,
invoked by `.github/workflows/spin-up.yml`. Lower-level subcommands
(`infisical bootstrap`, `services configure`, etc.) are reachable
for ad-hoc operator work but rarely needed in the steady state.

## Local dev

```bash
uv sync                   # install deps + this package in editable mode
uv run pytest             # run tests (~7 sec, no network)
uv run mypy               # strict type-check (covers src + tests, per pyproject.toml)
uv run ruff check .       # lint
uv run ruff format .      # auto-format
```

After `uv sync`:

```bash
nexus-deploy --version
nexus-deploy hello        # smoke test, prints "nexus_deploy ready"
```

Or equivalently:

```bash
python -m nexus_deploy --version
```

## Subcommands (selection)

The full list is reachable via `python -m nexus_deploy <unknown>` —
the dispatcher prints a help block on unknown commands. Most-used:

| Command | Purpose |
|---|---|
| `run-pipeline` | Top-level deploy entrypoint. Reads tofu state + config.tfvars, walks setup → orchestrator → service URLs banner. |
| `select-capacity --tfvars PATH` | Pre-flight Hetzner capacity check ([#536](https://github.com/stefanko-ch/Nexus-Stack/issues/536) / [#537](https://github.com/stefanko-ch/Nexus-Stack/pull/537)) — picks the first available `<server_type>:<location>` pair from a preference list and rewrites config.tfvars. |
| `infisical bootstrap` | Push generated secrets into the Infisical project. |
| `services configure --enabled <list>` | Run per-service admin-setup hooks (e.g. provision Filestash admin, register RedPanda SASL user). |
| `kestra register-system-flows` | Register internal `system.*` flows (flow-sync, git-sync, etc.). |
| `firewall configure` | Render `docker-compose.firewall.yml` overrides from D1 firewall rules. |

## Background

See [docs/admin-guides/migration-to-python.md](../../docs/admin-guides/migration-to-python.md)
for the phased migration plan and the historical breakdown.
