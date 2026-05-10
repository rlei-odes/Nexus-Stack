#!/bin/bash
# =============================================================================
# Nexus-Stack - Hetzner Object Storage bucket cleanup (RFC 0001)
# =============================================================================
# Deletes a per-stack persistence bucket on Hetzner Object Storage.
# Called from `destroy-all.yml` workflow ONLY when the operator
# explicitly opted in via `--delete-data` (see RFC 0001 decision #6).
#
# Default behaviour: PRESERVE the bucket. The script is a no-op
# unless the operator passes the explicit confirmation environment
# variable, mirroring the existing `confirm=DESTROY` pattern.
#
# Required environment variables:
#   HETZNER_S3_ACCESS_KEY   - Project-level access key
#   HETZNER_S3_SECRET_KEY   - Matching secret
#   HETZNER_S3_LOCATION     - fsn1 / hel1 / nbg1
#   STACK_SLUG              - Per-stack slug (used as bucket name)
#   CONFIRM_DELETE_DATA     - Must equal 'DESTROY' for the script to act.
#                             Anything else (including unset) → no-op.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[cleanup-s3-bucket]${NC} $*" >&2; }
ok()  { echo -e "${GREEN}[cleanup-s3-bucket] ✓${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[cleanup-s3-bucket] ⚠${NC}  $*" >&2; }
err() { echo -e "${RED}[cleanup-s3-bucket] ✗${NC}  $*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Safety gate (decision #6 — opt-in delete)
# -----------------------------------------------------------------------------
#
# The bucket holds the only copy of the stack's persistent data
# under the v1.0 architecture (no Hetzner volume to fall back on).
# Deleting it without explicit confirmation would silently destroy
# student work — the same pattern as `destroy-all.yml -f
# confirm=DESTROY`, just with a per-data-store gate.

if [ "${CONFIRM_DELETE_DATA:-}" != "DESTROY" ]; then
  warn "CONFIRM_DELETE_DATA != 'DESTROY' — preserving bucket"
  warn "Pass CONFIRM_DELETE_DATA=DESTROY to actually delete the bucket and its contents"
  log "No-op (this is the default safety behaviour)"
  exit 0
fi

# -----------------------------------------------------------------------------
# Argument validation (only run when actually deleting)
# -----------------------------------------------------------------------------

: "${HETZNER_S3_ACCESS_KEY:?HETZNER_S3_ACCESS_KEY is required}"
: "${HETZNER_S3_SECRET_KEY:?HETZNER_S3_SECRET_KEY is required}"
: "${HETZNER_S3_LOCATION:?HETZNER_S3_LOCATION is required (fsn1 / hel1 / nbg1)}"
: "${STACK_SLUG:?STACK_SLUG is required (used as bucket name)}"

if ! [[ "$STACK_SLUG" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  err "STACK_SLUG '$STACK_SLUG' is not a valid S3 bucket name"
fi

case "$HETZNER_S3_LOCATION" in
  fsn1|hel1|nbg1) ;;
  *) err "HETZNER_S3_LOCATION must be one of fsn1, hel1, nbg1 (got '$HETZNER_S3_LOCATION')" ;;
esac

ENDPOINT="https://${HETZNER_S3_LOCATION}.your-objectstorage.com"
BUCKET="$STACK_SLUG"

if ! command -v aws >/dev/null 2>&1; then
  err "aws CLI not found in PATH"
fi

export AWS_ACCESS_KEY_ID="$HETZNER_S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$HETZNER_S3_SECRET_KEY"
export AWS_DEFAULT_REGION="$HETZNER_S3_LOCATION"

aws_s3() {
  aws --endpoint-url "$ENDPOINT" s3api "$@"
}

# -----------------------------------------------------------------------------
# Bucket existence probe
# -----------------------------------------------------------------------------

log "Checking bucket '$BUCKET' on $ENDPOINT"
if ! aws_s3 head-bucket --bucket "$BUCKET" 2>/dev/null; then
  warn "Bucket '$BUCKET' does not exist — nothing to clean up"
  exit 0
fi

# -----------------------------------------------------------------------------
# Empty the bucket (versioning means we need to delete every version)
# -----------------------------------------------------------------------------
#
# `aws s3 rb --force` only deletes current versions; with versioning
# enabled (which init-s3-bucket.sh turned on) we'd be left with a
# bucket that's "empty" but still has thousands of noncurrent
# versions, and the bucket-delete call would 409. The robust path is
# to enumerate every (key, version-id) pair and delete in batches.

log "Listing object versions to delete"
TMP_VERSIONS=$(mktemp)
trap 'rm -f "$TMP_VERSIONS"' EXIT

# Paginate via aws_s3's built-in `--max-items` cursor. Hetzner's
# Object Storage caps a single ListObjectVersions response at 1000
# entries (matching AWS's documented limit), so we paginate.
NEXT_TOKEN=""
DELETED_COUNT=0
while :; do
  if [ -z "$NEXT_TOKEN" ]; then
    PAGE=$(aws_s3 list-object-versions --bucket "$BUCKET" --max-items 1000 --output json)
  else
    PAGE=$(aws_s3 list-object-versions --bucket "$BUCKET" --max-items 1000 \
      --starting-token "$NEXT_TOKEN" --output json)
  fi

  # Two collections to delete: live `Versions` and tombstone `DeleteMarkers`
  echo "$PAGE" | python3 -c '
import json, sys
page = json.load(sys.stdin)
to_delete = []
for v in (page.get("Versions") or []):
    to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
for m in (page.get("DeleteMarkers") or []):
    to_delete.append({"Key": m["Key"], "VersionId": m["VersionId"]})
if not to_delete:
    sys.exit(0)
print(json.dumps({"Objects": to_delete, "Quiet": True}))
' > "$TMP_VERSIONS"

  if [ -s "$TMP_VERSIONS" ]; then
    aws_s3 delete-objects --bucket "$BUCKET" --delete "file://$TMP_VERSIONS" >/dev/null
    BATCH=$(grep -c '"Key"' "$TMP_VERSIONS" || true)
    DELETED_COUNT=$((DELETED_COUNT + BATCH))
  fi

  NEXT_TOKEN=$(echo "$PAGE" | python3 -c '
import json, sys
page = json.load(sys.stdin)
print(page.get("NextToken") or "")
')
  if [ -z "$NEXT_TOKEN" ]; then
    break
  fi
done

ok "Deleted $DELETED_COUNT object versions"

# -----------------------------------------------------------------------------
# Delete the bucket itself
# -----------------------------------------------------------------------------

log "Deleting bucket '$BUCKET'"
aws_s3 delete-bucket --bucket "$BUCKET" >/dev/null
ok "Bucket '$BUCKET' deleted"
