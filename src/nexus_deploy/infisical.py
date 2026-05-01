"""Infisical bootstrap — folder creation + per-folder secret upsert.

Replaces deploy.sh:1996-2390 (the ``build_folder`` helper plus its 39
callers plus the rsync + ssh + curl-loop push, ~395 lines of bash) with
typed Python. The migration is the strangler-fig of #505 Modul 1.1.

Pre-migration shape (deploy.sh):
- Local: ``build_folder NAME K V K V …`` writes per-folder JSON files
  (folder-creation payload + secrets-upsert payload) into
  ``/tmp/infisical-push``.
- Empty ``V`` values are silently skipped (preserves operator UI edits
  per #504); same skip-empty rule applies here in :func:`compute_folders`.
- ``rsync -aq --delete /tmp/infisical-push/ nexus:/tmp/infisical-push/``.
- ``ssh nexus "<curl-loop>"`` — POST /api/v2/folders for f-*.json (200
  + 409 both treated as success), PATCH /api/v4/secrets/batch for
  s-*.json (counted as OK or FAIL by ``"error"`` substring scan).

Post-migration shape (this module):
- :func:`compute_folders` — pure data, takes :class:`NexusConfig` +
  :class:`BootstrapEnv`, returns the list of :class:`FolderSpec` in
  exact source-order. Skip-empty handled here.
- :class:`InfisicalClient` carries project_id / env / token / push_dir.
- :meth:`InfisicalClient.bootstrap` — writes the same JSON files,
  rsyncs, runs the same server-side bash loop. Returns
  :class:`BootstrapResult` with pushed/failed counts.

Why preserve the exact server-side curl loop? It's already proven in
production, it batches all calls into one SSH round-trip (matters for
~80 API calls), and Phase 3's paramiko port-forward replaces it
wholesale. Faithful migration here, real refactor in Phase 3.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from nexus_deploy import _remote
from nexus_deploy.config import NexusConfig

# Server-side Infisical endpoint — matches deploy.sh exactly.
_INFISICAL_HOST = "localhost"
_INFISICAL_PORT = 8070
_FOLDERS_PATH = "/api/v2/folders"
_SECRETS_BATCH_PATH = "/api/v4/secrets/batch"

# Server-side path where the rsync upload lands and the curl loop reads.
_REMOTE_PUSH_DIR = "/tmp/infisical-push"  # noqa: S108 — server-side path matched to deploy.sh's location; transient (rm -rf'd by the curl loop's last step)

# Server-side path where the deploy SSH user can find an
# operator-managed Infisical token; falls back to the env-supplied
# token when absent. Mirrors deploy.sh:2284.
_REMOTE_TOKEN_FALLBACK_FILE = "/opt/docker-server/.infisical-token"  # noqa: S105 — file PATH, not a credential value


@dataclass(frozen=True)
class BootstrapEnv:
    """Configuration values that come from outside ``SECRETS_JSON``.

    deploy.sh's ``build_folder`` calls reference globals that are
    populated from a mix of sources — config.tfvars (DOMAIN,
    ADMIN_EMAIL), workflow inputs (SSH_PRIVATE_KEY_CONTENT,
    WOODPECKER_GITEA_*), other tofu outputs, etc. Rather than reach
    into ``os.environ`` from inside :func:`compute_folders`, we take
    them as a typed dataclass so callers can also build folders from
    fixtures in tests.

    Fields are ``str | None`` so a missing/empty value behaves like
    deploy.sh's ``${VAR:-}`` — the corresponding key is skipped from
    the upsert payload via the per-folder skip-empty pass.
    """

    domain: str | None = None
    admin_email: str | None = None
    gitea_user_email: str | None = None
    gitea_user_username: str | None = None
    gitea_repo_owner: str | None = None
    repo_name: str | None = None
    om_principal_domain: str | None = None
    woodpecker_gitea_client: str | None = None
    woodpecker_gitea_secret: str | None = None
    ssh_private_key_base64: str | None = None


@dataclass(frozen=True)
class FolderSpec:
    """One Infisical folder to create + a dict of secrets to upsert into it.

    ``secrets`` is the ALREADY-FILTERED set: empty/None values were
    dropped at construction time by :func:`_filter_empty`. The
    skip-empty contract from #504 (preserve operator UI edits) is
    enforced here.
    """

    name: str
    secrets: dict[str, str]

    def folder_payload(self, project_id: str, env: str) -> dict[str, str]:
        """Match the bash: ``jq -n '{projectId, environment, name, path: "/"}'``."""
        return {
            "projectId": project_id,
            "environment": env,
            "name": self.name,
            "path": "/",
        }

    def secrets_payload(self, project_id: str, env: str) -> dict[str, object]:
        """Match the bash secrets-batch shape: ``mode: "upsert"`` + secrets list."""
        return {
            "projectId": project_id,
            "environment": env,
            "secretPath": f"/{self.name}",
            "mode": "upsert",
            "secrets": [{"secretKey": k, "secretValue": v} for k, v in self.secrets.items()],
        }


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of an end-to-end bootstrap.

    ``pushed`` and ``failed`` come from the server-side curl loop's
    final ``echo "$OK:$FAIL"`` — they count successful vs errored
    secrets-batch PATCHes, NOT folder POSTs (folder POSTs are
    fire-and-forget per the legacy logic). ``folders_built`` is the
    count of FolderSpecs we wrote to the push dir, including ones that
    ended up with zero secrets after skip-empty.
    """

    folders_built: int
    pushed: int
    failed: int


def _filter_empty(items: Mapping[str, str | None]) -> dict[str, str]:
    """Skip-empty rule from deploy.sh's build_folder + #504 hardening.

    Drops entries where the value is ``None`` or the empty string. The
    bash form was ``[ -z "$2" ] && shift 2 && continue`` — same
    behaviour, typed.
    """
    return {k: v for k, v in items.items() if v is not None and v != ""}


def compute_folders(config: NexusConfig, env: BootstrapEnv) -> list[FolderSpec]:
    """Mirror of deploy.sh:2042-2335 — 39 ``build_folder`` calls in source order.

    Conditional folders (R2, Hetzner-S3, External-S3, SSH, Woodpecker
    OAuth) match the bash gates exactly so the resulting Infisical
    folder set is identical to pre-migration. Order is the same as
    deploy.sh; reviewers comparing against the legacy block see no
    re-ordering noise.
    """
    folders: list[FolderSpec] = []

    # Apply the same fallbacks deploy.sh's jq layer applies BEFORE
    # build_folder runs, so the resolved values get pushed to Infisical
    # — not None / empty (which would skip the key entirely). Mirrors
    # `// "admin"` and the two `EXTERNAL_S3_*={VAR:-default}` lines
    # at deploy.sh:123, 176-177. The same _FIELDS table lives in
    # config.py for the bash-eval handoff; we resolve here to keep
    # Infisical-push parity.
    admin_username = config.admin_username or "admin"
    external_s3_label = config.external_s3_label or "External Storage"
    external_s3_region = config.external_s3_region or "auto"

    folders.append(
        FolderSpec(
            "config",
            _filter_empty(
                {
                    "DOMAIN": env.domain,
                    "ADMIN_EMAIL": env.admin_email,
                    "ADMIN_USERNAME": admin_username,
                }
            ),
        )
    )

    if (
        config.r2_data_endpoint
        and config.r2_data_access_key
        and config.r2_data_secret_key
        and config.r2_data_bucket
    ):
        folders.append(
            FolderSpec(
                "r2-datalake",
                _filter_empty(
                    {
                        "R2_ENDPOINT": config.r2_data_endpoint,
                        "R2_ACCESS_KEY": config.r2_data_access_key,
                        "R2_SECRET_KEY": config.r2_data_secret_key,
                        "R2_BUCKET": config.r2_data_bucket,
                    }
                ),
            )
        )

    if config.hetzner_s3_server and config.hetzner_s3_access_key and config.hetzner_s3_secret_key:
        # Fallback chain for canonical HETZNER_S3_BUCKET (used by ad-hoc
        # workloads): prefer _general (workloads bucket by convention),
        # fall back to _lakefs (always populated when LakeFS-aware path runs).
        # See deploy.sh:2069-2087 for the rationale.
        default_bucket = config.hetzner_s3_bucket_general or config.hetzner_s3_bucket_lakefs or ""
        folders.append(
            FolderSpec(
                "hetzner-s3",
                _filter_empty(
                    {
                        "HETZNER_S3_ENDPOINT": f"https://{config.hetzner_s3_server}",
                        "HETZNER_S3_REGION": config.hetzner_s3_region,
                        "HETZNER_S3_ACCESS_KEY": config.hetzner_s3_access_key,
                        "HETZNER_S3_SECRET_KEY": config.hetzner_s3_secret_key,
                        "HETZNER_S3_BUCKET": default_bucket,
                        "HETZNER_S3_BUCKET_LAKEFS": config.hetzner_s3_bucket_lakefs,
                        "HETZNER_S3_BUCKET_GENERAL": config.hetzner_s3_bucket_general,
                        "HETZNER_S3_BUCKET_PGDUCKLAKE": config.hetzner_s3_bucket_pgducklake,
                    }
                ),
            )
        )

    if (
        config.external_s3_endpoint
        and config.external_s3_access_key
        and config.external_s3_secret_key
        and config.external_s3_bucket
    ):
        folders.append(
            FolderSpec(
                "external-s3",
                _filter_empty(
                    {
                        "EXTERNAL_S3_ENDPOINT": config.external_s3_endpoint,
                        "EXTERNAL_S3_REGION": external_s3_region,
                        "EXTERNAL_S3_ACCESS_KEY": config.external_s3_access_key,
                        "EXTERNAL_S3_SECRET_KEY": config.external_s3_secret_key,
                        "EXTERNAL_S3_BUCKET": config.external_s3_bucket,
                        "EXTERNAL_S3_LABEL": external_s3_label,
                    }
                ),
            )
        )

    folders.append(
        FolderSpec(
            "infisical",
            _filter_empty(
                {
                    "INFISICAL_USERNAME": env.admin_email,
                    "INFISICAL_PASSWORD": config.infisical_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "portainer",
            _filter_empty(
                {
                    "PORTAINER_USERNAME": admin_username,
                    "PORTAINER_PASSWORD": config.portainer_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "uptime-kuma",
            _filter_empty(
                {
                    "UPTIME_KUMA_USERNAME": admin_username,
                    "UPTIME_KUMA_PASSWORD": config.kuma_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "grafana",
            _filter_empty(
                {
                    "GRAFANA_USERNAME": admin_username,
                    "GRAFANA_PASSWORD": config.grafana_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "n8n",
            _filter_empty(
                {
                    "N8N_USERNAME": env.admin_email,
                    "N8N_PASSWORD": config.n8n_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "dagster",
            _filter_empty({"DAGSTER_DB_PASSWORD": config.dagster_db_password}),
        )
    )
    folders.append(
        FolderSpec(
            "kestra",
            _filter_empty(
                {
                    "KESTRA_USERNAME": env.admin_email,
                    "KESTRA_PASSWORD": config.kestra_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "metabase",
            _filter_empty(
                {
                    "METABASE_USERNAME": env.admin_email,
                    "METABASE_PASSWORD": config.metabase_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "superset",
            _filter_empty(
                {
                    "SUPERSET_USERNAME": "admin",
                    "SUPERSET_PASSWORD": config.superset_admin_password,
                    "SUPERSET_DB_PASSWORD": config.superset_db_password,
                    "SUPERSET_SECRET_KEY": config.superset_secret_key,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "cloudbeaver",
            _filter_empty(
                {
                    "CLOUDBEAVER_USERNAME": "nexus-cloudbeaver",
                    "CLOUDBEAVER_PASSWORD": config.cloudbeaver_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "mage",
            _filter_empty(
                {
                    "MAGE_USERNAME": env.gitea_user_email or env.admin_email,
                    "MAGE_PASSWORD": config.mage_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "minio",
            _filter_empty(
                {
                    "MINIO_ROOT_USER": "nexus-minio",
                    "MINIO_ROOT_PASSWORD": config.minio_root_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "sftpgo",
            _filter_empty(
                {
                    "SFTPGO_ADMIN_USERNAME": "nexus-sftpgo",
                    "SFTPGO_ADMIN_PASSWORD": config.sftpgo_admin_password,
                    "SFTPGO_USER_USERNAME": "nexus-default",
                    "SFTPGO_USER_PASSWORD": config.sftpgo_user_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "nocodb",
            _filter_empty(
                {
                    "NOCODB_USERNAME": env.admin_email,
                    "NOCODB_PASSWORD": config.nocodb_admin_password,
                    "NOCODB_DB_PASSWORD": config.nocodb_db_password,
                    "NOCODB_JWT_SECRET": config.nocodb_jwt_secret,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "appsmith",
            _filter_empty(
                {
                    "APPSMITH_ENCRYPTION_PASSWORD": config.appsmith_encryption_password,
                    "APPSMITH_ENCRYPTION_SALT": config.appsmith_encryption_salt,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "dinky",
            _filter_empty(
                {
                    "DINKY_USERNAME": "admin",
                    "DINKY_PASSWORD": config.dinky_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "dify",
            _filter_empty(
                {
                    "DIFY_USERNAME": env.admin_email,
                    "DIFY_PASSWORD": config.dify_admin_password,
                    "DIFY_DB_PASSWORD": config.dify_db_password,
                    "DIFY_SECRET_KEY": config.dify_secret_key,
                    "DIFY_REDIS_PASSWORD": config.dify_redis_password,
                    "DIFY_WEAVIATE_API_KEY": config.dify_weaviate_api_key,
                    "DIFY_SANDBOX_API_KEY": config.dify_sandbox_api_key,
                    "DIFY_PLUGIN_DAEMON_KEY": config.dify_plugin_daemon_key,
                    "DIFY_PLUGIN_INNER_API_KEY": config.dify_plugin_inner_api_key,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "rustfs",
            _filter_empty(
                {
                    "RUSTFS_ACCESS_KEY": "nexus-rustfs",
                    "RUSTFS_SECRET_KEY": config.rustfs_root_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "seaweedfs",
            _filter_empty(
                {
                    "SEAWEEDFS_ACCESS_KEY": "nexus-seaweedfs",
                    "SEAWEEDFS_SECRET_KEY": config.seaweedfs_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "garage",
            _filter_empty({"GARAGE_ADMIN_TOKEN": config.garage_admin_token}),
        )
    )
    folders.append(
        FolderSpec(
            "lakefs",
            _filter_empty(
                {
                    "LAKEFS_DB_PASSWORD": config.lakefs_db_password,
                    "LAKEFS_ACCESS_KEY_ID": config.lakefs_admin_access_key,
                    "LAKEFS_SECRET_ACCESS_KEY": config.lakefs_admin_secret_key,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "filestash",
            _filter_empty(
                {
                    "FILESTASH_S3_BUCKET": config.hetzner_s3_bucket_general,
                    "FILESTASH_ADMIN_PASSWORD": config.filestash_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "redpanda",
            _filter_empty(
                {
                    "REDPANDA_SASL_USERNAME": "nexus-redpanda",
                    "REDPANDA_SASL_PASSWORD": config.redpanda_admin_password,
                    "REDPANDA_KAFKA_PUBLIC_URL": (
                        f"redpanda-kafka.{env.domain}:9092" if env.domain else None
                    ),
                    "REDPANDA_SCHEMA_REGISTRY_PUBLIC_URL": (
                        f"redpanda-schema-registry.{env.domain}:18081" if env.domain else None
                    ),
                    "REDPANDA_ADMIN_PUBLIC_URL": (
                        f"redpanda-admin.{env.domain}:9644" if env.domain else None
                    ),
                    "REDPANDA_CONNECT_PUBLIC_URL": (
                        f"redpanda-connect-api.{env.domain}:4195" if env.domain else None
                    ),
                }
            ),
        )
    )
    folders.append(
        FolderSpec("meltano", _filter_empty({"MELTANO_DB_PASSWORD": config.meltano_db_password}))
    )
    folders.append(
        FolderSpec(
            "postgres",
            _filter_empty(
                {
                    "POSTGRES_USERNAME": "nexus-postgres",
                    "POSTGRES_PASSWORD": config.postgres_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "pg-ducklake",
            _filter_empty(
                {
                    "PG_DUCKLAKE_USERNAME": "nexus-pgducklake",
                    "PG_DUCKLAKE_PASSWORD": config.pgducklake_password,
                    "PG_DUCKLAKE_DATABASE": "ducklake",
                    "PG_DUCKLAKE_S3_BUCKET": config.hetzner_s3_bucket_pgducklake,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "pgadmin",
            _filter_empty(
                {
                    "PGADMIN_USERNAME": env.admin_email,
                    "PGADMIN_PASSWORD": config.pgadmin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec("prefect", _filter_empty({"PREFECT_DB_PASSWORD": config.prefect_db_password}))
    )
    folders.append(
        FolderSpec(
            "windmill",
            _filter_empty(
                {
                    "WINDMILL_ADMIN_EMAIL": env.admin_email,
                    "WINDMILL_ADMIN_PASSWORD": config.windmill_admin_password,
                    "WINDMILL_DB_PASSWORD": config.windmill_db_password,
                    "WINDMILL_SUPERADMIN_SECRET": config.windmill_superadmin_secret,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "openmetadata",
            _filter_empty(
                {
                    "OPENMETADATA_USERNAME": (
                        f"admin@{env.om_principal_domain}" if env.om_principal_domain else None
                    ),
                    "OPENMETADATA_PASSWORD": config.openmetadata_admin_password,
                    "OPENMETADATA_DB_PASSWORD": config.openmetadata_db_password,
                }
            ),
        )
    )
    # Gitea: GITEA_REPO_URL is built from DOMAIN + repo_owner + repo_name
    # with the same `${REPO_NAME:-nexus-${DOMAIN//./-}-gitea}` fallback
    # the bash carried at L2300.
    repo_name = env.repo_name or (
        f"nexus-{env.domain.replace('.', '-')}-gitea" if env.domain else None
    )
    repo_owner = env.gitea_repo_owner or admin_username
    gitea_repo_url = (
        f"https://git.{env.domain}/{repo_owner}/{repo_name}.git"
        if env.domain and repo_owner and repo_name
        else None
    )
    folders.append(
        FolderSpec(
            "gitea",
            _filter_empty(
                {
                    "GITEA_ADMIN_USERNAME": admin_username,
                    "GITEA_ADMIN_PASSWORD": config.gitea_admin_password,
                    "GITEA_USER_USERNAME": env.gitea_user_username,
                    "GITEA_USER_PASSWORD": config.gitea_user_password,
                    "GITEA_REPO_URL": gitea_repo_url,
                    "GITEA_DB_PASSWORD": config.gitea_db_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "clickhouse",
            _filter_empty(
                {
                    "CLICKHOUSE_USERNAME": "nexus-clickhouse",
                    "CLICKHOUSE_PASSWORD": config.clickhouse_admin_password,
                }
            ),
        )
    )
    folders.append(
        FolderSpec(
            "wikijs",
            _filter_empty(
                {
                    "WIKIJS_USERNAME": env.gitea_user_email or env.admin_email,
                    "WIKIJS_PASSWORD": config.wikijs_admin_password,
                    "WIKIJS_DB_PASSWORD": config.wikijs_db_password,
                }
            ),
        )
    )
    # Woodpecker: agent_secret unconditional, OAuth pair optional.
    woodpecker_secrets: dict[str, str | None] = {
        "WOODPECKER_AGENT_SECRET": config.woodpecker_agent_secret,
    }
    if env.woodpecker_gitea_client:
        woodpecker_secrets["WOODPECKER_GITEA_CLIENT"] = env.woodpecker_gitea_client
    if env.woodpecker_gitea_secret:
        woodpecker_secrets["WOODPECKER_GITEA_SECRET"] = env.woodpecker_gitea_secret
    folders.append(FolderSpec("woodpecker", _filter_empty(woodpecker_secrets)))

    if env.ssh_private_key_base64:
        folders.append(
            FolderSpec(
                "ssh",
                _filter_empty({"SSH_PRIVATE_KEY_BASE64": env.ssh_private_key_base64}),
            )
        )

    return folders


# Type aliases for the runner injection points used in tests.
SshRunner = Callable[[str], subprocess.CompletedProcess[str]]
RsyncRunner = Callable[[Path, str], subprocess.CompletedProcess[str]]


@dataclass
class InfisicalClient:
    """Bundles the project_id / env / token / push_dir for a bootstrap call.

    Stateless except for the ``push_dir`` it manages on the local
    filesystem. The actual server-side execution is via the injected
    ``ssh_runner`` / ``rsync_runner`` callables — defaults wire to
    :mod:`nexus_deploy._remote`, tests pass mocks.
    """

    project_id: str
    env: str
    token: str
    push_dir: Path = Path("/tmp/infisical-push")  # noqa: S108 - matches deploy.sh's
    # public location; the same dir on the server is what the curl loop
    # reads. There's nothing secret in the path itself; the JSON files
    # inside contain secret values but are removed by the server-side
    # `rm -rf` at the end of the bootstrap.

    def encode_payloads(self, folders: list[FolderSpec]) -> dict[str, str]:
        """Return the f-NAME.json + s-NAME.json file-name → JSON-text mapping.

        Pure function — useful for tests that want to verify the exact
        bytes that would be written to disk without touching the
        filesystem.

        Output uses ``json.dumps(..., separators=(",", ":"),
        sort_keys=False)`` to keep the encoding compact and stable
        without imposing alphabetical ordering on the secrets list
        (deploy.sh's jq emitted source-order via the explicit jq
        filter; we preserve the same convention).
        """
        out: dict[str, str] = {}
        for spec in folders:
            out[f"f-{spec.name}.json"] = json.dumps(
                spec.folder_payload(self.project_id, self.env), separators=(",", ":")
            )
            out[f"s-{spec.name}.json"] = json.dumps(
                spec.secrets_payload(self.project_id, self.env), separators=(",", ":")
            )
        return out

    def _build_remote_loop(self) -> str:
        """Build the server-side bash that POSTs folders + PATCHes secrets.

        Mirrors deploy.sh:2280-2300 byte-for-byte (modulo one
        difference: the token is shlex-quoted on the way through, since
        we're now interpolating into a remote bash script generated
        from Python — see the comment block in ``dump_shell()``).
        """
        token_quoted = shlex.quote(self.token)
        folders_url = f"http://{_INFISICAL_HOST}:{_INFISICAL_PORT}{_FOLDERS_PATH}"
        secrets_url = f"http://{_INFISICAL_HOST}:{_INFISICAL_PORT}{_SECRETS_BATCH_PATH}"
        # The `\$f` / `\$TOKEN` etc. escaping is gone here vs deploy.sh —
        # that escaping was only needed because deploy.sh interpolated
        # the heredoc through a layer of bash before ssh saw it. We
        # send raw text directly to ssh, so the inner `$f` expands at
        # the remote bash, which is what we want.
        #
        # `printf '%s'` instead of `echo`: bash's built-in `echo` can
        # eat a leading `-n` / `-e` / `-E` as an option flag, blanking
        # the captured TOKEN if a token happens to start with one.
        # Infisical tokens are alphanumeric in practice, but the
        # printf form costs nothing and rules out the edge case.
        return f"""
TOKEN=$(cat {_REMOTE_TOKEN_FALLBACK_FILE} 2>/dev/null || printf '%s' {token_quoted})
if [ -z "$TOKEN" ]; then echo '0:0'; exit 0; fi
OK=0; FAIL=0
for f in {_REMOTE_PUSH_DIR}/f-*.json; do
    curl -s -X POST '{folders_url}' \\
        -H "Authorization: Bearer $TOKEN" \\
        -H 'Content-Type: application/json' \\
        -d @"$f" >/dev/null 2>&1 || true
done
for f in {_REMOTE_PUSH_DIR}/s-*.json; do
    RESULT=$(curl -s -X PATCH '{secrets_url}' \\
        -H "Authorization: Bearer $TOKEN" \\
        -H 'Content-Type: application/json' \\
        -d @"$f" 2>&1)
    if echo "$RESULT" | grep -q '"error"'; then
        FAIL=$((FAIL+1))
    else
        OK=$((OK+1))
    fi
done
rm -rf {_REMOTE_PUSH_DIR}
echo "$OK:$FAIL"
"""

    def bootstrap(
        self,
        folders: list[FolderSpec],
        *,
        ssh_runner: SshRunner | None = None,
        rsync_runner: RsyncRunner | None = None,
    ) -> BootstrapResult:
        """Write payloads, rsync, run the curl loop. Return push counts.

        Default runners come from :mod:`nexus_deploy._remote`; tests
        override both via the kwargs.

        Local payload files (which contain secret values) are removed
        in a ``finally`` block whether the rsync/ssh succeeds, fails,
        or raises. Mirrors deploy.sh:2370 (`rm -rf "$PUSH_DIR"`); the
        server-side ``/tmp/infisical-push`` is removed by the curl
        loop's last step. No secrets-at-rest on either end after a
        bootstrap call returns.

        The remote bash script is fed to ``ssh nexus bash -s`` via
        stdin (:func:`_remote.ssh_run_script`), NOT as an argv. The
        script embeds the Infisical token (shlex-quoted), and stdin
        keeps it out of ``ps``, CI argv-logging, and any
        ``CalledProcessError`` / ``TimeoutExpired`` exception messages
        that would otherwise dump the full argv.
        """
        ssh = ssh_runner or (lambda script: _remote.ssh_run_script(script))
        rsync = rsync_runner or (
            lambda local, remote: _remote.rsync_to_remote(local, remote, delete=True)
        )

        # Restrictive perms because the JSON contains secret values.
        # Dir 0o700 + files 0o600 mean only the owner can read; rsync
        # preserves perms by default, so the server-side mirror inherits
        # the same protection.
        self.push_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Re-chmod in case the dir pre-existed with looser perms (mkdir's
        # mode is only applied at creation, ignored when exist_ok=True
        # and the dir is already there).
        self.push_dir.chmod(0o700)

        # ENTIRE materialise+push+execute path is wrapped in try/finally
        # so the cleanup of secret-bearing files runs even if write_text
        # / chmod / rsync / ssh fails mid-flight. The previous version
        # only wrapped the rsync+ssh phase, leaving the write loop
        # outside protection — a disk-full or permission error during
        # writes would leave half-written f-/s-*.json files in push_dir
        # with secret values still in them.
        try:
            # 1a. Clear stale files from prior runs so deleted folders
            #     don't ship to the server (matches `rsync --delete`
            #     semantics on the upload side).
            for stale in self.push_dir.glob("[fs]-*.json"):
                stale.unlink()

            # 1b. Atomic create-with-mode-0o600 via os.open. Avoids
            #     the TOCTOU race of `write_text` then `chmod`, where
            #     the file briefly exists with the umask-derived mode
            #     (often 0o644) before the chmod tightens it.
            for filename, body in self.encode_payloads(folders).items():
                payload_path = self.push_dir / filename
                fd = os.open(
                    str(payload_path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(fd, "w") as f:
                    f.write(body)

            # 2. rsync to server.
            rsync(self.push_dir, f"nexus:{_REMOTE_PUSH_DIR}/")

            # 3. Run the server-side curl loop.
            completed = ssh(self._build_remote_loop())

            # 4. Parse the final `OK:FAIL` line. The server's stdout
            #    may include earlier echoes (warnings from the
            #    baseline-capture step in deploy.sh); take the last line.
            last_line = completed.stdout.strip().splitlines()[-1] if completed.stdout else "0:0"
            try:
                ok_str, fail_str = last_line.split(":", 1)
                pushed = int(ok_str)
                failed = int(fail_str)
            except (ValueError, IndexError):
                # Unparseable output is itself a failure signal.
                pushed = 0
                failed = len(folders)

            return BootstrapResult(folders_built=len(folders), pushed=pushed, failed=failed)
        finally:
            # Best-effort: secret-bearing payloads must not survive a
            # bootstrap call (success OR failure). We delete only the
            # f-/s-*.json files we wrote, not the directory itself —
            # the dir may pre-exist with operator state we don't own.
            for payload in self.push_dir.glob("[fs]-*.json"):
                payload.unlink(missing_ok=True)
