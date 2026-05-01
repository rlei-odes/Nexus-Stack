"""Typed parsing of `tofu output -json secrets` (Phase 1, #505).

Replaces the 70-field jq-pipeline block in `scripts/deploy.sh:115-212`
that lifts SECRETS_JSON into bash globals. The migration uses the
strangler-fig pattern: deploy.sh keeps running, but instead of running
its own jq pipeline it `eval`s the output of `python -m nexus_deploy
config dump-shell`. Same bash globals after eval, single Python source
of truth for the secret schema.

Field-mapping ground truth: every entry in ``_FIELDS`` corresponds 1:1
to a line in deploy.sh between L123 and L212. Adding a new secret means
editing ``_FIELDS`` here AND adding the matching tofu variable; the
deploy.sh jq line is removed in the same PR.

Out of scope (these stay in deploy.sh):
- ``DOMAIN`` / ``ADMIN_EMAIL`` — read from ``config.tfvars`` (L60-61),
  not from ``tofu output secrets``.
- ``CF_ACCESS_CLIENT_ID`` / ``CF_ACCESS_CLIENT_SECRET`` — read from
  the separate ``tofu output ssh_service_token`` (L215-217).
- ``IMAGE_VERSIONS_JSON`` — separate ``tofu output image_versions``.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigError(Exception):
    """Raised when SECRETS_JSON parsing fails (malformed JSON, schema mismatch)."""


# ---------------------------------------------------------------------------
# Field schema — single source of truth.
#
# Each tuple: (bash_var_name, json_key, fallback_when_missing).
#
# `bash_var_name` is what `dump_shell()` emits and what deploy.sh consumes
# via `eval`. `json_key` is the snake_case key from `tofu output -json
# secrets`. `fallback` mirrors the jq `// X` clause exactly:
#   - "" matches `// empty` (the overwhelming majority)
#   - "admin" matches `// "admin"` (admin_username — see deploy.sh:123)
#   - "External Storage" / "auto" mirror the explicit overwrite at L176-177
#
# Order matches deploy.sh source-order so the emitted block reads the same
# top-to-bottom; reviewers comparing the Python output against the legacy
# bash see no reordering noise.
# ---------------------------------------------------------------------------
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("ADMIN_USERNAME", "admin_username", "admin"),
    ("INFISICAL_PASS", "infisical_admin_password", ""),
    ("INFISICAL_ENCRYPTION_KEY", "infisical_encryption_key", ""),
    ("INFISICAL_AUTH_SECRET", "infisical_auth_secret", ""),
    ("INFISICAL_DB_PASSWORD", "infisical_db_password", ""),
    ("PORTAINER_PASS", "portainer_admin_password", ""),
    ("KUMA_PASS", "kuma_admin_password", ""),
    ("GRAFANA_PASS", "grafana_admin_password", ""),
    ("DAGSTER_DB_PASS", "dagster_db_password", ""),
    ("KESTRA_PASS", "kestra_admin_password", ""),
    ("KESTRA_DB_PASS", "kestra_db_password", ""),
    ("N8N_PASS", "n8n_admin_password", ""),
    ("METABASE_PASS", "metabase_admin_password", ""),
    ("SUPERSET_PASS", "superset_admin_password", ""),
    ("SUPERSET_DB_PASS", "superset_db_password", ""),
    ("SUPERSET_SECRET", "superset_secret_key", ""),
    ("CLOUDBEAVER_PASS", "cloudbeaver_admin_password", ""),
    ("MAGE_PASS", "mage_admin_password", ""),
    ("MINIO_ROOT_PASS", "minio_root_password", ""),
    ("SFTPGO_ADMIN_PASS", "sftpgo_admin_password", ""),
    ("SFTPGO_USER_PASS", "sftpgo_user_password", ""),
    ("HOPPSCOTCH_DB_PASS", "hoppscotch_db_password", ""),
    ("HOPPSCOTCH_JWT", "hoppscotch_jwt_secret", ""),
    ("HOPPSCOTCH_SESSION", "hoppscotch_session_secret", ""),
    ("HOPPSCOTCH_ENCRYPTION", "hoppscotch_encryption_key", ""),
    ("MELTANO_DB_PASS", "meltano_db_password", ""),
    ("SODA_DB_PASS", "soda_db_password", ""),
    ("REDPANDA_ADMIN_PASS", "redpanda_admin_password", ""),
    ("POSTGRES_PASS", "postgres_password", ""),
    ("PG_DUCKLAKE_PASS", "pgducklake_password", ""),
    ("HETZNER_S3_BUCKET_PGDUCKLAKE", "hetzner_s3_bucket_pgducklake", ""),
    ("PGADMIN_PASS", "pgadmin_password", ""),
    ("PREFECT_DB_PASS", "prefect_db_password", ""),
    ("RUSTFS_ROOT_PASS", "rustfs_root_password", ""),
    ("SEAWEEDFS_ADMIN_PASS", "seaweedfs_admin_password", ""),
    ("GARAGE_ADMIN_TOKEN", "garage_admin_token", ""),
    ("GARAGE_RPC_SECRET", "garage_rpc_secret", ""),
    ("LAKEFS_DB_PASS", "lakefs_db_password", ""),
    ("LAKEFS_ENCRYPT_SECRET", "lakefs_encrypt_secret", ""),
    ("LAKEFS_ADMIN_ACCESS_KEY", "lakefs_admin_access_key", ""),
    ("LAKEFS_ADMIN_SECRET_KEY", "lakefs_admin_secret_key", ""),
    ("HETZNER_S3_SERVER", "hetzner_s3_server", ""),
    ("HETZNER_S3_REGION", "hetzner_s3_region", ""),
    ("HETZNER_S3_ACCESS_KEY", "hetzner_s3_access_key", ""),
    ("HETZNER_S3_SECRET_KEY", "hetzner_s3_secret_key", ""),
    ("HETZNER_S3_BUCKET", "hetzner_s3_bucket_lakefs", ""),
    ("HETZNER_S3_BUCKET_GENERAL", "hetzner_s3_bucket_general", ""),
    ("EXTERNAL_S3_ENDPOINT", "external_s3_endpoint", ""),
    ("EXTERNAL_S3_REGION", "external_s3_region", "auto"),
    ("EXTERNAL_S3_ACCESS_KEY", "external_s3_access_key", ""),
    ("EXTERNAL_S3_SECRET_KEY", "external_s3_secret_key", ""),
    ("EXTERNAL_S3_BUCKET", "external_s3_bucket", ""),
    ("EXTERNAL_S3_LABEL", "external_s3_label", "External Storage"),
    ("R2_DATA_ENDPOINT", "r2_data_endpoint", ""),
    ("R2_DATA_ACCESS_KEY", "r2_data_access_key", ""),
    ("R2_DATA_SECRET_KEY", "r2_data_secret_key", ""),
    ("R2_DATA_BUCKET", "r2_data_bucket", ""),
    ("FILESTASH_ADMIN_PASSWORD", "filestash_admin_password", ""),
    ("WINDMILL_ADMIN_PASS", "windmill_admin_password", ""),
    ("WINDMILL_DB_PASS", "windmill_db_password", ""),
    ("WINDMILL_SUPERADMIN_SECRET", "windmill_superadmin_secret", ""),
    ("OPENMETADATA_ADMIN_PASS", "openmetadata_admin_password", ""),
    ("OPENMETADATA_DB_PASS", "openmetadata_db_password", ""),
    ("OPENMETADATA_AIRFLOW_PASS", "openmetadata_airflow_password", ""),
    ("OPENMETADATA_FERNET_KEY", "openmetadata_fernet_key", ""),
    ("GITEA_ADMIN_PASS", "gitea_admin_password", ""),
    ("GITEA_USER_PASS", "gitea_user_password", ""),
    ("GITEA_DB_PASS", "gitea_db_password", ""),
    ("CLICKHOUSE_ADMIN_PASS", "clickhouse_admin_password", ""),
    ("WIKIJS_ADMIN_PASS", "wikijs_admin_password", ""),
    ("WIKIJS_DB_PASS", "wikijs_db_password", ""),
    ("WOODPECKER_AGENT_SECRET", "woodpecker_agent_secret", ""),
    ("NOCODB_ADMIN_PASS", "nocodb_admin_password", ""),
    ("NOCODB_DB_PASS", "nocodb_db_password", ""),
    ("NOCODB_JWT_SECRET", "nocodb_jwt_secret", ""),
    ("DINKY_ADMIN_PASS", "dinky_admin_password", ""),
    ("APPSMITH_ENCRYPTION_PASSWORD", "appsmith_encryption_password", ""),
    ("APPSMITH_ENCRYPTION_SALT", "appsmith_encryption_salt", ""),
    ("DIFY_ADMIN_PASS", "dify_admin_password", ""),
    ("DIFY_DB_PASS", "dify_db_password", ""),
    ("DIFY_REDIS_PASS", "dify_redis_password", ""),
    ("DIFY_SECRET_KEY", "dify_secret_key", ""),
    ("DIFY_WEAVIATE_API_KEY", "dify_weaviate_api_key", ""),
    ("DIFY_SANDBOX_API_KEY", "dify_sandbox_api_key", ""),
    ("DIFY_PLUGIN_DAEMON_KEY", "dify_plugin_daemon_key", ""),
    ("DIFY_PLUGIN_INNER_API_KEY", "dify_plugin_inner_api_key", ""),
    ("DOCKERHUB_USER", "dockerhub_username", ""),
    ("DOCKERHUB_TOKEN", "dockerhub_token", ""),
)


class NexusConfig(BaseModel):
    """Typed view of ``tofu output -json secrets``.

    All fields are ``str | None`` to mirror jq's ``// empty`` semantics:
    a missing or null JSON value parses as ``None`` and renders as the
    empty string in :meth:`dump_shell`. Per-field fallbacks (admin
    username, the two ``EXTERNAL_S3_*`` overwrites) are applied at
    :meth:`dump_shell` time, not at parse time, so the round-trip
    (parse → dump → re-parse) is lossless for actual JSON inputs.

    ``frozen=True`` because a config is constructed once per deploy and
    must not mutate; ``extra="ignore"`` so adding a new tofu output key
    doesn't break parsing for unrelated callers (additive evolution).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    admin_username: str | None = None
    infisical_admin_password: str | None = None
    infisical_encryption_key: str | None = None
    infisical_auth_secret: str | None = None
    infisical_db_password: str | None = None
    portainer_admin_password: str | None = None
    kuma_admin_password: str | None = None
    grafana_admin_password: str | None = None
    dagster_db_password: str | None = None
    kestra_admin_password: str | None = None
    kestra_db_password: str | None = None
    n8n_admin_password: str | None = None
    metabase_admin_password: str | None = None
    superset_admin_password: str | None = None
    superset_db_password: str | None = None
    superset_secret_key: str | None = None
    cloudbeaver_admin_password: str | None = None
    mage_admin_password: str | None = None
    minio_root_password: str | None = None
    sftpgo_admin_password: str | None = None
    sftpgo_user_password: str | None = None
    hoppscotch_db_password: str | None = None
    hoppscotch_jwt_secret: str | None = None
    hoppscotch_session_secret: str | None = None
    hoppscotch_encryption_key: str | None = None
    meltano_db_password: str | None = None
    soda_db_password: str | None = None
    redpanda_admin_password: str | None = None
    postgres_password: str | None = None
    pgducklake_password: str | None = None
    hetzner_s3_bucket_pgducklake: str | None = None
    pgadmin_password: str | None = None
    prefect_db_password: str | None = None
    rustfs_root_password: str | None = None
    seaweedfs_admin_password: str | None = None
    garage_admin_token: str | None = None
    garage_rpc_secret: str | None = None
    lakefs_db_password: str | None = None
    lakefs_encrypt_secret: str | None = None
    lakefs_admin_access_key: str | None = None
    lakefs_admin_secret_key: str | None = None
    hetzner_s3_server: str | None = None
    hetzner_s3_region: str | None = None
    hetzner_s3_access_key: str | None = None
    hetzner_s3_secret_key: str | None = None
    hetzner_s3_bucket_lakefs: str | None = None
    hetzner_s3_bucket_general: str | None = None
    external_s3_endpoint: str | None = None
    external_s3_region: str | None = None
    external_s3_access_key: str | None = None
    external_s3_secret_key: str | None = None
    external_s3_bucket: str | None = None
    external_s3_label: str | None = None
    r2_data_endpoint: str | None = None
    r2_data_access_key: str | None = None
    r2_data_secret_key: str | None = None
    r2_data_bucket: str | None = None
    filestash_admin_password: str | None = None
    windmill_admin_password: str | None = None
    windmill_db_password: str | None = None
    windmill_superadmin_secret: str | None = None
    openmetadata_admin_password: str | None = None
    openmetadata_db_password: str | None = None
    openmetadata_airflow_password: str | None = None
    openmetadata_fernet_key: str | None = None
    gitea_admin_password: str | None = None
    gitea_user_password: str | None = None
    gitea_db_password: str | None = None
    clickhouse_admin_password: str | None = None
    wikijs_admin_password: str | None = None
    wikijs_db_password: str | None = None
    woodpecker_agent_secret: str | None = None
    nocodb_admin_password: str | None = None
    nocodb_db_password: str | None = None
    nocodb_jwt_secret: str | None = None
    dinky_admin_password: str | None = None
    appsmith_encryption_password: str | None = None
    appsmith_encryption_salt: str | None = None
    dify_admin_password: str | None = None
    dify_db_password: str | None = None
    dify_redis_password: str | None = None
    dify_secret_key: str | None = None
    dify_weaviate_api_key: str | None = None
    dify_sandbox_api_key: str | None = None
    dify_plugin_daemon_key: str | None = None
    dify_plugin_inner_api_key: str | None = None
    dockerhub_username: str | None = None
    dockerhub_token: str | None = None

    # Schema is exposed for tests + tooling so they don't re-derive it.
    FIELDS: ClassVar[tuple[tuple[str, str, str], ...]] = _FIELDS

    @classmethod
    def from_secrets_json(cls, raw: str) -> NexusConfig:
        """Parse the output of ``tofu output -json secrets``.

        ``raw`` may be the literal string ``"{}"`` (deploy.sh's fallback
        when tofu state is missing), in which case every field is
        ``None`` and :meth:`dump_shell` emits the per-field defaults
        from ``_FIELDS`` — which is exactly what deploy.sh does today.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"SECRETS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigError(f"SECRETS_JSON must be a JSON object, got {type(payload).__name__}")
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:  # pragma: no cover — every field is Optional[str]
            # Reachable only if a future field gains stricter validation.
            raise ConfigError(f"SECRETS_JSON failed validation: {exc}") from exc

    @classmethod
    def from_tofu_output(cls, tofu_dir: Path = Path("tofu/stack")) -> NexusConfig:
        """Run ``tofu output -json secrets`` in ``tofu_dir`` and parse.

        Mirrors deploy.sh:115 exactly — including the "tofu failed →
        treat as empty config" fallback. Operators see the same
        behavior whether deploy.sh runs the jq pipeline or invokes us
        through ``dump-shell`` during the strangler-fig phase.
        """
        try:
            completed = subprocess.run(
                ["tofu", "output", "-json", "secrets"],
                cwd=tofu_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return cls.from_secrets_json("{}")
        return cls.from_secrets_json(completed.stdout)

    def dump_shell(self) -> str:
        """Render bash assignments matching deploy.sh:123-212 byte-for-byte.

        Output is sorted by source-order (the order in :data:`_FIELDS`),
        not alphabetical, so a side-by-side review against the legacy
        deploy.sh block has no spurious reordering noise. Values are
        passed through :func:`shlex.quote` so an embedded ``$``, ``"``,
        backtick, or shell metacharacter can't trigger eval-injection
        — a strict improvement over the legacy ``$()``-based capture
        which was vulnerable to those.
        """
        lines: list[str] = []
        for bash_var, json_key, fallback in _FIELDS:
            value = getattr(self, json_key)
            if value is None or value == "":
                value = fallback
            lines.append(f"{bash_var}={shlex.quote(value)}")
        return "\n".join(lines) + "\n"
