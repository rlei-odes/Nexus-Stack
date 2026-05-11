---
title: "Gitea"
---

## Gitea

![Gitea](https://img.shields.io/badge/Gitea-609926?logo=gitea&logoColor=white)

**Self-hosted Git service with pull requests, code review, and CI/CD**

A lightweight, self-hosted Git hosting solution that provides:
- Pull requests and code review
- Issue tracking and project management
- CI/CD via Gitea Actions
- Repository mirroring from GitHub
- HTTPS access via Cloudflare Tunnel

| Setting | Value |
|---------|-------|
| Default Port | `3200` (-> internal 3000) |
| Suggested Subdomain | `gitea` |
| Public Access | No |
| Website | [gitea.com](https://about.gitea.com) |
| Source | [GitHub](https://github.com/go-gitea/gitea) |

> ✅ **Auto-configured:** Admin account is automatically created during deployment. Credentials are stored in Infisical under the `gitea` tag.

### Architecture

The stack includes:
- **Gitea** - Git service (Web UI + API)
- **Git Proxy** - Nginx reverse proxy for public Git HTTPS access (separate stack)
- **PostgreSQL** - Database for users, issues, PRs, and metadata

### Shared Workspace Repo

During deployment, a shared workspace repo named `nexus-<domain>-gitea` is automatically created. This repo is auto-cloned into the following services:

| Service | Clone Location | Method |
|---------|---------------|--------|
| Jupyter | `/home/jovyan/work/<repo>` | Entrypoint + jupyterlab-git |
| Marimo | `/app/notebooks/<repo>` | Entrypoint clone |
| code-server | `/home/coder/<repo>` | Entrypoint clone (opens as workspace) |
| Meltano | `/project/<repo>` | Entrypoint clone |
| Prefect | `/flows/<repo>` (worker) | Entrypoint clone |
| Kestra | Git sync flow | `plugin-git` SyncNamespaceFiles (every 15 min) |

### GitHub Repository Mirroring (Optional)

You can automatically mirror one or more private GitHub repositories into Gitea.
This is useful for distributing course material or read-only code to students.

**Setup:** Add the following two secrets to your GitHub repository
(Settings → Secrets and variables → Actions → Secrets):

| Secret | Description |
|--------|-------------|
| `GH_MIRROR_TOKEN` | GitHub Fine-grained Personal Access Token with `Contents: Read-only` permission |
| `GH_MIRROR_REPOS` | Comma-separated list of GitHub HTTPS repo URLs to mirror |

**Example value for `GH_MIRROR_REPOS`:**
```
https://github.com/my-org/course-2025.git,https://github.com/my-org/examples.git
```

> ⚠️ If either secret is not set, the mirroring step is skipped entirely.

**How it works:**
- During each spin-up, deploy.sh creates a pull mirror in Gitea for each configured URL
- The mirrored repo is named `mirror-<repo>` (e.g. GitHub `course-2025` → Gitea `mirror-course-2025`)
- Gitea syncs from GitHub **every 10 minutes** (delta fetch — only new commits are transferred)
- Mirrored repos are **private** in Gitea (accessible only via Cloudflare Access)
- The student user (derived from `TF_VAR_user_email`) is automatically added as a **read-only** collaborator
- The operation is **idempotent**: re-running spin-up skips mirrors that already exist

**GitHub rate limits:** 10-minute intervals = 6 git fetches/hour per repo — well within the 5,000/hour PAT limit.

**Triggering an immediate sync:** Log into Gitea as admin → open the mirrored repo → Settings → Mirror sync. This is a built-in Gitea feature, no additional setup required.

#### Creating a Fine-grained PAT

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**
2. Click "Generate new token"
3. Set **Resource owner** to the org or user that owns the repo to mirror
4. Under **Repository access** → "Only select repositories" → select the repo(s) to mirror
5. Under **Permissions** → Repository permissions → set **Contents: Read-only**
   (all other permissions can remain "No access")
6. If the org enforces SAML SSO: after creating the token, go to
   Settings → Personal access tokens → "Configure SSO" → authorize the org

> The token must belong to a GitHub account that has read access to the target repo.
> It does not need to be in the same organization as your Nexus-Stack repository.
>
> `Contents: Read-only` is the only permission required — Gitea uses it solely for
> HTTPS git fetch operations, which only need read access to repository contents.

### Persistent Storage

Gitea stores repository data and its database on the server's **local SSD**, snapshotted to **Cloudflare R2** on teardown and restored on spin-up (RFC 0001):
- Git repositories: `/mnt/nexus-data/gitea/repos` (uid 1000:1000)
- LFS objects: `/mnt/nexus-data/gitea/lfs` (uid 1000:1000)
- PostgreSQL data: `/mnt/nexus-data/gitea/db` (uid 70:70)

On **teardown**: `python -m nexus_deploy s3-snapshot` runs BEFORE `tofu destroy`. It stops gitea + dify briefly, pg_dumps both databases, rclone-syncs the file trees to `s3://<persistence-bucket>/snapshots/<timestamp>/`, verifies every source, and only then points `snapshots/latest.txt` at the new snapshot. Any failure aborts the teardown and the server stays up. The atomicity guarantee is on `snapshots/latest.txt`: it only flips after every source verifies. A failure mid-upload may leave a partial `snapshots/<timestamp>/` tree in R2, but since `latest.txt` doesn't point at it, the next spin-up's `restore_from_s3` never sees it. The cleanup cron (RFC 0001 v1.1) sweeps orphan trees by sort-order; in the meantime they cost only R2 storage.

On **spin-up**, the pipeline splits restore into two halves around compose-up: (1) `restore_from_s3(phase="filesystem")` BEFORE compose-up pulls `snapshots/latest.txt`, downloads the referenced filesystem trees into `/mnt/nexus-data/`; (2) `ensure_data_dirs` then chowns the rsync'd trees to the container-expected UIDs (1000:1000 for gitea, 70:70 for postgres, 999:999 for redis) BEFORE compose-up so containers start with the right ownership; (3) `restore_from_s3(phase="postgres")` AFTER compose-up applies the pg_dumps via `docker exec` against the now-running gitea-db / dify-db. A first-ever spin-up against an empty bucket fresh-starts in both halves (no data restored; compose comes up with empty data dirs).

On **destroy-all**: The Hetzner server is destroyed; the R2 bucket holding snapshots is preserved (it lives outside Tofu state) so a later `initial-setup` + `spin-up` reattaches to the existing snapshot history. To wipe persistence too, run `scripts/cleanup-s3-bucket.sh` with `CONFIRM_DELETE_DATA=DESTROY` in the environment — that's the audited deletion path per RFC 0001 decision #6 (iterates the bucket via S3 API and removes every object before the bucket-delete). The Cloudflare dashboard still works as an alternative but leaves no audit trail.
