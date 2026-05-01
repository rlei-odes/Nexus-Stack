---
title: "Migration: deploy.sh → Python (`nexus_deploy`)"
---

## Overview

`scripts/deploy.sh` (5,539 lines of bash) is being incrementally replaced by a proper Python package, **`nexus_deploy`**, under `src/nexus_deploy/`. See [GitHub issue #505](https://github.com/stefanko-ch/Nexus-Stack/issues/505) for the full migration plan.

## Why

The deploy script grew organically with the project (65 stacks, 480 commits/month). Specific pain points it now causes:

- **Duplicated pattern blocks** — the Infisical secret-sync block has been copy-pasted across Jupyter, Marimo, and (soon) code-server stacks. Each clone went through ~8 review rounds for the same bug classes.
- **Test loop = full spin-up** — every change requires a 10-25 minute Tofu-apply round-trip. No fast unit tests.
- **No refactoring safety** — no type-checks, no automated tests, every edit is risk.
- **Onboarding barrier** — bash idioms (`set -euo pipefail` quirks, heredoc escaping, variable-naming conventions) are real hurdles for any contributor besides the original author.
- **Unbounded growth** — adding a new notebook-style stack means another ~600-line clone of the secret-sync block.

## Strategy: Strangler Fig

The Python package grows alongside the existing `scripts/deploy.sh`. Each migration phase is a separate PR. As Python modules take over, the bash deploy.sh shrinks. In Phase 4 it's removed entirely.

| Phase | Goal | Status |
|---|---|---|
| **0** | Setup: `pyproject.toml`, ruff/mypy/pytest, CI quality gates | **In progress** |
| 1 | Migrate highest-pain modules: `infisical.py`, `secret_sync.py`, `config.py` | Pending |
| 2 | `seeder.py`, `services.py`, `kestra.py` | Pending |
| 3 | `ssh.py`, `tofu.py`, `stack_sync.py`, `orchestrator.py` | Pending |
| 4 | Remove deploy.sh; spin-up.yml calls Python directly | Pending |

Acceptance for each migrated module: **gold-master tests** that compare Python output byte-for-byte against pre-migration bash output, so behavior cannot drift silently.

## What's NOT migrated

- `stacks/*/Dockerfile` + `docker-compose.yml` — declarative config
- `tofu/*.tf` — separate tooling ecosystem
- `.github/workflows/*.yml` — GitHub Actions DSL
- Small bash helpers in `scripts/` (`init-r2-state.sh`, `check-*.sh`, `cleanup-orphaned-resources.sh`, `parse-services-yaml.sh`, `setup-control-plane-secrets.sh`) — single-purpose, bash is fine
- Existing standalone Python scripts in `stacks/*/` — already Python, not deploy logic

## Local development

After cloning:

```bash
# Install uv (one-time, see https://docs.astral.sh/uv/)
brew install uv          # macOS

# Sync dependencies (creates .venv, installs all deps + this package)
uv sync

# Run the same checks CI runs
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest

# Auto-format / auto-fix
uv run ruff format .
uv run ruff check --fix .

# Optional: install pre-commit hooks for automatic checks before each commit
uv run pre-commit install
```

After `uv sync`, the package is importable and a `nexus-deploy` CLI command is on PATH inside the venv:

```bash
uv run nexus-deploy --version    # 0.1.0 (Phase 0)
uv run nexus-deploy hello        # smoke test
```

## Quality gates

Enforced via CI on every PR (`.github/workflows/python-tests.yml`):

| Gate | Tool | Threshold |
|---|---|---|
| Format | `ruff format --check` | zero diff |
| Lint | `ruff check` | zero violations |
| Type-check | `mypy --strict` | zero errors |
| Tests | `pytest` | all green |
| Coverage | `pytest --cov --cov-fail-under=80` | ≥80% line coverage on `src/nexus_deploy/` |
| Shell | `shellcheck scripts/*.sh` | zero violations on remaining bash |

## During the migration

While both code paths coexist (Phase 0-3):

- **`scripts/deploy.sh` stays the entry point** for `gh workflow run spin-up.yml`. It progressively shells out to `python -m nexus_deploy <command>` for migrated functionality.
- **New deployment features**: if the relevant module has been migrated, implement in Python; otherwise, in bash. Don't double-implement.
- **Existing Python scripts** elsewhere in the repo (e.g., `stacks/*/connectivity-test.py`) are out of scope — this migration only covers the deploy orchestration.

## See also

- [GitHub issue #505](https://github.com/stefanko-ch/Nexus-Stack/issues/505) — full migration plan, 59 sub-tasks
- [`src/nexus_deploy/README.md`](../../src/nexus_deploy/README.md) — package-level usage notes
- Related deploy.sh PRs that informed this migration: #495, #499, #500, #504 (each surfaced specific bug classes that pytest+mypy would catch automatically)
