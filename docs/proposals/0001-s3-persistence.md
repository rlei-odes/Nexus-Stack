# RFC 0001 — S3-Backed Persistence (Hetzner Object Storage)

**Status:** Draft
**Author:** sk@stefanko.ch
**Date:** 2026-05-10
**Target version:** v1.0.0 (breaking change)

## tl;dr

Replace the per-stack Hetzner Block Storage volume with **Hetzner Object Storage** as the canonical persistence layer. On `spinup`, restore from S3 to local SSD; on `teardown`, snapshot back to S3, then destroy infra. This eliminates the volume-location lock-in that today wedges every stack to a single Hetzner DC, surfaces dramatically during EU stock crunches (root cause of the 2026-05-10 Hetzner OOS incident).

## Motivation

### The current wedge

Every Nexus-Stack instance has one persistent Hetzner Block Storage volume mounted at `/mnt/nexus-data/`. The volume is created once at control-plane setup and pinned to a specific Hetzner location (typically `fsn1`). On every spinup the server is provisioned and the existing volume is attached.

Hetzner enforces: **server and volume must be in the same location**. Volumes are **not migratable** between locations.

Today (2026-05-10) Hetzner Falkenstein went out of stock for every server type we tried (cx43, cpx41, cx42, cx52). Capacity exists at hel1, nbg1, ash, hil, sin — but no fsn1 volume can be attached to any of those. Result: 26 student stacks completely wedged for hours, no graceful fallback.

### The architectural fix

Move all persistent data to Hetzner Object Storage. Server local SSD becomes ephemeral cache. Spinup-anywhere becomes possible because the server has no location-locked dependency.

### What this is NOT

- Not a backup solution (though it gets you one for free). Recovery from S3 is the *primary* path, not a fallback.
- Not multi-region (Hetzner Object Storage is EU-only: fsn1, hel1, nbg1). For multi-region resilience use Cloudflare R2; for now we stay on Hetzner for data-residency / GDPR alignment with the rest of the stack.
- Not Postgres-on-S3. Postgres needs POSIX semantics and stays on local SSD, but its **dump** lives on S3.

## Goals

1. **Eliminate Hetzner volume location lock-in.** A spinup must succeed in any of fsn1, hel1, nbg1, ash, hil, sin given Hetzner has stock there.
2. **Preserve all student-visible state across teardown→spinup cycles** (Gitea repos, Postgres data, Dify uploads, Weaviate vectors).
3. **Atomic teardown.** A teardown that fails to upload to S3 must abort, not destroy infra. No "half-saved" state.
4. **Acceptable spinup overhead.** Adding S3-restore should not extend spinup beyond +5 minutes vs. today.
5. **Clean migration for existing 26 stacks.** No data loss; one-time evacuation script that runs against current volumes before they're decommissioned.

## Non-goals

- Real-time replication (streaming changes to S3 on every write). Snapshot-based suffices for the "scheduled teardown / class-end" pattern.
- Per-second RPO. RPO is "since last teardown" (typically nightly). Acceptable for a class environment.
- Encryption at rest beyond what Hetzner Object Storage provides natively (server-side encryption is on by default; we don't add envelope encryption in v1).
- Migrating away from Postgres entirely. We keep the existing Postgres containers, just move their dump (not their live data) to S3.

## Current architecture

### Volume layout (today)

```
/mnt/nexus-data/                      ← Hetzner Volume (fsn1, immutable location)
├── gitea/
│   ├── repos/                         Git repos: nexus_seeds + student-pushed code
│   ├── lfs/                           Git LFS files
│   └── db/                            Gitea Postgres data dir
└── dify/
    ├── storage/                       Dify uploaded files (knowledge bases)
    ├── db/                            Dify Postgres data dir
    ├── weaviate/                      Vector DB files
    ├── plugins/                       Installed Dify plugins
    └── redis/                         Redis dump (ephemeral, regeneratable)
```

### Current spinup flow

```
1. select-capacity:  pick (server_type, location) honoring SERVER_PREFERENCES
2. tofu apply:       provision server + attach existing volume by ID
                     ← FAILS HERE if server.location ≠ volume.location
3. cloud-init:       mount volume at /mnt/nexus-data
4. compose-runner:   docker compose up (services bind-mount /mnt/nexus-data/...)
```

### Current teardown flow

```
1. tofu destroy:     server + tunnel + DNS + access apps
                     volume is RETAINED (lifecycle prevent_destroy)
```

The volume sits idle costing ~€0.50/month per stack until next spinup.

## Proposed architecture

### Storage layout

```
Hetzner Object Storage (fsn1):
  s3://nexus-<class>-<user>-internal/
    ├── manifest.json                     ← snapshot metadata: timestamp, version, hashes
    ├── postgres/
    │   ├── gitea.sql.gz                  ← pg_dump
    │   └── dify.sql.gz
    ├── gitea/
    │   ├── repos/                        ← rsync mirror (excluding ephemeral .lock files)
    │   └── lfs/                          ← rsync mirror (or native Gitea S3-LFS, see Open Questions)
    ├── dify/
    │   ├── storage/                      ← rsync mirror (or native Dify S3 backend)
    │   ├── weaviate/                     ← rsync mirror of weaviate persistent dir
    │   └── plugins/                      ← rsync mirror
    └── _versioning/                      ← retain last N snapshots (Hetzner lifecycle policy)

Server local SSD (ephemeral, recreated on every spinup):
  /var/lib/nexus-data/
    ├── gitea/                            ← restored from S3 on spinup
    ├── dify/
    └── postgres-bootstrap/               ← scratch dir for pg_restore
```

`/mnt/nexus-data/` symlink points to `/var/lib/nexus-data/` for backward-compat with existing docker-compose paths.

### New spinup flow

```
1. select-capacity:    pick (type, location) — no volume constraint, full SERVER_PREFERENCES list valid
2. tofu apply:         provision server (no volume attachment) +
                       per-stack minio_s3_bucket (see "Bucket
                       provisioning" below)
3. cloud-init / setup:
   a. mkdir -p /var/lib/nexus-data
   b. ln -sfn /var/lib/nexus-data /mnt/nexus-data    (back-compat symlink:
                                                      existing docker-compose
                                                      bind-mounts under
                                                      /mnt/nexus-data resolve to
                                                      the new SSD-local location
                                                      without docker-compose
                                                      changes)
   c. rclone sync s3://<bucket>/ → /var/lib/nexus-data/   (skip if first-time spinup)
   d. start docker compose for postgres containers (gitea-db, dify-db) on EMPTY data dirs
   e. pg_restore < /var/lib/nexus-data/postgres/gitea.dump  (custom binary format)
   f. pg_restore < /var/lib/nexus-data/postgres/dify.dump
4. compose-runner:     docker compose up for the rest of the stack
```

**Postgres dump format**: ``pg_dump -F c | gzip`` (custom binary
format, gzipped) on snapshot, ``gunzip | pg_restore`` on spinup.
Plain SQL output (``pg_dump -F p``) would only round-trip through
``psql``, which doesn't support the ``--clean --no-owner --no-acl``
options we use for cross-version restores. The implementation in
``s3_persistence.py:render_snapshot_script`` and
``render_restore_script`` uses the custom format end-to-end.

### New teardown flow (atomic)

```
1. pre-snapshot:       docker compose stop on app services (Gitea web,
                       Dify api/web) + Postgres exec CHECKPOINT to flush
                       WAL. We deliberately do NOT use `docker compose
                       pause` (cgroup-freezer SIGSTOP is hard-stop, not
                       drain — in-flight HTTP requests die mid-write).
                       The compose stop with default 10s timeout gives
                       app processes time to finish in-flight requests
                       and close DB connections cleanly.
2. dump postgres:
   a. docker exec gitea-db pg_dump -F c -U <user> <db> | gzip > /tmp/dumps/gitea.dump.gz
   b. docker exec dify-db  pg_dump -F c -U <user> <db> | gzip > /tmp/dumps/dify.dump.gz
3. rsync-to-s3:
   a. rclone sync /var/lib/nexus-data/gitea → s3://<bucket>/gitea/
   b. rclone sync /var/lib/nexus-data/dify → s3://<bucket>/dify/
   c. rclone copy /tmp/dumps/ → s3://<bucket>/postgres/
   d. write manifest.json (timestamps, file count, hashes)
4. verify:             rclone check (re-list, compare ETag/size)
   ✗ on mismatch:      ABORT — leave server up, alert operator, do NOT proceed to step 5
   ✓ on match:         proceed
5. tofu destroy:       server + tunnel + DNS + access apps
                       volume resource removed entirely from the tofu state
```

### Atomicity guarantees

- **Step 4 is the gate.** If verify fails for any reason (network blip, bucket permission, partial upload), the workflow stops. Server stays up; operator decides whether to retry teardown or troubleshoot.
- **No infrastructure destruction before verified S3 state.**
- **Idempotency:** re-running teardown after a partial failure replays steps 1–4. Step 4 short-circuits if hashes already match.

## Code changes

### Template (`stefanko-ch/Nexus-Stack`)

#### Tofu

| File | Change |
|---|---|
| `tofu/control-plane/main.tf` | Remove `hcloud_volume "persistent"` resource + outputs. Replace with Hetzner Object Storage bucket creation (`hcloud_storage_box` + S3 credentials, OR document manual bucket creation if Terraform provider lacks support — see Open Questions). |
| `tofu/control-plane/variables.tf` | Remove `persistent_volume_size`. Add `s3_persistence_bucket_name`, `s3_persistence_endpoint`, `s3_persistence_region`. |
| `tofu/control-plane/outputs.tf` | Replace `persistent_volume_id` with `s3_persistence_credentials` (sensitive). |
| `tofu/stack/main.tf` | Remove `hcloud_volume_attachment "persistent"`. Add user-data / cloud-init pulling from S3 (or do it in `pipeline.py` instead — see below). |
| `tofu/stack/variables.tf` | Remove `persistent_volume_id`. |

#### `src/nexus_deploy/`

| File | Change |
|---|---|
| `setup.py` | Remove `mount_persistent_volume()`. Add `restore_from_s3()` that runs after server boot: rclone sync + pg_restore. |
| `pipeline.py` | Replace `_setup.mount_persistent_volume(...)` with `_setup.restore_from_s3(...)`. Add new phase `_phase_postgres_restore` that runs pg_restore before `_phase_compose_up`. |
| `compose_runner.py` | Replace bind-mount paths from `/mnt/nexus-data/...` to `/var/lib/nexus-data/...`, OR keep `/mnt/nexus-data` as symlink (decide based on whether `/mnt` is conventional in any tooling). |
| **NEW** `s3_persistence.py` | Module containing: `dump_postgres_to_s3()`, `rclone_sync_to_s3()`, `rclone_sync_from_s3()`, `verify_s3_snapshot()`, `write_manifest()`, `read_manifest()`. |
| **NEW** `teardown.py` (or extend existing teardown logic in `__main__.py`) | New phase ordering: pause → dump → sync → verify → tofu destroy. Abort on verify failure. |

#### `services.yaml` / `stacks/`

| Stack | Change |
|---|---|
| `gitea` | Bind-mount path update only (or keep — if `/mnt/nexus-data` symlink stays). Optionally migrate Gitea LFS to native S3 backend (`[lfs] STORAGE_TYPE=minio`) — saves an rsync round-trip but adds Gitea config complexity. **Open Question.** |
| `dify` | Bind-mount path update only (or keep). Optionally migrate Dify storage to native S3 (`STORAGE_TYPE=s3` env var) — see Dify docs. **Open Question.** Weaviate stays local — no S3 backend mode. |
| All other stacks | No changes. Their docker-named-volumes were already ephemeral; the volume removal doesn't affect them. |

#### GitHub Actions workflows

| File | Change |
|---|---|
| `.github/workflows/spin-up.yml` | Remove `persistent_volume_id` extraction from control-plane outputs. The new pipeline phase pulls S3 credentials from Infisical instead. |
| `.github/workflows/teardown.yml` | Add pre-tofu-destroy phase: run `nexus_deploy.teardown` (the new module). Abort workflow if S3-verify fails. |
| `.github/workflows/destroy-all.yml` | Document that this now also deletes the S3 bucket contents (or doesn't — operator confirmation). |

#### Documentation

| File | Change |
|---|---|
| `CLAUDE.md` | Update "Adding New Stacks" — clarify which stacks need S3-aware persistence. |
| `docs/admin-guides/setup-guide.md` | Replace volume-creation step with S3 bucket creation. |
| `README.md` | Architecture diagram — server is now stateless, data lives in S3. |

### Education (`stefanko-ch/Nexus-Stack-for-Education`)

| File | Change |
|---|---|
| `nexus-admin/packages/shared/src/db/schema.ts` | Add `s3PersistenceBucket` to classes / users (or compute from naming convention). |
| `nexus-admin/packages/shared/src/operations/setup.ts` | Replace volume-creation API call with bucket-creation API call against Hetzner Storage Box / Object Storage. |
| `nexus-admin/packages/shared/src/operations/lifecycle.ts` | Remove volume-related preflight. |
| Admin UI | Class-create form: drop "Persistent Volume Size" field, replace with "S3 Bucket Region" picker (fsn1/hel1/nbg1). |

## Migration path for the 26 existing stacks

This is the riskiest part. Existing stacks have data on Hetzner volumes; we must move it to S3 without loss.

### Phase A: prepare (no breaking changes yet)

1. Pre-create per-stack S3 buckets (one-time admin action).
2. Push Hetzner Object Storage credentials to each stack's Infisical secrets folder.
3. Add `nexus_deploy.s3_persistence` module to template, but don't wire it into pipeline yet.

### Phase B: one-time evacuation

For each of the 26 existing stacks, run a manual evacuation workflow:

```
1. Spin up the stack (current behaviour, attaches volume)
2. Run `nexus-evacuate-volume-to-s3.yaml` — a one-shot GH workflow:
   a. docker compose pause
   b. dump postgres (gitea, dify)
   c. rsync /mnt/nexus-data → s3://<bucket>/
   d. write manifest.json
   e. verify
3. Operator confirms S3 contents look right
4. Stack stays up on volume (still using old code path)
```

Run for all 26 stacks during a maintenance window.

### Phase C: cutover (atomic per stack)

Per stack:

1. Teardown (current code path — preserves volume)
2. Update fork to new template version (S3-aware)
3. Spinup (new code path — restores from S3, ignores volume)
4. Verify functionality
5. Detach + delete the now-unused Hetzner volume

If anything goes wrong in step 3, roll back: spin up with the old template version, the volume is still there.

### Phase D: decommission

Once all 26 stacks are on the new code path and a few weeks have passed without regressions:

1. Delete the orphaned volumes (one-time cleanup script).
2. Remove the legacy `mount_persistent_volume` code path entirely from the template (clean removal in v1.0.0).

## Risks and open questions

### Risks

| Risk | Mitigation |
|---|---|
| **S3 upload fails mid-teardown** | Atomic 2-phase: verify before destroy. Operator manually resolves; never silently lose data. |
| **Spinup time becomes too long** | Profile per-stack data size; for large stacks (>5 GB) consider parallel rsync streams or streaming pg_restore. Document expected spinup time impact in setup guide. |
| **Postgres dump consistency** | `pg_dump` on running DB takes a consistent snapshot at start of operation. For Gitea: pause Gitea before dump (already in teardown plan). For Dify: same. |
| **Weaviate corruption on incomplete restore** | Treat Weaviate as rebuildable from Dify-DB metadata if the dump is partial. Worst case: knowledge bases need re-indexing (slow but recoverable). |
| **Hetzner Object Storage outage in fsn1** | Same blast radius as today's volume situation, but bucket can be (manually) replicated to a different bucket in hel1/nbg1 for DR. Out of scope for v1. |
| **R2 might be a better choice after all** | R2 is global, zero egress, free tier sufficient for tutorial-scale data. We ship v1 on Hetzner Object Storage per operator preference, but `s3_persistence.py` is endpoint-agnostic — switching to R2 is a config change, not a code change. |
| **Existing Class config breaks on upgrade** | Migration script; back-compat shim in tofu (`persistent_volume_id = 0` becomes the new normal). |

### Open questions

1. **Gitea LFS native backend vs. rsync.** Gitea natively supports S3 LFS storage. Switching to native saves an rsync of the LFS dir on every teardown but adds Gitea config to the deployment. Recommendation: **rsync in v1, migrate to native LFS-S3 in v1.1** once we know the rsync performance impact.

2. **Dify storage native S3 backend.** Dify supports `STORAGE_TYPE=s3`. Same trade-off as Gitea. Recommendation: **rsync in v1, migrate to native in v1.1**.

3. **Weaviate.** No native S3 mode. Always rsync. Document that weaviate restoration is best-effort and Dify can rebuild if needed.

4. **Bucket-per-stack vs. shared bucket with prefixes.** Per-stack is operationally cleaner (easy to delete on destroy-all, isolation, separate IAM scopes). Shared with `<stack>/<path>` prefixes saves the bucket-creation step but couples blast radius. Recommendation: **bucket-per-stack**.

5. **Hetzner Object Storage Terraform provider support.** RESOLVED — the existing `aminueza/minio` provider (already in `tofu/control-plane/main.tf:287-316` for LakeFS / general / pgducklake buckets) handles Hetzner Object Storage cleanly via `minio_s3_bucket`. v1.0 adds a fourth `minio_s3_bucket "persistence"` resource per stack, same pattern. The shell scripts shipped in PR-1 of the implementation become migration tooling for the existing-stack evacuation phase, not the steady-state path.

6. **Snapshot retention.** v1.0 ships with **30-day NoncurrentVersionExpiration** as the safety net (rough ceiling on storage cost, no precise N-of-each control). At typical tutorial-stack sizes (~5 GB current copy, plus same again in noncurrent versions for the most recent ~30 days of teardown→spinup churn) this lands around ~5-10 GB/stack peak — see Cost analysis table for the per-stack monthly impact. The "last 7 daily + last 4 weekly" pattern is a v1.1 follow-up that needs either tag-based lifecycle rules (not in the current minio provider) or a separate cleanup cron.

7. **What happens to `destroy-all`?** Today it removes the volume too. New behaviour: also deletes the S3 bucket? Or preserves it for cold-storage / forensics? Recommendation: **destroy-all deletes the bucket as well** (matches user intent of "clean slate") with a `--keep-data` flag for the cautious case.

8. **How to surface S3 latency in spinup logs.** Add a `_phase_s3_restore` log section showing per-directory transfer rate so the operator can spot regressions.

## Phased rollout plan

### v1.0-rc.1 — code complete on a feature branch

- All template changes (tofu, src/nexus_deploy/, stacks, workflows)
- Migration script `evacuate-volume-to-s3.yaml` workflow
- Documentation updates
- Unit tests for `s3_persistence.py`
- Integration test: spin up a fresh stack on the feature branch, teardown, spinup again — verify no data loss

### v1.0-rc.2 — evacuation of pilot stack

- Pick 1 of the 26 (e.g. `stefan-hslu` or a Template Dev Stack)
- Run evacuation
- Cutover this stack to new code path
- Run for 24-48 h, verify nothing breaks

### v1.0-rc.3 — broader pilot

- Cutover 5 more stacks, monitor

### v1.0.0 — full cutover

- All 26 stacks migrated
- Old code path removed from template
- Release Please cuts v1.0.0

## Cost analysis (back-of-envelope)

Hetzner Object Storage pricing (2026-05): **€0.99 per TB/month** for storage in
the bucket, free egress within the Hetzner network, **€1.19 per TB** egress to
the public internet. We multiply through with 26 stacks at ~5 GB current data.

Storage volume estimation under v1.0 retention (30-day NoncurrentVersion-
Expiration): each snapshot is roughly the size of the live data (~5 GB).
Stacks teardown+spinup ~10× per month under typical class usage; with 30-day
retention, peak storage per stack is ``~5 GB current × (1 + 10) = ~55 GB``
in the absolute worst case. That's the ceiling — typical usage churns less.

| Item | Today | Post-v1.0 (worst case) | Post-v1.0 (typical) |
|---|---|---|---|
| Hetzner volume (10 GB × 26 stacks) | €0.50 × 26 = €13/month | €0 | €0 |
| Hetzner Object Storage storage (~55 GB worst / ~10 GB typical × 26 stacks × €0.99/TB) | €0 | ~€1.40/month | ~€0.25/month |
| Hetzner Object Storage egress (1× per spinup, ~5 GB × 30 spinups/month, mostly within-Hetzner so free) | €0 | ~€0.20/month (only the cross-DC chunks) | ~€0.20/month |
| Increased spinup time (3-5 min × 30 spinups) | n/a | negligible | negligible |
| **Net** | **~€13/month** | **~€1.60/month** | **~€0.45/month** |

Both columns are dramatically cheaper than the pre-v1.0 baseline. The
"~€10/month" figure that previously appeared in Open Question #6 was an
overestimate from before the actual Hetzner pricing was looked up — v1.0
storage cost across the whole class is comfortably under €2/month. The
operational benefit (spinup-anywhere; no more EU stock crunch) dwarfs the
storage saving anyway.

## Decision points — RESOLVED 2026-05-10

1. **Storage provider:** ✅ **Hetzner Object Storage** (EU data-residency, S3-compatible, already used elsewhere in the stack). R2 deferred — switching is a config change later.
2. **Bucket scoping:** ✅ **Bucket-per-stack** — operational isolation, easy `destroy-all` cleanup, per-stack IAM scope.
3. **Bucket provisioning:** ✅ **Tofu via the existing `aminueza/minio` provider** (`minio_s3_bucket` resource). The repo already provisions three Hetzner Object Storage buckets this way (`tofu/control-plane/main.tf:287-316` — LakeFS, general, pgducklake), so adding a fourth `minio_s3_bucket "persistence"` resource per stack stays in the established pattern. This was originally proposed as a shell script because the `hcloud` provider doesn't cover Object Storage — that's still true, but the `minio` provider does, and we already use it. The shell scripts (`scripts/init-s3-bucket.sh`, `scripts/cleanup-s3-bucket.sh`) shipped in PR-1 of the implementation series remain as **migration tools** for the existing 26 stacks (whose buckets need to be created against the not-yet-Tofu-managed state during the evacuation phase) and as a manual-fallback path for operators not using Education's setup automation. They are not the primary provisioning mechanism in the steady state.
4. **Native S3 backends (Gitea LFS, Dify storage):** ✅ **Defer to v1.1** — v1.0 ships with rsync only. Removes one source of risk per release.
5. **Snapshot retention:** ✅ **30-day NoncurrentVersionExpiration as the safety net for v1.0** — the precise "7 daily + 4 weekly" pattern requires either tag-based lifecycle rules (not supported by the minio provider's lifecycle resource as of writing) or a separate cleanup cron. v1.0 ships with the 30-day cap; a v1.1 follow-up adds a precise-N-of-each cleanup script. ~50 GB/stack worst case under 30-day retention, ~€1.30/month total per the cost analysis below.
6. **`destroy-all` behaviour:** ✅ **Opt-in delete** — bucket preserved by default, `--delete-data` flag (or workflow input) required to remove it. Same shape as the existing `confirm=DESTROY` confirmation.

## Estimated effort

- Architecture + RFC: 0.5 days (this document)
- Implementation (template-side): 3-5 days
- Implementation (Education-side): 1-2 days
- Migration scripts + evacuation workflow: 1-2 days
- Pilot rollout + monitoring: 2-3 days
- Full cutover: 1 day (mostly waiting for queue)
- Documentation: 1 day

**Total: ~10-15 working days end-to-end**, can be parallelised between template + Education.

## Appendix A — manifest.json schema

```json
{
  "version": 1,
  "created_at": "2026-05-10T20:30:00Z",
  "stack": "nexus-stefan-hslu",
  "template_version": "v0.56.0",
  "components": {
    "gitea": {
      "repos_count": 12,
      "repos_size_bytes": 245760,
      "lfs_size_bytes": 0,
      "postgres_dump_bytes": 18432,
      "postgres_schema_version": "1.21.0"
    },
    "dify": {
      "storage_size_bytes": 1048576,
      "weaviate_size_bytes": 4194304,
      "plugins_count": 3,
      "postgres_dump_bytes": 32768
    }
  },
  "checksums": {
    "gitea/repos/": "sha256:...",
    "gitea/postgres.sql.gz": "sha256:...",
    "dify/storage/": "sha256:...",
    "dify/postgres.sql.gz": "sha256:...",
    "dify/weaviate/": "sha256:..."
  }
}
```

## Appendix B — example rclone config snippet

```ini
[hetzner-s3]
type = s3
provider = Other
endpoint = https://fsn1.your-objectstorage.com
access_key_id = <from-infisical>
secret_access_key = <from-infisical>
region = fsn1
acl = private
```
