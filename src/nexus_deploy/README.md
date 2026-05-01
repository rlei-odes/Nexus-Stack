# nexus_deploy

Python package replacing `scripts/deploy.sh` (in progress, see issue #505).

## Status

**Phase 0 — Setup.** Package skeleton + CI quality gates. No real
deployment logic yet; `scripts/deploy.sh` is still the entry point.

## Usage (Phase 0)

```bash
uv sync                   # install deps + this package in editable mode
uv run pytest             # run tests (unit only, ~5 sec)
uv run mypy               # strict type-check (covers src + tests)
uv run ruff check .       # lint
uv run ruff format .      # auto-format
```

After `uv sync`:

```bash
nexus-deploy --version    # 0.1.0
nexus-deploy hello        # smoke test
```

Or equivalently:

```bash
python -m nexus_deploy --version
```

## Migration progress

See [docs/admin-guides/migration-to-python.md](../../docs/admin-guides/migration-to-python.md)
for the phased plan and current status.
