"""Tests for nexus_deploy.tfvars — Phase 4c (#505).

Pure-logic test surface. Two layers:

1. ``parse(path)`` — regex extraction of domain / admin_email /
   user_email from a synthetic config.tfvars fixture.
2. ``derive_gitea_identity(config)`` — admin-email collision
   fallback + first-comma-trim semantics. Mirrors deploy.sh:80-99
   exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_deploy.tfvars import (
    GiteaIdentity,
    TfvarsConfig,
    TfvarsError,
    derive_gitea_identity,
    parse,
)

# ---------------------------------------------------------------------------
# parse() — file I/O + regex extraction
# ---------------------------------------------------------------------------


def _write_tfvars(path: Path, content: str) -> Path:
    """Helper: write a config.tfvars fixture to a tmp_path."""
    path.write_text(content, encoding="utf-8")
    return path


def test_parse_standard_form(tmp_path: Path) -> None:
    """The vanilla case: 3 single-line double-quoted assignments."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain = "example.com"\n'
        'admin_email = "admin@example.com"\n'
        'user_email = "user@example.com"\n',
    )
    assert parse(fixture) == TfvarsConfig(
        domain="example.com",
        admin_email_raw="admin@example.com",
        user_email_raw="user@example.com",
    )


def test_parse_comma_separated_user_email(tmp_path: Path) -> None:
    """user_email may be a comma-list for the CF Access allow-list.
    parse() returns it RAW; the comma-split happens in derive()."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain = "example.com"\n'
        'admin_email = "admin@example.com"\n'
        'user_email = "alice@example.com, bob@example.com"\n',
    )
    config = parse(fixture)
    assert config.user_email_raw == "alice@example.com, bob@example.com"


def test_parse_missing_admin_email(tmp_path: Path) -> None:
    """admin_email may be absent (self-provisioned tfvars often omit
    it). parse() returns an empty string; derive() applies the
    synthetic fallback."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain = "example.com"\nuser_email = "user@example.com"\n',
    )
    assert parse(fixture).admin_email_raw == ""


def test_parse_extra_keys_are_ignored(tmp_path: Path) -> None:
    """The regex only captures domain / admin_email / user_email.
    Other tfvars keys (cloudflare_api_token, hcloud_token, etc.) must
    NOT trip the parser into emitting unexpected fields."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain = "example.com"\n'
        'cloudflare_api_token = "ABC123_secret_token"\n'
        'admin_email = "admin@example.com"\n'
        'hcloud_token = "secret_too"\n'
        'user_email = "user@example.com"\n',
    )
    config = parse(fixture)
    assert config.domain == "example.com"
    assert config.admin_email_raw == "admin@example.com"
    assert config.user_email_raw == "user@example.com"


def test_parse_whitespace_around_equals(tmp_path: Path) -> None:
    """The regex tolerates whitespace around ``=``: ``var = "x"``
    AND ``var="x"`` are both valid HCL."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain="example.com"\nadmin_email = "admin@example.com"\n',
    )
    config = parse(fixture)
    assert config.domain == "example.com"
    assert config.admin_email_raw == "admin@example.com"


def test_parse_whitespace_inside_value_preserved(tmp_path: Path) -> None:
    """Leading/trailing space inside the quoted value IS preserved by
    parse() — the trim happens in derive()."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain = "example.com"\n'
        'admin_email = "  admin@example.com  "\n'
        'user_email = " user@example.com"\n',
    )
    config = parse(fixture)
    assert config.admin_email_raw == "  admin@example.com  "
    assert config.user_email_raw == " user@example.com"


def test_parse_unquoted_values_not_matched(tmp_path: Path) -> None:
    """The regex requires double-quoted values. An unquoted line
    silently doesn't match — that's fine for our project's
    convention but could surprise a future contributor. Test pins
    the behavior so a regex relaxation is a deliberate decision."""
    fixture = _write_tfvars(
        tmp_path / "config.tfvars",
        'domain = "example.com"\nadmin_email = unquoted\n',
    )
    config = parse(fixture)
    assert config.domain == "example.com"
    assert config.admin_email_raw == ""  # unquoted line didn't match


def test_parse_empty_file(tmp_path: Path) -> None:
    """Empty config.tfvars → all-empty TfvarsConfig (defaults). The
    pipeline's own gates (e.g. ``if not domain``) decide whether to
    abort."""
    fixture = _write_tfvars(tmp_path / "config.tfvars", "")
    assert parse(fixture) == TfvarsConfig()


def test_parse_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TfvarsError, match=r"config\.tfvars not found"):
        parse(tmp_path / "does-not-exist.tfvars")


# ---------------------------------------------------------------------------
# derive_gitea_identity — admin/user collision fallback
# ---------------------------------------------------------------------------


def test_derive_no_collision(tfvars_no_collision: TfvarsConfig) -> None:
    """admin distinct from user → both used as-is."""
    identity = derive_gitea_identity(tfvars_no_collision)
    assert identity == GiteaIdentity(
        admin_email="admin@example.com",
        gitea_user_email="user@example.com",
        gitea_user_username="user",
        om_principal_domain="example.com",
    )


def test_derive_collision_falls_back_to_synthetic(
    tfvars_collision: TfvarsConfig,
) -> None:
    """admin == user → synthesise gitea-admin@<domain>."""
    identity = derive_gitea_identity(tfvars_collision)
    assert identity.admin_email == "gitea-admin@example.com"
    assert identity.gitea_user_email == "shared@example.com"
    assert identity.om_principal_domain == "example.com"


def test_derive_empty_admin_falls_back_to_synthetic(tmp_path: Path) -> None:
    """admin_email empty → synthesise. Same path as the collision
    case (the `if not admin OR admin == user` gate)."""
    config = TfvarsConfig(
        domain="example.com",
        admin_email_raw="",
        user_email_raw="user@example.com",
    )
    assert derive_gitea_identity(config).admin_email == "gitea-admin@example.com"


def test_derive_first_comma_entry_used(tmp_path: Path) -> None:
    """Multi-admin user_email list: only the first entry is used for
    the Gitea user.email column (Gitea rejects commas with 'unsupported
    character')."""
    config = TfvarsConfig(
        domain="example.com",
        admin_email_raw="admin@example.com",
        user_email_raw="first@example.com, second@example.com, third@example.com",
    )
    identity = derive_gitea_identity(config)
    assert identity.gitea_user_email == "first@example.com"
    assert identity.gitea_user_username == "first"


def test_derive_trims_whitespace_from_emails(tmp_path: Path) -> None:
    """Self-provisioned tfvars commonly have leading spaces inside
    quoted values. Gitea/Windmill/Wiki.js validators reject those, so
    derive() must trim. Mirrors the legacy bash sed at deploy.sh:80-81."""
    config = TfvarsConfig(
        domain="example.com",
        admin_email_raw="   admin@example.com   ",
        user_email_raw=" user@example.com ",
    )
    identity = derive_gitea_identity(config)
    assert identity.admin_email == "admin@example.com"
    assert identity.gitea_user_email == "user@example.com"


def test_derive_username_is_local_part(tmp_path: Path) -> None:
    """gitea_user_username = local part of email (text before @)."""
    config = TfvarsConfig(
        domain="example.com",
        admin_email_raw="admin@example.com",
        user_email_raw="alice.bob+tag@university.edu",
    )
    assert derive_gitea_identity(config).gitea_user_username == "alice.bob+tag"


def test_derive_om_principal_domain_extracted_from_admin(tmp_path: Path) -> None:
    """OM_PRINCIPAL_DOMAIN is the domain part of the (post-fallback)
    admin_email. With the synthetic fallback, that's the configured
    project domain."""
    config = TfvarsConfig(
        domain="my.subdomain.example.com",
        admin_email_raw="admin@my.subdomain.example.com",
        user_email_raw="user@my.subdomain.example.com",
    )
    identity = derive_gitea_identity(config)
    # Collision → synthetic admin → OM domain = project domain
    assert identity.om_principal_domain == "my.subdomain.example.com"


def test_derive_no_user_email_skips_username(tmp_path: Path) -> None:
    """When user_email is empty, the orchestrator's user-create gate
    skips. derive() returns empty username — NOT the admin fallback
    (which would re-introduce the original collision bug)."""
    config = TfvarsConfig(
        domain="example.com",
        admin_email_raw="admin@example.com",
        user_email_raw="",
    )
    identity = derive_gitea_identity(config)
    assert identity.gitea_user_email == ""
    assert identity.gitea_user_username == ""
    assert identity.admin_email == "admin@example.com"


def test_derive_collision_with_no_domain_returns_empty_admin(tmp_path: Path) -> None:
    """Defensive: if domain is empty AND we hit the collision branch,
    we can't synthesise gitea-admin@<empty> meaningfully — return an
    empty admin_email and let the pipeline's own gates abort. (The
    pipeline rejects empty domain BEFORE this function runs, so this
    branch is a defence-in-depth safety net.)"""
    config = TfvarsConfig(
        domain="",
        admin_email_raw="shared@somewhere.com",
        user_email_raw="shared@somewhere.com",
    )
    assert derive_gitea_identity(config).admin_email == ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tfvars_no_collision() -> TfvarsConfig:
    return TfvarsConfig(
        domain="example.com",
        admin_email_raw="admin@example.com",
        user_email_raw="user@example.com",
    )


@pytest.fixture
def tfvars_collision() -> TfvarsConfig:
    return TfvarsConfig(
        domain="example.com",
        admin_email_raw="shared@example.com",
        user_email_raw="shared@example.com",
    )


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants
# ---------------------------------------------------------------------------


def test_tfvars_config_frozen() -> None:
    from dataclasses import FrozenInstanceError

    config = TfvarsConfig(domain="x", admin_email_raw="y", user_email_raw="z")
    with pytest.raises(FrozenInstanceError):
        config.domain = "other"  # type: ignore[misc]


def test_gitea_identity_frozen() -> None:
    from dataclasses import FrozenInstanceError

    identity = GiteaIdentity(
        admin_email="a", gitea_user_email="b", gitea_user_username="c", om_principal_domain="d"
    )
    with pytest.raises(FrozenInstanceError):
        identity.admin_email = "other"  # type: ignore[misc]
