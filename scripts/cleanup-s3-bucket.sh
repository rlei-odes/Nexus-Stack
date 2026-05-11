#!/bin/bash
# =============================================================================
# Nexus-Stack - Cloudflare R2 persistence bucket cleanup (RFC 0001)
# =============================================================================
# Deletes a per-stack R2 persistence bucket. STANDALONE script — run
# manually by the operator when they want to wipe snapshot history
# (the audited deletion path per RFC 0001 decision #6). Not invoked
# by destroy-all.yml — that workflow intentionally preserves the
# bucket so operators can re-attach to existing snapshots on the
# next initial-setup. See destroy-all.yml's R2-persistence comment
# for the operator workflow.
#
# Default behaviour: PRESERVE the bucket. The script is a no-op
# unless the operator passes the explicit confirmation environment
# variable, mirroring the existing `confirm=DESTROY` pattern.
#
# Required environment variables:
#   CLOUDFLARE_ACCOUNT_ID    - Cloudflare account ID
#   R2_ACCESS_KEY_ID         - R2 S3-API access key (shared project token)
#   R2_SECRET_ACCESS_KEY     - matching secret
#   STACK_SLUG               - Per-stack slug (used as bucket name)
#   CONFIRM_DELETE_DATA      - Must equal 'DESTROY' for the script to act.
#                              Anything else (including unset) → no-op.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[cleanup-s3-bucket]${NC} $*" >&2; }
ok()   { echo -e "${GREEN}[cleanup-s3-bucket] ✓${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[cleanup-s3-bucket] ⚠${NC}  $*" >&2; }
err()  { echo -e "${RED}[cleanup-s3-bucket] ✗${NC}  $*" >&2; exit 1; }

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

: "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is required}"
: "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID is required (reuse the one from init-r2-state.sh)}"
: "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY is required}"
: "${STACK_SLUG:?STACK_SLUG is required (used as bucket name)}"

if ! [[ "$STACK_SLUG" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  err "STACK_SLUG '$STACK_SLUG' is not a valid R2 bucket name"
fi

ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
BUCKET="$STACK_SLUG"

if ! command -v aws >/dev/null 2>&1; then
  err "aws CLI not found in PATH"
fi
# The pagination loop below parses ``list-object-versions`` JSON
# output via python3. Check up-front so a missing interpreter
# fails the script BEFORE we start deleting; without the check, the
# loop would tear through one page of versions, then error half-way
# with ``python3: command not found`` leaving the bucket in a
# partially-emptied state.
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found in PATH — needed for paginated version-list parsing. Install Python 3 or use a runner image that includes it."
fi

export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="auto"

r2_s3() {
  aws --endpoint-url "$ENDPOINT" s3api "$@"
}

# -----------------------------------------------------------------------------
# Bucket existence probe
# -----------------------------------------------------------------------------

log "Checking bucket '$BUCKET' on $ENDPOINT"
if ! r2_s3 head-bucket --bucket "$BUCKET" 2>/dev/null; then
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

# Paginate via aws_s3's built-in `--max-items` cursor. R2 caps a
# single ListObjectVersions response at 1000 entries (matching AWS's
# documented limit), so we paginate.
NEXT_TOKEN=""
DELETED_COUNT=0
while :; do
  if [ -z "$NEXT_TOKEN" ]; then
    PAGE=$(r2_s3 list-object-versions --bucket "$BUCKET" --max-items 1000 --output json)
  else
    PAGE=$(r2_s3 list-object-versions --bucket "$BUCKET" --max-items 1000 \
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
    r2_s3 delete-objects --bucket "$BUCKET" --delete "file://$TMP_VERSIONS" >/dev/null
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
r2_s3 delete-bucket --bucket "$BUCKET" >/dev/null
ok "Bucket '$BUCKET' deleted"
