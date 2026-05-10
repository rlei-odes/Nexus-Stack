"""S3-backed persistence for stack data (RFC 0001).

Replaces the per-stack Hetzner Block Storage volume with Hetzner
Object Storage as the canonical persistence layer. Server local
SSD becomes ephemeral cache; on spinup we restore from S3, on
teardown we snapshot to S3 *atomically* (verify before destroy).

This module follows the same pattern as ``setup.py``: pure rendering
functions that return server-side bash. Actual execution happens via
:class:`SSHClient` in the orchestrator pipeline. Two upsides:

1. Tests are subprocess-free — we assert on the rendered string.
2. The SSHClient already handles connection pooling, error
   propagation and structured logging; we don't reinvent that.

Public surface:

* :class:`S3Endpoint` — frozen ``(endpoint, region, access_key,
  secret_key, bucket)`` tuple. The credentials are intentionally
  passed in rather than read from the environment so the rendered
  script never relies on ambient state — and so unit tests can
  inject a fixture without touching real Hetzner credentials.
* :class:`SnapshotManifest` — Python-level dataclass + JSON
  serialiser for the snapshot metadata. The version-1.0 *rendered*
  bash writes a slim manifest (timestamp, stack, template version)
  and relies on rclone's ETag check for integrity, so v1.0
  ``manifest.json`` files in S3 carry no per-component checksums.
  This dataclass + :func:`manifest_for_components` exist for
  callers that need to compute and emit per-component checksums
  client-side — currently used only by tests and a planned v1.1
  cleanup-and-verify script. See "Open question 1" in
  ``docs/proposals/0001-s3-persistence.md``.
* :func:`render_rclone_config` — produces a ``[hetzner-s3]`` rclone
  profile block from an :class:`S3Endpoint`. Written to
  ``~/.config/rclone/rclone.conf`` on the server. Idempotent — the
  block is identified by name and replaced wholesale on every
  spinup so credential rotation is a single render away.
* :func:`render_snapshot_script` — bash that pauses the relevant
  docker compose stacks, runs ``pg_dump`` for each Postgres
  database we care about, rsyncs ``/var/lib/nexus-data/`` to S3,
  writes the manifest, and exits with rc=0 only if every
  ``rclone check`` passed. Caller is responsible for treating
  rc≠0 as "abort teardown — leave the server up".
* :func:`render_restore_script` — bash that reads the latest
  manifest, rclone-syncs the snapshot to ``/var/lib/nexus-data/``,
  and runs ``pg_restore`` for each Postgres dump. Idempotent on
  the empty-S3 case (first-time spinup).

Why no client-side rclone bindings: rclone is a Go binary that
ships as a single static executable. Driving it via subprocess
from Python on the orchestrator would mean a) shipping rclone in
the dev environment, b) reasoning about cross-platform binary
selection, c) duplicating the credential handling we already need
to do remote-side. Generating bash that the remote runs keeps the
boundary clean.

Design choices for v1.0 (see RFC 0001 in
``docs/proposals/0001-s3-persistence.md`` for the full reasoning):

* **Hetzner Object Storage**, not Cloudflare R2 — operator
  preference for EU data-residency. The endpoint URL is the only
  S3-flavour-specific bit; switching to R2 later is an
  ``S3Endpoint`` constructor argument away.
* **Bucket per stack** — one Hetzner bucket per
  ``<class>-<user>`` slug. Easier blast-radius isolation than
  ``<bucket>/<stack>/...`` prefixes.
* **rsync (rclone) for everything in v1.0** — Gitea LFS and Dify
  storage have native S3 backends but that's deferred to v1.1.
  v1.0 keeps the docker-compose layout untouched and drives
  persistence purely via rclone sync of the bind-mount directory.
* **Snapshot-versioning strategy**: timestamped directories under
  ``snapshots/<ISO8601>/`` plus a ``snapshots/latest`` pointer
  file. Retention (last 7 daily + 4 weekly) is enforced by a
  separate cleanup script — out of scope for this module.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------------------
# Identifier shape — protects the rendered bash from injection
# ---------------------------------------------------------------------------

# Hetzner location names (`fsn1`, `hel1`, `nbg1`) are lowercase
# alphanumeric with optional dashes. The bucket name follows S3 rules
# (3-63 chars, lowercase, digits, hyphens). We're strict on both
# because they're interpolated into rendered bash without further
# escaping; a value containing ``$``, ``;`` or backticks would let an
# attacker who controlled the value execute arbitrary commands on
# the server. Hetzner's own identifiers are always within this set,
# so the gate is conservative on legitimate input.
_HETZNER_REGION = re.compile(r"^[a-z0-9-]+$")
_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_ACCESS_KEY = re.compile(r"^[A-Za-z0-9]+$")
# Hetzner Object Storage secret keys are base64-ish (40-80 chars).
# We allow the full set of base64 + URL-safe characters so we don't
# reject a future format change, but still gate against bash
# metacharacters.
_SECRET_KEY = re.compile(r"^[A-Za-z0-9+/=_-]+$")
# Postgres identifier shape — applies to both database names and
# role names interpolated into rendered SQL (``DROP DATABASE
# {pg.database}``, ``CREATE DATABASE ... OWNER {pg.user}``). PG
# itself permits a wider character set when identifiers are
# double-quoted, but we deliberately don't accept that complexity:
# every database/user we manage today matches this strict shape, and
# an attacker who controls the value should be rejected at config
# time, not handled with quoting acrobatics.
_PG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class S3PersistenceError(Exception):
    """Raised when an :class:`S3Endpoint` is constructed with values
    that would be unsafe to interpolate into a rendered bash script.

    We surface this as an exception (not a silent ``ValueError``) so
    the CLI handler can give the operator a clear "your config has
    a bad value" message rather than a confusing rendered-bash
    failure later in the pipeline.
    """


# ---------------------------------------------------------------------------
# Endpoint + manifest data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class S3Endpoint:
    """Hetzner Object Storage connection coordinates.

    All five fields are required: missing credentials are an error
    surfaced at construction time, not at script-render time, so the
    operator gets a stack trace that points at the *source* of the
    missing value (config, secret store, …) rather than a confusing
    rclone error on the remote.

    ``endpoint`` is the full URL (e.g.
    ``https://fsn1.your-objectstorage.com``) so we don't have to
    assume the URL shape. ``region`` is the short code (``fsn1``)
    needed by the S3 v4 signing protocol — Hetzner requires it to
    match the location.
    """

    endpoint: str
    region: str
    access_key: str
    secret_key: str
    bucket: str

    def __post_init__(self) -> None:
        # Endpoint must be an http(s) URL — trivial guard against
        # accidentally passing the bucket name into the endpoint slot.
        if not self.endpoint.startswith(("http://", "https://")):
            raise S3PersistenceError(
                f"S3Endpoint.endpoint must start with http(s)://: {self.endpoint!r}",
            )
        for name, value, pattern in (
            ("region", self.region, _HETZNER_REGION),
            ("bucket", self.bucket, _BUCKET_NAME),
            ("access_key", self.access_key, _ACCESS_KEY),
            ("secret_key", self.secret_key, _SECRET_KEY),
        ):
            if not pattern.fullmatch(value):
                raise S3PersistenceError(
                    f"S3Endpoint.{name} contains characters that would be unsafe to "
                    f"interpolate into rendered bash; got {value!r}",
                )


@dataclass(frozen=True)
class ComponentSnapshot:
    """One component's contribution to the snapshot manifest.

    Tracks size + checksum so the restore-side can detect a partial
    or corrupt upload before it pollutes the live system. ``path``
    is the relative S3 key under the snapshot's timestamped
    directory; the remote bash uses it both as the rclone source and
    as the lookup key in the manifest.
    """

    name: str
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class SnapshotManifest:
    """The ``manifest.json`` written at the root of every snapshot.

    Versioned: a future v2 manifest can carry additional fields
    without breaking forward-compat — the restore-side reads
    ``version`` and dispatches accordingly. For v1.0 we keep one
    flat shape covering the four components we care about today
    (Gitea repos+lfs+postgres, Dify storage+postgres+weaviate).
    """

    version: int = 1
    created_at: str = ""
    stack: str = ""
    template_version: str = ""
    components: tuple[ComponentSnapshot, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        """Serialise to indented JSON — written to ``manifest.json``
        at the root of the timestamped snapshot directory.

        Indented because it's read by humans during ops/debugging,
        and 200-300 bytes of whitespace doesn't move the needle on
        Hetzner egress costs.
        """
        return json.dumps(
            {
                **asdict(self),
                "components": [asdict(c) for c in self.components],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> SnapshotManifest:
        """Parse a manifest written by an earlier teardown.

        Raises :class:`S3PersistenceError` for any structural
        problem so the caller (restore script) can hard-fail
        before pulling potentially-corrupt data.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise S3PersistenceError(
                f"manifest.json is not valid JSON: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise S3PersistenceError(
                f"manifest.json root must be an object, got {type(data).__name__}",
            )
        version = data.get("version")
        if version != 1:
            raise S3PersistenceError(
                f"manifest.json version {version!r} is not supported (expected 1)",
            )
        components_raw = data.get("components", [])
        if not isinstance(components_raw, list):
            raise S3PersistenceError(
                "manifest.json 'components' must be a list",
            )
        # Each component must be a dict with the four expected keys.
        # The previous implementation indexed straight into ``c[...]``
        # which raised KeyError/TypeError on corrupt input — a class
        # of failure the docstring explicitly promises to surface as
        # S3PersistenceError. Validate each entry explicitly so a
        # malformed manifest produces an actionable error rather than
        # a confusing KeyError stack trace from the restore path.
        components: list[ComponentSnapshot] = []
        for idx, c in enumerate(components_raw):
            if not isinstance(c, dict):
                raise S3PersistenceError(
                    f"manifest.json components[{idx}] must be an object, got {type(c).__name__}",
                )
            for key in ("name", "path", "size_bytes", "sha256"):
                if key not in c:
                    raise S3PersistenceError(
                        f"manifest.json components[{idx}] is missing required key {key!r}",
                    )
            try:
                components.append(
                    ComponentSnapshot(
                        name=str(c["name"]),
                        path=str(c["path"]),
                        size_bytes=int(c["size_bytes"]),
                        sha256=str(c["sha256"]),
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise S3PersistenceError(
                    f"manifest.json components[{idx}] has a bad value: {exc}",
                ) from exc
        return cls(
            version=1,
            created_at=str(data.get("created_at", "")),
            stack=str(data.get("stack", "")),
            template_version=str(data.get("template_version", "")),
            components=tuple(components),
        )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


# rclone profile name. Used as the destination prefix in rclone
# commands (``rclone sync /local hetzner-s3:bucket/path``). Picked
# at module level so the config-render and the script-render can't
# drift apart.
RCLONE_PROFILE = "hetzner-s3"


def render_rclone_config(endpoint: S3Endpoint) -> str:
    """Render the ``[hetzner-s3]`` rclone profile block.

    The output is the *full* config file content, not a diff.
    Caller writes it atomically to ``~/.config/rclone/rclone.conf``
    on the server (overwrite-with-tempfile pattern) so a partial
    write can't leave the file in a state where rclone reads
    half-old half-new credentials.
    """
    return (
        f"[{RCLONE_PROFILE}]\n"
        "type = s3\n"
        "provider = Other\n"
        "env_auth = false\n"
        f"access_key_id = {endpoint.access_key}\n"
        f"secret_access_key = {endpoint.secret_key}\n"
        f"endpoint = {endpoint.endpoint}\n"
        f"region = {endpoint.region}\n"
        # `acl = private` is the Hetzner default but spelling it
        # out makes the intent explicit and protects against a
        # future rclone-default change.
        "acl = private\n"
    )


@dataclass(frozen=True)
class PostgresDumpTarget:
    """One Postgres database to dump on teardown / restore on spinup.

    ``container`` is the docker-compose service name (e.g.
    ``gitea-db``); ``database`` is the PG database name (often the
    same as ``user``); ``user`` is the role used for pg_dump and
    pg_restore. We pass these in (rather than infer them) so the
    same module supports any new stateful stack — the caller in
    pipeline.py decides which databases to back up.

    All three fields are charset-validated at construction so the
    rendered bash + SQL never sees a value that could break out of
    interpolation. ``container`` matches the Hetzner-region shape
    (alnum + dash, lowercase) — every docker-compose service in
    this codebase uses that style. ``database`` and ``user`` match
    the strict Postgres-identifier subset (``[A-Za-z_][A-Za-z0-9_-]*``)
    we use across all stacks; we deliberately don't accept the
    wider double-quoted-identifier space because no service we ship
    needs it and it would force quoting acrobatics in the rendered
    SQL.
    """

    container: str
    database: str
    user: str

    def __post_init__(self) -> None:
        if not _HETZNER_REGION.fullmatch(self.container):
            raise S3PersistenceError(
                f"PostgresDumpTarget.container must match docker-service-name shape "
                f"(lowercase alnum + dash): {self.container!r}",
            )
        for name, value in (("database", self.database), ("user", self.user)):
            if not _PG_IDENTIFIER.fullmatch(value):
                raise S3PersistenceError(
                    f"PostgresDumpTarget.{name} must match strict PG identifier shape "
                    f"([A-Za-z_][A-Za-z0-9_-]*): {value!r}",
                )


@dataclass(frozen=True)
class RsyncTarget:
    """One filesystem subtree to mirror to/from S3.

    ``local_path`` is the absolute path under
    ``/var/lib/nexus-data/`` (the post-volume layout). ``s3_subpath``
    is the relative key under the snapshot's timestamped directory
    — kept short and stable across snapshots so a future
    diff-based optimisation has a useful key to compare against.
    """

    name: str
    local_path: str
    s3_subpath: str


def render_snapshot_script(
    *,
    endpoint: S3Endpoint,
    stack_slug: str,
    template_version: str,
    timestamp: str,
    postgres_targets: Iterable[PostgresDumpTarget],
    rsync_targets: Iterable[RsyncTarget],
    pause_compose_files: Iterable[str] = (),
) -> str:
    """Render the bash that snapshots the live stack to S3.

    Steps the rendered script performs (in order, ``set -euo
    pipefail`` throughout — first failure aborts):

    1. ``docker compose pause`` for every file in
       ``pause_compose_files``. This lets in-flight HTTP requests
       drain naturally instead of being killed mid-write.
    2. ``pg_dump`` (compressed) for each postgres target into
       ``/tmp/nexus-snapshot/postgres/<db>.sql.gz``.
    3. ``rclone sync`` each rsync target into the timestamped S3
       directory ``snapshots/<timestamp>/<subpath>``.
    4. Upload the postgres dumps under
       ``snapshots/<timestamp>/postgres/``.
    5. Compute sha256 + size for each component, write
       ``manifest.json``, upload it.
    6. ``rclone check`` re-reads the upload and compares ETags —
       this is the gate the caller checks for atomicity.
    7. Update ``snapshots/latest`` to point at the new timestamp.

    On any non-zero step the script prints a clear ``✗
    snapshot-failed: ...`` line and exits non-zero. Pipeline.py
    interprets that as "do NOT proceed to tofu destroy".

    Side note on shlex.quote: every interpolated value is gated
    upstream by :class:`S3Endpoint`'s charset checks and the
    ``stack_slug``/``timestamp`` regexes below — but we still
    ``shlex.quote`` belt-and-suspenders to keep the rendered bash
    safe even if a future caller bypasses the constructor.
    """
    if not _BUCKET_NAME.fullmatch(stack_slug):
        raise S3PersistenceError(
            f"stack_slug must match S3 bucket-name shape: {stack_slug!r}",
        )
    # ISO-8601 with safe filesystem chars only (no ``:``, since some
    # tools — including rclone on Windows-share remotes — choke on
    # colons). Caller decides the format; we just verify it's
    # injection-safe.
    if not re.fullmatch(r"[0-9A-Za-z_-]+", timestamp):
        raise S3PersistenceError(
            f"timestamp must be alphanumeric/underscore/dash only: {timestamp!r}",
        )

    pg_targets = tuple(postgres_targets)
    rs_targets = tuple(rsync_targets)
    pause_files = tuple(pause_compose_files)

    bucket_url = f"{RCLONE_PROFILE}:{shlex.quote(endpoint.bucket)}"
    snapshot_prefix = f"snapshots/{shlex.quote(timestamp)}"

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# Generated by nexus_deploy.s3_persistence — do not edit by hand.",
        "set -euo pipefail",
        "",
        f"STACK={shlex.quote(stack_slug)}",
        f"TIMESTAMP={shlex.quote(timestamp)}",
        f"TEMPLATE_VERSION={shlex.quote(template_version)}",
        f"BUCKET={bucket_url}",
        f"SNAPSHOT_PREFIX={snapshot_prefix}",
        "WORKDIR=/tmp/nexus-snapshot",
        "POSTGRES_DIR=$WORKDIR/postgres",
        "",
        'echo "→ snapshot: preparing workdir"',
        'rm -rf "$WORKDIR"',
        'mkdir -p "$POSTGRES_DIR"',
        "",
    ]

    if pause_files:
        lines.append('echo "→ snapshot: pausing compose stacks"')
        for compose_file in pause_files:
            quoted = shlex.quote(compose_file)
            lines.append(
                f"docker compose -f {quoted} pause || "
                'echo "  (compose pause non-fatal: stack may already be down)"',
            )
        lines.append("")

    if pg_targets:
        lines.append('echo "→ snapshot: dumping postgres databases"')
        for pg in pg_targets:
            container = shlex.quote(pg.container)
            db = shlex.quote(pg.database)
            user = shlex.quote(pg.user)
            dump_file = f"$POSTGRES_DIR/{pg.database}.sql.gz"
            lines.append(
                f"docker exec {container} pg_dump -U {user} -d {db} -F c | gzip -9 > {dump_file}",
            )
        lines.append("")

    lines.append('echo "→ snapshot: uploading filesystem trees"')
    for rs in rs_targets:
        local = shlex.quote(rs.local_path)
        sub = shlex.quote(rs.s3_subpath)
        lines.append(
            f'rclone sync --create-empty-src-dirs {local} "$BUCKET/$SNAPSHOT_PREFIX/{sub}"',
        )
    lines.append("")

    if pg_targets:
        lines.append('echo "→ snapshot: uploading postgres dumps"')
        lines.append(
            'rclone sync "$POSTGRES_DIR" "$BUCKET/$SNAPSHOT_PREFIX/postgres"',
        )
        lines.append("")

    lines.extend(
        [
            'echo "→ snapshot: writing manifest"',
            'cat > "$WORKDIR/manifest.json" <<EOF',
            "{",
            '  "version": 1,',
            '  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",',
            '  "stack": "$STACK",',
            '  "timestamp": "$TIMESTAMP",',
            '  "template_version": "$TEMPLATE_VERSION"',
            "}",
            "EOF",
            'rclone copyto "$WORKDIR/manifest.json" "$BUCKET/$SNAPSHOT_PREFIX/manifest.json"',
            "",
            'echo "→ snapshot: verifying upload (rclone check)"',
            # Atomicity gate. Two distinct failure modes need distinct
            # treatment, which the previous ``... | grep ... && {...}
            # || true`` form silently merged into one bucket:
            #
            #   1. rclone-check itself fails (auth/network/quota) →
            #      ``rclone check`` exits non-zero. The previous form
            #      would let ``set -e`` catch that, but only if the
            #      pipe-grep didn't find any drift markers; the ``||
            #      true`` fallback then masked it again. We capture
            #      the rclone-check rc explicitly via ``${PIPESTATUS}``
            #      and abort if non-zero.
            #
            #   2. rclone-check succeeds but reports drift (some file
            #      missing-on-remote ``-`` or different ``*``). In
            #      that case ``grep -E "^[-*]"`` *succeeds* (rc=0 →
            #      drift found) and we abort. If grep returns rc=1
            #      (no drift markers found) that's the happy path.
            #
            # ``--one-way`` keeps the comparison local→S3-only, so a
            # stale orphan in S3 from a previous failed snapshot can't
            # by itself fail the gate — only files we actually
            # uploaded need to round-trip cleanly.
            "set +e",
            'rclone check "$WORKDIR" "$BUCKET/$SNAPSHOT_PREFIX" '
            '--one-way --combined - 2>"$WORKDIR/rclone-check.err" '
            '| tee "$WORKDIR/rclone-check.out" '
            '| grep -qE "^[-*]"',
            "drift_rc=${PIPESTATUS[1]}     # 0 = drift found, 1 = clean",
            "rclone_rc=${PIPESTATUS[0]}    # rclone-check's own exit",
            "set -e",
            'if [ "$rclone_rc" -ne 0 ]; then',
            '  echo "✗ snapshot-failed: rclone check itself errored (rc=$rclone_rc)" >&2',
            '  cat "$WORKDIR/rclone-check.err" >&2 || true',
            "  exit 2",
            "fi",
            'if [ "$drift_rc" -eq 0 ]; then',
            '  echo "✗ snapshot-failed: rclone check found drift" >&2',
            '  cat "$WORKDIR/rclone-check.out" >&2',
            "  exit 2",
            "fi",
            "",
            'echo "→ snapshot: pointing snapshots/latest at $TIMESTAMP"',
            'echo "$TIMESTAMP" > "$WORKDIR/latest.txt"',
            'rclone copyto "$WORKDIR/latest.txt" "$BUCKET/snapshots/latest.txt"',
            "",
            'echo "✓ snapshot complete: $SNAPSHOT_PREFIX"',
        ],
    )

    return "\n".join(lines) + "\n"


def render_restore_script(
    *,
    endpoint: S3Endpoint,
    postgres_targets: Iterable[PostgresDumpTarget],
    rsync_targets: Iterable[RsyncTarget],
    local_root: str = "/var/lib/nexus-data",
) -> str:
    """Render the bash that restores a snapshot from S3 to the local
    filesystem.

    Idempotent on the empty-S3 case (first-time spinup) — the
    rendered script:

    1. Reads ``snapshots/latest.txt`` to get the active timestamp.
       If the file is missing, it short-circuits with
       ``echo 'fresh-start: no snapshot in S3, leaving local
       state empty'`` and exits 0. Pipeline.py then proceeds with
       a clean docker-compose up just like a brand-new install.
    2. ``rclone sync`` the snapshot's filesystem trees into
       ``local_root``.
    3. ``pg_restore`` each postgres dump into the matching
       container's database. The container is assumed to already be
       running (compose-up ran first); we drop+recreate the
       database to get a clean restore.

    Does NOT touch ``snapshots/latest.txt`` — the active snapshot
    pointer is owned by the snapshot side. A restore is purely
    read-only against S3.
    """
    pg_targets = tuple(postgres_targets)
    rs_targets = tuple(rsync_targets)
    bucket_url = f"{RCLONE_PROFILE}:{shlex.quote(endpoint.bucket)}"

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# Generated by nexus_deploy.s3_persistence — do not edit by hand.",
        "set -euo pipefail",
        "",
        f"BUCKET={bucket_url}",
        f"LOCAL_ROOT={shlex.quote(local_root)}",
        "WORKDIR=/tmp/nexus-restore",
        "",
        'mkdir -p "$WORKDIR" "$LOCAL_ROOT"',
        "",
        'echo "→ restore: looking up latest snapshot"',
        # `rclone copyto` returns rc=0 even on missing source if we
        # use `--ignore-checksum --error-on-no-transfer=false`, but
        # the simplest probe is `rclone lsf` which exits non-zero
        # if the file is missing. Wrap it in an explicit check so
        # the absence-case branches cleanly.
        'if ! rclone lsf "$BUCKET/snapshots/latest.txt" >/dev/null 2>&1; then',
        '  echo "fresh-start: no snapshot in S3, leaving local state empty"',
        "  exit 0",
        "fi",
        'rclone copyto "$BUCKET/snapshots/latest.txt" "$WORKDIR/latest.txt"',
        'TIMESTAMP=$(tr -d "\\r\\n" < "$WORKDIR/latest.txt")',
        'if [[ ! "$TIMESTAMP" =~ ^[0-9A-Za-z_-]+$ ]]; then',
        '  echo "✗ restore-failed: latest.txt has invalid timestamp" >&2',
        "  exit 2",
        "fi",
        'SNAPSHOT_PREFIX="snapshots/$TIMESTAMP"',
        'echo "→ restore: using snapshot $SNAPSHOT_PREFIX"',
        "",
    ]

    if rs_targets:
        lines.append('echo "→ restore: pulling filesystem trees"')
        for rs in rs_targets:
            sub = shlex.quote(rs.s3_subpath)
            # Use rs.local_path directly — it's already an absolute
            # path. We deliberately don't recompose under local_root
            # because callers may want to restore to a different
            # absolute path (e.g. /opt/data on a future stack) and
            # the value is already injection-safe via shlex.quote.
            #
            # ``local_root`` is still used as the parent dir for
            # mkdir at the top of the script (so the very first
            # rsync target lands in a created directory). It does
            # NOT govern restore destinations.
            local = shlex.quote(rs.local_path)
            lines.append(
                f'rclone sync "$BUCKET/$SNAPSHOT_PREFIX/{sub}" {local} --create-empty-src-dirs',
            )
        lines.append("")

    if pg_targets:
        lines.append('echo "→ restore: pulling postgres dumps"')
        lines.append(
            'rclone sync "$BUCKET/$SNAPSHOT_PREFIX/postgres" "$WORKDIR/postgres"',
        )
        lines.append('echo "→ restore: applying postgres dumps"')
        for pg in pg_targets:
            container = shlex.quote(pg.container)
            db = shlex.quote(pg.database)
            user = shlex.quote(pg.user)
            dump_file = f"$WORKDIR/postgres/{pg.database}.sql.gz"
            # We drop+recreate the database to guarantee a clean
            # restore. pg_restore --clean would do something similar
            # but is fragile across PG versions; the explicit
            # drop+create is portable.
            lines.append(
                f"docker exec {container} psql -U {user} -d postgres "
                f'-c "DROP DATABASE IF EXISTS {pg.database} WITH (FORCE);"',
            )
            lines.append(
                f"docker exec {container} psql -U {user} -d postgres "
                f'-c "CREATE DATABASE {pg.database} OWNER {pg.user};"',
            )
            lines.append(
                f"gunzip -c {dump_file} | "
                f"docker exec -i {container} pg_restore -U {user} -d {db} "
                "--no-owner --no-acl",
            )
        lines.append("")

    lines.append('echo "✓ restore complete from $SNAPSHOT_PREFIX"')

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Manifest helpers (used by tests + future cleanup script)
# ---------------------------------------------------------------------------


def manifest_for_components(
    *,
    stack: str,
    template_version: str,
    timestamp: str,
    components: Mapping[str, tuple[int, str]],
) -> SnapshotManifest:
    """Build a :class:`SnapshotManifest` from a sized+hashed component map.

    Helper for callers that compute checksums client-side (e.g. unit
    tests, a future cleanup-and-verify script). The on-server bash
    in :func:`render_snapshot_script` builds a slimmer manifest
    without per-component checksums in v1.0 — the rclone check on
    upload covers the integrity property cheaply, and computing
    sha256 over multi-GB rsync trees on the server adds material
    minutes to teardown.

    The version-1.1 plan is to revisit this and either (a) make the
    rendered bash compute and emit per-component sha256 (slower
    teardown, more robust restore) or (b) trust rclone's own
    integrity check entirely and remove this helper.
    """
    return SnapshotManifest(
        version=1,
        created_at="",
        stack=stack,
        template_version=template_version,
        components=tuple(
            ComponentSnapshot(name=name, path=name, size_bytes=size, sha256=sha)
            for name, (size, sha) in sorted(components.items())
        ),
    )
