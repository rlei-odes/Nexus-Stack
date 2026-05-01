"""Shared pytest fixtures for nexus_deploy tests.

Phase 0 ships a small set: just enough to demonstrate the pattern
and unblock unit-test writing in Phase 1. Real fixtures (mock
Infisical API, mock SSH server, fake SECRETS_JSON, testcontainers)
land alongside the modules that need them — pytest auto-discovers
fixtures from any conftest.py in the test path, so per-module
conftest.py files are fine and encouraged.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_workdir(tmp_path: Path) -> Path:
    """Return a fresh temporary directory as Path.

    Wrapper around pytest's built-in `tmp_path` for typing clarity
    and to make it explicit at the test-call site that we're using
    isolated filesystem state.
    """
    return tmp_path


@pytest.fixture
def fake_secrets_json() -> dict[str, str]:
    """Minimal valid SECRETS_JSON shape.

    Phase 1's `config.py` expansion replaces this with a richer
    fixture that covers all ~70 fields. For Phase 0 we just want
    a stable example to demonstrate fixture wiring.
    """
    return {
        "domain": "example.com",
        "admin_email": "admin@example.com",
        "admin_username": "admin",
    }
