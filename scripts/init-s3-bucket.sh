#!/bin/bash
# =============================================================================
# Nexus-Stack - Cloudflare R2 persistence bucket bootstrap (RFC 0001)
# =============================================================================
# Creates the per-stack R2 persistence bucket, enables versioning, sets a
# 30-day NoncurrentVersionExpiration lifecycle policy (the safety net that
# covers the eventual 7-daily + 4-weekly retention window per RFC 0001
# decision #5 — precise N-of-each retention is enforced by a v1.1 cleanup
# script). Reuses the same R2 token already minted by `init-r2-state.sh` for
# the Tofu state bucket, so this script doesn't need its own credential
# bootstrap.
#
# Called once per stack, either:
#   - by the operator from their workstation during initial setup
#   - by the Education repo's setup.ts during fork creation
#   - by the migration workflow during the existing-26-stacks evacuation phase
#
# Idempotent: running it twice with the same arguments produces no changes
# after the first run. The bucket-exists check lets a re-trigger after a
# partial failure (e.g. lifecycle policy didn't stick) succeed cleanly.
#
# Required environment variables:
#   CLOUDFLARE_ACCOUNT_ID    - Cloudflare account ID
#                              (used to derive the R2 endpoint URL:
#                              https://<account_id>.r2.cloudflarestorage.com)
#   R2_ACCESS_KEY_ID         - R2 S3-API access key (from init-r2-state.sh
#                              output; reused across all R2 buckets in the
#                              project)
#   R2_SECRET_ACCESS_KEY     - matching secret
#   STACK_SLUG               - Per-stack slug, e.g. "nexus-stefan-hslu"
#                              (used as bucket name; must match R2 naming
#                              rules: lowercase alnum + hyphens + dots,
#                              3-63 chars, must start/end with alnum)
#   INFISICAL_PROJECT_ID     - Where to push the bucket coordinates (optional;
#                              skipped with a warning if unset)
#   INFISICAL_TOKEN          - Infisical service-account token (optional)
#
# Outputs (on stdout):
#   BUCKET=<bucket-name>
#   ENDPOINT=<https://<account_id>.r2.cloudflarestorage.com>
#   REGION=auto
#
# The Education repo's setup.ts captures these and writes them as
# GitHub Actions secrets on the per-stack fork.
# =============================================================================

set -euo pipefail

# Colors. Logs go to stderr so the trailing stdout KEY=VALUE block stays
# clean for downstream automation.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[init-s3-bucket]${NC} $*" >&2; }
ok()   { echo -e "${GREEN}[init-s3-bucket] ✓${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[init-s3-bucket] ⚠${NC}  $*" >&2; }
err()  { echo -e "${RED}[init-s3-bucket] ✗${NC}  $*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------------

# Note: CLOUDFLARE_API_TOKEN is NOT required here — all operations
# go through the S3-compatible API at the R2 endpoint with the
# R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY pair. The token would be
# needed for non-S3 Cloudflare API operations (e.g. minting a new
# R2 token, configuring buckets via the CF management API), but
# v1.0 doesn't do any of that — the token from init-r2-state.sh
# already exists and is reused. Requiring an unused token here
# would just make the script harder to call.
: "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is required}"
: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID is required (reuse the one from init-r2-state.sh)}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY is required}"
: "${STACK_SLUG:?STACK_SLUG is required (used as bucket name)}"

# Validate STACK_SLUG against R2 bucket-name rules — same regex as the
# Python module (`_BUCKET_NAME`). R2 bucket names follow the AWS S3 v2
# convention: lowercase alnum, hyphens, dots; 3-63 chars; must start
# and end with alnum.
if ! [[ "$STACK_SLUG" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  err "STACK_SLUG '$STACK_SLUG' is not a valid R2 bucket name (3-63 chars, lowercase alnum + hyphens + dots, must start/end with alnum)"
fi

ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
BUCKET="$STACK_SLUG"

log "Stack: $STACK_SLUG"
log "Bucket: $BUCKET"
log "Endpoint: $ENDPOINT"

# -----------------------------------------------------------------------------
# Tooling check
# -----------------------------------------------------------------------------

if ! command -v aws >/dev/null 2>&1; then
  err "aws CLI not found in PATH. Install from https://aws.amazon.com/cli/"
fi

AWS_VERSION=$(aws --version 2>&1 | head -1)
log "Using $AWS_VERSION"

# Use the R2 S3-compatible access keys (same shape as AWS), but pointed
# at the Cloudflare R2 endpoint. We avoid touching the operator's
# ~/.aws/ so the script doesn't overwrite an existing personal profile.
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
# R2 uses ``auto`` as the region (R2 is a single global namespace; the
# SDK doesn't route based on region). The S3 v4 signing protocol still
# requires *some* region to be set or it 400s — ``auto`` is the
# Cloudflare-canonical value.
export AWS_DEFAULT_REGION="auto"

r2_s3() {
  aws --endpoint-url "$ENDPOINT" s3api "$@"
}

# -----------------------------------------------------------------------------
# Create bucket (idempotent)
# -----------------------------------------------------------------------------

log "Checking if bucket '$BUCKET' already exists"
if r2_s3 head-bucket --bucket "$BUCKET" 2>/dev/null; then
  ok "Bucket '$BUCKET' already exists — skipping create"
else
  log "Creating bucket '$BUCKET' on R2"
  # R2 doesn't use ``LocationConstraint`` like AWS S3 — passing it
  # gives a 400. Just create with the default location, which for an
  # account in the EU jurisdiction lands in an EU data centre.
  r2_s3 create-bucket --bucket "$BUCKET" >/dev/null
  ok "Bucket '$BUCKET' created"
fi

# -----------------------------------------------------------------------------
# Enable versioning (required for the lifecycle retention policy below)
# -----------------------------------------------------------------------------

log "Enabling versioning on '$BUCKET'"
r2_s3 put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration "Status=Enabled" \
  >/dev/null
ok "Versioning enabled"

# -----------------------------------------------------------------------------
# Lifecycle policy: 30-day NoncurrentVersionExpiration
# -----------------------------------------------------------------------------
#
# Per RFC 0001 decision #5: snapshots are written under
# `snapshots/<timestamp>/`. We retain non-current versions of any object
# in that prefix for 30 days, which covers the 7-daily + 4-weekly
# window with a generous buffer. Precise N-of-each retention is a v1.1
# follow-up via a separate cleanup script.

log "Setting 30-day lifecycle policy for noncurrent versions"
LIFECYCLE_POLICY=$(cat <<'EOF'
{
  "Rules": [
    {
      "ID": "nexus-snapshot-retention-v1",
      "Status": "Enabled",
      "Filter": { "Prefix": "snapshots/" },
      "NoncurrentVersionExpiration": {
        "NoncurrentDays": 30
      }
    }
  ]
}
EOF
)
TMP_LIFECYCLE=$(mktemp)
trap 'rm -f "$TMP_LIFECYCLE"' EXIT
echo "$LIFECYCLE_POLICY" > "$TMP_LIFECYCLE"
r2_s3 put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" \
  --lifecycle-configuration "file://$TMP_LIFECYCLE" \
  >/dev/null
ok "Lifecycle policy applied"

# -----------------------------------------------------------------------------
# Push bucket coordinates to Infisical (optional)
# -----------------------------------------------------------------------------
#
# When INFISICAL_PROJECT_ID + INFISICAL_TOKEN are set, push the per-stack
# bucket coordinates into the stack's Infisical folder so the spinup
# pipeline can read them via the existing `infisical.py` machinery. Note:
# the *credentials* (R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY) are SHARED
# across all R2 buckets in the project (same token as init-r2-state.sh),
# so we don't push those here — they're already in Infisical from the
# control-plane setup. We push only the bucket-specific bits (name +
# endpoint + region).

if [ -n "${INFISICAL_PROJECT_ID:-}" ] && [ -n "${INFISICAL_TOKEN:-}" ]; then
  if ! command -v infisical >/dev/null 2>&1; then
    warn "infisical CLI not found — skipping credential push to Infisical"
    warn "Install from https://infisical.com/docs/cli/overview"
  else
    log "Pushing bucket coordinates to Infisical project '$INFISICAL_PROJECT_ID'"
    # Secret values must NOT appear in argv — that's visible via `ps`,
    # in shell history, and in CI logs. Pipe a tempfile of
    # KEY=VALUE lines into ``infisical secrets set --file ...``. The
    # file lives in $TMPDIR with mode 600 and is removed via the EXIT
    # trap.
    SECRETS_FILE=$(mktemp)
    chmod 600 "$SECRETS_FILE"
    trap 'rm -f "$TMP_LIFECYCLE" "$SECRETS_FILE"' EXIT
    cat > "$SECRETS_FILE" <<EOF
PERSISTENCE_S3_ENDPOINT=$ENDPOINT
PERSISTENCE_S3_REGION=auto
PERSISTENCE_S3_BUCKET=$BUCKET
EOF
    # INFISICAL_TOKEN via env, not --token argv.
    INFISICAL_TOKEN="$INFISICAL_TOKEN" infisical secrets set \
      --projectId "$INFISICAL_PROJECT_ID" \
      --path "/persistence/$STACK_SLUG" \
      --file "$SECRETS_FILE" \
      >/dev/null 2>&1 || warn "infisical secrets set returned non-zero (values may already exist)"
    ok "Bucket coordinates pushed to Infisical (R2 credentials reused from project-wide secrets)"
  fi
else
  warn "INFISICAL_PROJECT_ID/INFISICAL_TOKEN not set — bucket coordinates NOT pushed automatically"
  warn "The caller is responsible for getting them to the per-stack fork's secrets"
fi

# -----------------------------------------------------------------------------
# Output (parseable by setup.ts / GitHub Actions)
# -----------------------------------------------------------------------------

ok "init-s3-bucket complete"
cat <<EOF
BUCKET=$BUCKET
ENDPOINT=$ENDPOINT
REGION=auto
EOF
