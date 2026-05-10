#!/bin/bash
# =============================================================================
# Nexus-Stack - Hetzner Object Storage bucket bootstrap (RFC 0001)
# =============================================================================
# Creates the per-stack persistence bucket on Hetzner Object Storage,
# enables versioning, sets a 30-day NoncurrentVersionExpiration
# lifecycle policy (the safety net that covers the eventual 7-daily +
# 4-weekly retention window per RFC 0001 decision #5 — precise N-of-
# each retention is enforced by a separate cleanup script in v1.1),
# and pushes the access credentials to Infisical so the stack's
# spinup pipeline can read them.
#
# Called once per stack, either:
#   - by the operator from their workstation during initial setup
#   - by the Education repo's setup.ts during fork creation
#
# Idempotent: running it twice with the same arguments produces no
# changes after the first run. The bucket-exists check lets a
# re-trigger after a partial failure (e.g. lifecycle policy didn't
# stick) succeed cleanly.
#
# Required environment variables:
#   HETZNER_S3_ACCESS_KEY    - Project-level access key with create-bucket scope
#   HETZNER_S3_SECRET_KEY    - Matching secret
#   HETZNER_S3_LOCATION      - fsn1 / hel1 / nbg1 (must match the project)
#   STACK_SLUG               - Per-stack slug, e.g. "nexus-stefan-hslu"
#                              (used as bucket name; must match S3 naming rules)
#   INFISICAL_PROJECT_ID     - Where to push the bucket creds (optional;
#                              skipped with a warning if unset)
#   INFISICAL_TOKEN          - Infisical service-account token (optional)
#
# Outputs (on stdout):
#   BUCKET=<bucket-name>
#   ENDPOINT=<https://<location>.your-objectstorage.com>
#   REGION=<location>
#
# The Education repo's setup.ts captures these and writes them as
# GitHub Actions secrets on the per-stack fork.
# =============================================================================

set -euo pipefail

# Colors for human output. Logs to stderr (so the stdout-parsable
# KEY=VALUE block at the end isn't polluted).
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[init-s3-bucket]${NC} $*" >&2; }
ok()  { echo -e "${GREEN}[init-s3-bucket] ✓${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[init-s3-bucket] ⚠${NC}  $*" >&2; }
err() { echo -e "${RED}[init-s3-bucket] ✗${NC}  $*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Argument validation
# -----------------------------------------------------------------------------

: "${HETZNER_S3_ACCESS_KEY:?HETZNER_S3_ACCESS_KEY is required}"
: "${HETZNER_S3_SECRET_KEY:?HETZNER_S3_SECRET_KEY is required}"
: "${HETZNER_S3_LOCATION:?HETZNER_S3_LOCATION is required (fsn1 / hel1 / nbg1)}"
: "${STACK_SLUG:?STACK_SLUG is required (used as bucket name)}"

# Validate STACK_SLUG against S3 bucket-name rules — same regex as
# the s3_persistence Python module so a slug rejected here would
# also be rejected at script-render time.
if ! [[ "$STACK_SLUG" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  err "STACK_SLUG '$STACK_SLUG' is not a valid S3 bucket name (3-63 chars, lowercase, digits, hyphens, dots)"
fi

case "$HETZNER_S3_LOCATION" in
  fsn1|hel1|nbg1) ;;
  *) err "HETZNER_S3_LOCATION must be one of fsn1, hel1, nbg1 (got '$HETZNER_S3_LOCATION')" ;;
esac

ENDPOINT="https://${HETZNER_S3_LOCATION}.your-objectstorage.com"
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

# We rely on the v2 CLI's --endpoint-url flag, which has been stable
# since 2020. v1 is technically supported but quirky on signed-URL
# generation; nudge the operator if they're on an old build.
AWS_VERSION=$(aws --version 2>&1 | head -1)
log "Using $AWS_VERSION"

# Per-call AWS config. We avoid touching the operator's ~/.aws/
# directory so the init-bucket script doesn't overwrite an existing
# personal AWS profile.
export AWS_ACCESS_KEY_ID="$HETZNER_S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$HETZNER_S3_SECRET_KEY"
export AWS_DEFAULT_REGION="$HETZNER_S3_LOCATION"

aws_s3() {
  aws --endpoint-url "$ENDPOINT" s3api "$@"
}

# -----------------------------------------------------------------------------
# Create bucket (idempotent)
# -----------------------------------------------------------------------------

log "Checking if bucket '$BUCKET' already exists"
if aws_s3 head-bucket --bucket "$BUCKET" 2>/dev/null; then
  ok "Bucket '$BUCKET' already exists — skipping create"
else
  log "Creating bucket '$BUCKET' in $HETZNER_S3_LOCATION"
  # Hetzner Object Storage requires the LocationConstraint even
  # though the endpoint URL already encodes the location — leaving
  # it out gets a 400 with an unhelpful "InvalidLocationConstraint"
  # error. Pin it explicitly.
  aws_s3 create-bucket \
    --bucket "$BUCKET" \
    --create-bucket-configuration "LocationConstraint=$HETZNER_S3_LOCATION" \
    >/dev/null
  ok "Bucket '$BUCKET' created"
fi

# -----------------------------------------------------------------------------
# Enable versioning (required for the lifecycle retention policy below)
# -----------------------------------------------------------------------------

log "Enabling versioning on '$BUCKET'"
aws_s3 put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration "Status=Enabled" \
  >/dev/null
ok "Versioning enabled"

# -----------------------------------------------------------------------------
# Lifecycle policy: retain last 7 daily + 4 weekly snapshots
# -----------------------------------------------------------------------------
#
# Per RFC 0001 decision #5: snapshots-per-stack are written under
# `snapshots/<timestamp>/`. We treat every non-current version of
# the manifest.json + payload tree as a "previous snapshot". The
# rule below evicts non-current versions older than 30 days, which
# covers the 7-daily + 4-weekly window with a generous buffer.
#
# More granular retention (exactly 7+4) requires either tag-based
# rules or a separate cleanup cron — out of scope for v1.0. The
# 30-day cap is the safety net; a follow-up PR can refine it.

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
aws_s3 put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" \
  --lifecycle-configuration "file://$TMP_LIFECYCLE" \
  >/dev/null
ok "Lifecycle policy applied"

# -----------------------------------------------------------------------------
# Push credentials to Infisical (optional)
# -----------------------------------------------------------------------------
#
# When INFISICAL_PROJECT_ID + INFISICAL_TOKEN are set, push the per-stack
# credentials into the stack's Infisical folder so the spinup pipeline
# can read them via the existing `infisical.py` machinery. We push the
# project-level access key as-is — per-bucket sub-keys are a v1.1
# refinement that needs Hetzner's sub-user API, not yet implemented.

if [ -n "${INFISICAL_PROJECT_ID:-}" ] && [ -n "${INFISICAL_TOKEN:-}" ]; then
  if ! command -v infisical >/dev/null 2>&1; then
    warn "infisical CLI not found — skipping credential push to Infisical"
    warn "Install from https://infisical.com/docs/cli/overview"
  else
    log "Pushing bucket credentials to Infisical project '$INFISICAL_PROJECT_ID'"
    # Secret values must NOT appear in argv — that's visible via
    # `ps`, in shell history, and in CI logs. We pipe a tempfile of
    # `KEY=VALUE` lines into `infisical secrets set --read-from-file
    # -` (or via stdin equivalent). The file lives in $TMPDIR with
    # mode 600 and is removed via the EXIT trap.
    #
    # Path: `/persistence/$STACK_SLUG` matches the existing
    # `secret_sync.py` folder convention. We push 5 keys: endpoint,
    # region, bucket, access_key, secret_key.
    SECRETS_FILE=$(mktemp)
    chmod 600 "$SECRETS_FILE"
    # Append to the existing trap so we don't clobber the lifecycle
    # tempfile cleanup set earlier in the script.
    trap 'rm -f "$TMP_LIFECYCLE" "$SECRETS_FILE"' EXIT
    cat > "$SECRETS_FILE" <<EOF
S3_ENDPOINT=$ENDPOINT
S3_REGION=$HETZNER_S3_LOCATION
S3_BUCKET=$BUCKET
S3_ACCESS_KEY=$HETZNER_S3_ACCESS_KEY
S3_SECRET_KEY=$HETZNER_S3_SECRET_KEY
EOF
    # Pass the token via env, not argv, for the same reason. The
    # CLI honours `INFISICAL_TOKEN` from the environment per its
    # docs, so we just unset the explicit `--token` flag.
    INFISICAL_TOKEN="$INFISICAL_TOKEN" infisical secrets set \
      --projectId "$INFISICAL_PROJECT_ID" \
      --path "/persistence/$STACK_SLUG" \
      --file "$SECRETS_FILE" \
      >/dev/null 2>&1 || warn "infisical secrets set returned non-zero (values may already exist)"
    ok "Credentials pushed to Infisical (via stdin file, not argv)"
  fi
else
  warn "INFISICAL_PROJECT_ID/INFISICAL_TOKEN not set — credentials NOT pushed automatically"
  warn "The caller is responsible for getting them to the per-stack fork's secrets"
fi

# -----------------------------------------------------------------------------
# Output (parseable by setup.ts / GitHub Actions)
# -----------------------------------------------------------------------------

ok "init-s3-bucket complete"
# Single trailing block on stdout in `KEY=VALUE` form. This is what
# the calling automation parses; everything else above went to stderr.
cat <<EOF
BUCKET=$BUCKET
ENDPOINT=$ENDPOINT
REGION=$HETZNER_S3_LOCATION
EOF
