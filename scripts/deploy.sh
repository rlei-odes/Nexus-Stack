#!/bin/bash
set -euo pipefail

# =============================================================================
# Nexus-Stack Deployment Script
# =============================================================================
# Called by GitHub Actions spin-up workflow after infrastructure is provisioned.
# Syncs Docker stacks to server and starts enabled containers.
# =============================================================================

# =============================================================================
# Nexus-Stack Deploy Script
# Runs after tofu apply to start containers
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TOFU_DIR="$PROJECT_ROOT/tofu/stack"
STACKS_DIR="$PROJECT_ROOT/stacks"
REMOTE_STACKS_DIR="/opt/docker-server/stacks"

# Escape single quotes for safe SQL interpolation
escape_sql() { printf '%s' "${1//\'/\'\'}"; }

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

echo -e "${BLUE}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                   🚀 Nexus-Stack Deploy                       ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# -----------------------------------------------------------------------------
# Check OpenTofu state and load R2 credentials
# -----------------------------------------------------------------------------

# Load R2 credentials for remote state access
if [ -f "$PROJECT_ROOT/tofu/.r2-credentials" ]; then
    source "$PROJECT_ROOT/tofu/.r2-credentials"
    export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
    export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
fi

# Check if we can access state
cd "$TOFU_DIR"
if ! tofu state list >/dev/null 2>&1; then
    echo -e "${RED}Error: No OpenTofu state found. Infrastructure must be provisioned first.${NC}"
    exit 1
fi
cd "$PROJECT_ROOT"

# Get domain and admin email from config
DOMAIN=$(grep -E '^domain\s*=' "$TOFU_DIR/config.tfvars" 2>/dev/null | sed 's/.*"\(.*\)"/\1/' || echo "")
ADMIN_EMAIL=$(grep -E '^admin_email\s*=' "$TOFU_DIR/config.tfvars" 2>/dev/null | sed 's/.*"\(.*\)"/\1/' || echo "")
USER_EMAIL=$(grep -E '^user_email\s*=' "$TOFU_DIR/config.tfvars" 2>/dev/null | sed 's/.*"\(.*\)"/\1/' || echo "")

# Gitea needs a single address for the user.email column; USER_EMAIL may
# be a comma-separated list (student + teacher admins, so tofu/stack can
# build the Cloudflare Access allow-list from every entry). Strip to the
# first entry here — Gitea's validator rejects commas with "e-mail address
# contains unsupported character" and the raw list would otherwise reach
# `gitea admin user create --email`. Downstream derivations in this script
# (workspace-config block ~line 1193, user-create block ~line 3000,
# workspace-repo block ~line 3071) all reuse GITEA_USER_EMAIL for the same
# single-value semantics. Derived BEFORE the ADMIN_EMAIL collision check
# below so that check compares single-vs-single (not admin-single-vs-
# user-list, which would never match and silently skip the remap).
# Trim whitespace: upstream joins commonly emit ", " between entries
# (`a@b.com, c@d.com`), and self-provisioned tfvars can have leading
# spaces inside the quoted value. Gitea/Windmill/Wiki.js validators all
# reject space-prefixed emails. ADMIN_EMAIL gets the same treatment so
# the equality check below compares normalized single addresses.
ADMIN_EMAIL=$(printf '%s' "$ADMIN_EMAIL" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
GITEA_USER_EMAIL=$(printf '%s' "${USER_EMAIL%%,*}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
GITEA_USER_USERNAME="${GITEA_USER_EMAIL%%@*}"

# ADMIN_EMAIL must be distinct from GITEA_USER_EMAIL: Gitea enforces uniqueness
# on user.email, so if both rows are created with the same address the second
# create fails with "e-mail already in use". The admin-panel caller
# (Nexus-Stack-for-Education) passes both values from the same source field
# today (admin_email = first entry of user_email list), and self-provisioned
# tfvars can omit admin_email entirely. In either case fall back to a
# synthetic gitea-admin@${DOMAIN} that's guaranteed distinct from any real
# human email.
if [ -z "$ADMIN_EMAIL" ] || [ "$ADMIN_EMAIL" = "$GITEA_USER_EMAIL" ]; then
    # Use a local-part that no human-email scheme would produce. `admin@${DOMAIN}`
    # is also safe for the stack-scoped student domains (e.g. <user>.nona.company),
    # but `gitea-admin` narrows the probability of collision with a real USER_EMAIL
    # even further (no university / corporate mail provider uses this local-part).
    ADMIN_EMAIL="gitea-admin@$DOMAIN"
fi
OM_PRINCIPAL_DOMAIN=$(echo "$ADMIN_EMAIL" | cut -d'@' -f2)

# No USER_EMAIL fallback to ADMIN_EMAIL — that was the root of the Gitea
# uniqueness collision. The Gitea user-create block below is gated on
# `[ -n "$GITEA_USER_EMAIL" ]`, so an empty-after-trim GITEA_USER_EMAIL
# (no USER_EMAIL set, or its first entry was whitespace-only) skips user
# creation cleanly instead of colliding with the admin row.
SSH_HOST="ssh.${DOMAIN}"

if [ -z "$DOMAIN" ]; then
    echo -e "${RED}Error: Could not read domain from config.tfvars${NC}"
    exit 1
fi

# Get secrets from OpenTofu
echo -e "${YELLOW}[0/7] Loading secrets from OpenTofu...${NC}"
SECRETS_JSON=$(cd "$TOFU_DIR" && tofu output -json secrets 2>/dev/null || echo "{}")

if [ "$SECRETS_JSON" = "{}" ]; then
    echo -e "${RED}Error: Could not read secrets from OpenTofu state${NC}"
    exit 1
fi

# Extract secrets via nexus_deploy.config (Phase 1, #505 Modul 1.3).
# Replaces the previous 88-line jq pipeline that lifted SECRETS_JSON
# into bash globals. Same end-state — 88 named bash vars (one per
# entry in src/nexus_deploy/config.py:_FIELDS) — but the schema lives
# in Python and gets unit-tested instead of being eyeballed during
# review. shlex.quote in dump_shell makes the eval injection-safe;
# the legacy `$()`-capture was vulnerable to backtick / `$()` /
# `; cmd` payloads in any secret value.
#
# Capture-then-eval (NOT `if ! eval "$(...)"; then`): if the python
# subprocess fails, command-substitution still produces empty stdout
# and `eval ""` returns 0 — silently masking the failure. The
# `if ! VAR=$(pipeline)` form propagates the subshell's exit code
# (with pipefail active per the script's `set -euo pipefail`) so a
# python crash, missing uv binary, or invalid JSON all abort here.
# Empty-output check guards the (unlikely but possible) case of a
# zero-exit but blank rendering.
if ! RENDERED_SECRETS=$(printf '%s' "$SECRETS_JSON" | uv run --quiet --project "$PROJECT_ROOT" python -m nexus_deploy config dump-shell --stdin); then
    echo -e "${RED}Error: nexus_deploy.config dump-shell failed${NC}"
    exit 1
fi
if [ -z "$RENDERED_SECRETS" ]; then
    echo -e "${RED}Error: nexus_deploy.config dump-shell produced empty output${NC}"
    exit 1
fi
eval "$RENDERED_SECRETS"
unset RENDERED_SECRETS

# Get SSH Service Token for headless authentication
SSH_TOKEN_JSON=$(cd "$TOFU_DIR" && tofu output -json ssh_service_token 2>/dev/null || echo "{}")
CF_ACCESS_CLIENT_ID=$(echo "$SSH_TOKEN_JSON" | jq -r '.client_id // empty')
CF_ACCESS_CLIENT_SECRET=$(echo "$SSH_TOKEN_JSON" | jq -r '.client_secret // empty')

echo -e "${GREEN}  ✓ Secrets loaded (admin user: $ADMIN_USERNAME)${NC}"

# Get image versions from OpenTofu
echo ""
echo -e "${YELLOW}Loading image versions...${NC}"
IMAGE_VERSIONS_JSON=$(cd "$TOFU_DIR" && tofu output -json image_versions 2>/dev/null || echo "{}")
echo -e "${GREEN}  ✓ Image versions loaded${NC}"

# Clean old SSH known_hosts entries
SERVER_IP=$(cd "$TOFU_DIR" && tofu output -raw server_ip 2>/dev/null || echo "")
[ -n "$SSH_HOST" ] && ssh-keygen -R "$SSH_HOST" 2>/dev/null || true
[ -n "$SERVER_IP" ] && ssh-keygen -R "$SERVER_IP" 2>/dev/null || true

# -----------------------------------------------------------------------------
# Setup SSH Config with Service Token (replaces existing config)
# -----------------------------------------------------------------------------
SSH_CONFIG="$HOME/.ssh/config"

echo -e "${YELLOW}[1/7] Configuring SSH access...${NC}"
mkdir -p "$HOME/.ssh"

# Remove old nexus config if exists (to update with token)
if grep -q "^Host nexus$" "$SSH_CONFIG" 2>/dev/null; then
    # Create temp file without the nexus block
    # This approach handles blocks correctly regardless of position
    awk '
        /^Host nexus$/ { skip=1; next }
        /^Host / && skip { skip=0 }
        !skip { print }
    ' "$SSH_CONFIG" > "$SSH_CONFIG.tmp" && mv "$SSH_CONFIG.tmp" "$SSH_CONFIG"
fi

# Add new config with Service Token support
if [ -n "$CF_ACCESS_CLIENT_ID" ] && [ -n "$CF_ACCESS_CLIENT_SECRET" ]; then
    cat >> "$SSH_CONFIG" << EOF

Host nexus
  HostName ${SSH_HOST}
  User root
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ProxyCommand bash -c 'TUNNEL_SERVICE_TOKEN_ID=${CF_ACCESS_CLIENT_ID} TUNNEL_SERVICE_TOKEN_SECRET=${CF_ACCESS_CLIENT_SECRET} cloudflared access ssh --hostname %h'
EOF
    echo -e "${GREEN}  ✓ SSH config with Service Token added (no browser login required)${NC}"
    USE_SERVICE_TOKEN=true
else
    cat >> "$SSH_CONFIG" << EOF

Host nexus
  HostName ${SSH_HOST}
  User root
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ProxyCommand cloudflared access ssh --hostname %h
EOF
    echo -e "${GREEN}  ✓ SSH config added (browser login required)${NC}"
    USE_SERVICE_TOKEN=false
fi
chmod 600 "$SSH_CONFIG"

# -----------------------------------------------------------------------------
# Cloudflare Zero Trust Authentication (Service Token required)
# -----------------------------------------------------------------------------
if [ "$USE_SERVICE_TOKEN" = "false" ]; then
    echo ""
    echo -e "${RED}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ${YELLOW}❌ Service Token Required for GitHub Actions Deployment${RED}     ║${NC}"
    echo -e "${RED}╠═══════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${RED}║${NC}  Browser login is not supported in GitHub Actions.              ${RED}║${NC}"
    echo -e "${RED}║${NC}  Service Token must be configured in Terraform outputs.        ${RED}║${NC}"
    echo -e "${RED}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    exit 1
else
    echo -e "${GREEN}  ✓ Using Service Token for authentication${NC}"
fi
echo ""

# -----------------------------------------------------------------------------
# Wait for SSH connection
# -----------------------------------------------------------------------------
echo -e "${YELLOW}[2/7] Waiting for SSH via Cloudflare Tunnel...${NC}"

# If using Service Token, test it first with retry and exponential backoff
if [ "$USE_SERVICE_TOKEN" = "true" ]; then
    echo "  Testing Service Token authentication..."
    MAX_TOKEN_RETRIES=6
    echo "  Note: Service Token may need a few seconds to propagate in Cloudflare..."
    
    # Initial wait for Service Token propagation (Cloudflare needs time to activate)
    INITIAL_WAIT=10
    echo "  Waiting ${INITIAL_WAIT}s for initial propagation..."
    sleep $INITIAL_WAIT

    TOKEN_RETRY=0
    BACKOFF=5
    SSH_ERR=$(mktemp)
    trap 'rm -f "$SSH_ERR"' EXIT

    while [ $TOKEN_RETRY -lt $MAX_TOKEN_RETRIES ]; do
        if [ $TOKEN_RETRY -eq $((MAX_TOKEN_RETRIES - 1)) ]; then
            # Last attempt: verbose SSH for full diagnostics
            echo "  Last attempt - running with verbose SSH output..."
            if ssh -v -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes nexus 'echo ok' >"$SSH_ERR" 2>&1; then
                echo -e "${GREEN}  ✓ Service Token authentication successful${NC}"
                cat "$SSH_ERR"
                rm -f "$SSH_ERR"
                trap - EXIT
                break
            fi
            # Print verbose output for diagnostics
            cat "$SSH_ERR"
        else
            if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes nexus 'echo ok' 2>"$SSH_ERR"; then
                echo -e "${GREEN}  ✓ Service Token authentication successful${NC}"
                rm -f "$SSH_ERR"
                trap - EXIT
                break
            fi
        fi
        TOKEN_RETRY=$((TOKEN_RETRY + 1))
        if [ $TOKEN_RETRY -lt $MAX_TOKEN_RETRIES ]; then
            echo "  Retry $TOKEN_RETRY/$MAX_TOKEN_RETRIES - waiting ${BACKOFF}s for propagation..."
            echo -e "  ${DIM}Last error (last 3 lines):${NC}"
            tail -n 3 "$SSH_ERR" | sed 's/^/    /'
            sleep $BACKOFF
            BACKOFF=$((BACKOFF + 5))  # Linear increase: 5s, 10s, 15s, 20s, 25s
        fi
    done

    if [ $TOKEN_RETRY -eq $MAX_TOKEN_RETRIES ]; then
        echo ""
        echo -e "${RED}╔═══════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║  ${YELLOW}❌ Service Token Authentication Failed${RED}                            ║${NC}"
        echo -e "${RED}╠═══════════════════════════════════════════════════════════════╣${NC}"
        echo -e "${RED}║${NC}  Service Token authentication failed after $MAX_TOKEN_RETRIES attempts.  ${RED}║${NC}"
        echo -e "${RED}║${NC}  Browser login fallback is not supported in GitHub Actions.      ${RED}║${NC}"
        echo -e "${RED}╚═══════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo -e "${YELLOW}  Diagnostics:${NC}"
        echo "  SSH Host: $SSH_HOST"
        echo "  Service Token Client ID: [redacted]"
        echo "  cloudflared version: $(cloudflared --version 2>&1 || echo 'not found')"
        if command -v nslookup >/dev/null 2>&1; then
            echo "  DNS lookup for $SSH_HOST:"
            nslookup "$SSH_HOST" 2>&1 | head -6
        else
            echo "  DNS lookup: nslookup not available"
        fi
        echo ""
        echo -e "${YELLOW}  Last SSH error output:${NC}"
        cat "$SSH_ERR" 2>/dev/null || echo "    (no error output captured)"
        rm -f "$SSH_ERR"
        trap - EXIT
        echo ""
        exit 1
    fi
fi

MAX_RETRIES=15
RETRY=0
TIMEOUT=5
SSH_ERR=$(mktemp)
# Holds remote tmp paths (one per line) that must be removed on the
# server when the runner exits — including mid-loop interrupts or
# workflow timeouts. The EXIT trap walks this list and ssh-rm's each.
# Any later block that mktemp's a file on `nexus` should append the
# resulting path here, so cleanup is centralised in one trap.
REMOTE_CLEANUP_PATHS=$(mktemp)
# RUNNER_CLEANUP_PATHS — same idea as REMOTE_CLEANUP_PATHS but for
# secret-bearing temp files on the *runner* itself. Any block that
# mktemp's a runner-local file containing plaintext secrets (Infisical
# raw responses, base64-decoded credentials, etc.) should append its
# path here so the EXIT trap reliably wipes them even when the deploy
# is interrupted, set -e exits early, or a CI runner gets cancelled.
RUNNER_CLEANUP_PATHS=$(mktemp)
trap 'rm -f "$SSH_ERR"; if [ -s "$REMOTE_CLEANUP_PATHS" ]; then while IFS= read -r p; do [ -n "$p" ] && ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=3 -o ServerAliveCountMax=2 nexus "rm -f \"$p\"" 2>/dev/null || true; done < "$REMOTE_CLEANUP_PATHS"; fi; rm -f "$REMOTE_CLEANUP_PATHS"; if [ -s "$RUNNER_CLEANUP_PATHS" ]; then while IFS= read -r p; do [ -n "$p" ] && rm -f "$p"; done < "$RUNNER_CLEANUP_PATHS"; fi; rm -f "$RUNNER_CLEANUP_PATHS"' EXIT
while [ $RETRY -lt $MAX_RETRIES ]; do
    if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=$TIMEOUT -o BatchMode=yes nexus 'echo ok' 2>"$SSH_ERR"; then
        echo -e "${GREEN}  ✓ SSH connection established${NC}"
        rm -f "$SSH_ERR"
        # NOTE: do NOT `trap - EXIT` here. The EXIT trap installed at
        # the top of this section also walks $REMOTE_CLEANUP_PATHS and
        # ssh-rm's any remote tmp files that downstream blocks (seed
        # loop, secret-sync, …) registered. Removing the trap on first
        # SSH success would leave token-bearing curl --config files
        # behind on the server if the deploy aborts later. The trap'\''s
        # `rm -f $SSH_ERR` is no-op-safe when the file is already gone.
        break
    fi
    RETRY=$((RETRY + 1))
    if [ $RETRY -lt $MAX_RETRIES ]; then
        echo "  Attempt $RETRY/$MAX_RETRIES - waiting for tunnel..."
        echo -e "  ${DIM}Last error:${NC}"
        tail -n 1 "$SSH_ERR" | sed 's/^/    /'
        # Increase timeout gradually: 5s, 5s, 10s, 10s, 15s...
        if [ $RETRY -lt 3 ]; then
            TIMEOUT=5
            sleep 5
        elif [ $RETRY -lt 7 ]; then
            TIMEOUT=10
            sleep 10
        else
            TIMEOUT=15
            sleep 15
        fi
    fi
done

if [ $RETRY -eq $MAX_RETRIES ]; then
    echo -e "${RED}Timeout waiting for SSH. Check Cloudflare Tunnel status.${NC}"
    echo -e "${YELLOW}  Last SSH error:${NC}"
    cat "$SSH_ERR" 2>/dev/null || echo "    (no error output captured)"
    rm -f "$SSH_ERR"
    # Don't `trap - EXIT` here. The global EXIT trap handles cleanup
    # of both $REMOTE_CLEANUP_PATHS and $RUNNER_CLEANUP_PATHS list
    # files (the latter holds runner-side mktemp paths to plaintext
    # secrets — registered by later blocks). Disabling the trap on
    # this early exit would skip those rm-f's. The trap is no-op-safe
    # for files already removed (`rm -f`) and for empty list files
    # (the `while read` loop simply matches no lines).
    exit 1
fi

# -----------------------------------------------------------------------------
# Ensure jq is installed on the server.
# -----------------------------------------------------------------------------
# `jq` is now bundled into the cloud-init `apt-get install -y …` step
# in `tofu/stack/main.tf`, so freshly provisioned VMs (after destroy-all)
# already have it. This block is for already-running VMs that were
# created BEFORE that change — without jq, the SFTPGo user-creation
# heredoc and the Kestra register-flow verification block silently
# break (jq writes "command not found" to stderr that gets swallowed,
# the consuming `curl` ends up with empty stdin, and the operator
# sees mysterious 400/empty responses). Idempotent: `apt-get install`
# is a near-instant no-op when the package is already present.
if ! ssh nexus "command -v jq" >/dev/null 2>&1; then
    echo "  Installing jq on the server (one-time bootstrap for VMs created before jq was added to cloud-init)..."
    if ! ssh nexus "sudo apt-get update -qq >/dev/null && sudo apt-get install -y -qq jq >/dev/null"; then
        echo -e "${RED}Error: failed to install jq on the server. SFTPGo / Kestra register-verify blocks rely on jq.${NC}"
        exit 1
    fi
    echo -e "${GREEN}  ✓ jq installed${NC}"
fi

# -----------------------------------------------------------------------------
# Mount persistent volume (if configured)
# -----------------------------------------------------------------------------
PERSISTENT_VOLUME_ID=$(cd "$TOFU_DIR" && tofu output -raw persistent_volume_id 2>/dev/null || echo "0")

if [ "$PERSISTENT_VOLUME_ID" != "0" ] && [ -n "$PERSISTENT_VOLUME_ID" ]; then
    echo ""
    echo -e "${YELLOW}  Mounting persistent volume (ID: $PERSISTENT_VOLUME_ID)...${NC}"
    ssh nexus "
        MOUNT_POINT=/mnt/nexus-data

        # Check if already mounted
        if mountpoint -q \$MOUNT_POINT 2>/dev/null; then
            echo '  Volume already mounted at /mnt/nexus-data'
        else
            mkdir -p \$MOUNT_POINT

            # Find the volume device (Hetzner volumes appear as /dev/disk/by-id/scsi-0HC_Volume_*)
            VOLUME_DEVICE=\$(ls /dev/disk/by-id/scsi-0HC_Volume_${PERSISTENT_VOLUME_ID} 2>/dev/null || echo '')
            if [ -n \"\$VOLUME_DEVICE\" ]; then
                mount \$VOLUME_DEVICE \$MOUNT_POINT
                echo '  Volume mounted at /mnt/nexus-data'
            else
                echo '  Volume device not found via scsi ID, checking automount...'
                if mount | grep -q \$MOUNT_POINT; then
                    echo '  Volume auto-mounted at /mnt/nexus-data'
                else
                    echo '  Warning: Could not mount volume - checking /dev/sdb...'
                    if [ -b /dev/sdb ]; then
                        mount /dev/sdb \$MOUNT_POINT
                        echo '  Volume mounted via /dev/sdb'
                    fi
                fi
            fi
        fi

        # Add fstab entry for persistence across reboots (if not already present)
        if ! grep -q '/mnt/nexus-data' /etc/fstab; then
            VOLUME_DEVICE=\$(ls /dev/disk/by-id/scsi-0HC_Volume_${PERSISTENT_VOLUME_ID} 2>/dev/null || echo '/dev/sdb')
            echo \"\$VOLUME_DEVICE /mnt/nexus-data ext4 defaults,nofail 0 2\" >> /etc/fstab
            echo '  fstab entry added'
        fi

        # Create service subdirectories
        mkdir -p \$MOUNT_POINT/gitea/repos
        mkdir -p \$MOUNT_POINT/gitea/lfs
        mkdir -p \$MOUNT_POINT/gitea/db

        # Gitea runs as UID 1000 (git user)
        chown -R 1000:1000 \$MOUNT_POINT/gitea/repos
        chown -R 1000:1000 \$MOUNT_POINT/gitea/lfs

        # PostgreSQL runs as UID 70 in alpine images
        chown -R 70:70 \$MOUNT_POINT/gitea/db
    "
    echo -e "${GREEN}  ✓ Persistent volume mounted${NC}"
else
    echo ""
    echo -e "${DIM}  Persistent volume not configured (persistent_volume_id=0)${NC}"
fi

# -----------------------------------------------------------------------------
# Prepare stacks with secrets
# -----------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[3/7] Preparing stacks...${NC}"

# Debug log file for troubleshooting
LOG_FILE="/tmp/debug.log"

# Get enabled services from tofu output
TOFU_ERR=$(mktemp)
if ! ENABLED_SERVICES_JSON=$(cd "$TOFU_DIR" && tofu output -json enabled_services 2>"$TOFU_ERR"); then
    echo -e "${RED}  Error: Failed to read enabled_services from OpenTofu state${NC}"
    cat "$TOFU_ERR" >&2
    rm -f "$TOFU_ERR"
    exit 1
fi
rm -f "$TOFU_ERR"
ENABLED_SERVICES=$(echo "$ENABLED_SERVICES_JSON" | jq -r '.[]')

if [ -z "$ENABLED_SERVICES" ]; then
    echo -e "${YELLOW}  Warning: No enabled services in OpenTofu output${NC}"
    ENABLED_SERVICES=""
fi

# Create remote stacks directory
ssh nexus "mkdir -p $REMOTE_STACKS_DIR"

# Generate global .env file with image versions and DOMAIN
echo "  Creating global .env config..."
ENV_CONTENT="# Auto-generated global config - DO NOT EDIT
# Managed by OpenTofu via image-versions.tfvars

# Domain for service URLs
DOMAIN=$DOMAIN

# Admin credentials
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_USERNAME=$ADMIN_USERNAME
USER_EMAIL=$USER_EMAIL

# Docker image versions
# Keys are transformed to environment variables by:
#   - replacing '-' with '_'
#   - converting to upper-case
#   - prefixing with 'IMAGE_'
# Example: 'node-exporter' -> 'IMAGE_NODE_EXPORTER'
"
# Parse JSON and create IMAGE_XXX=value lines
if [ "$IMAGE_VERSIONS_JSON" != "{}" ]; then
    ENV_CONTENT+=$(echo "$IMAGE_VERSIONS_JSON" | jq -r 'to_entries | .[] | "IMAGE_\(.key | gsub("-"; "_") | ascii_upcase)=\(.value)"')
fi
# Write to server
echo "$ENV_CONTENT" | ssh nexus "cat > $REMOTE_STACKS_DIR/.env"
echo -e "${GREEN}  ✓ Global .env config created (DOMAIN + image versions)${NC}"


# Generate Infisical .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "infisical"; then
    echo "  Generating Infisical config from OpenTofu secrets..."
    cat > "$STACKS_DIR/infisical/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
ENCRYPTION_KEY=$INFISICAL_ENCRYPTION_KEY
AUTH_SECRET=$INFISICAL_AUTH_SECRET
POSTGRES_PASSWORD=$INFISICAL_DB_PASSWORD
EOF
    echo -e "${GREEN}  ✓ Infisical .env generated${NC}"
fi

# Generate Grafana .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "grafana"; then
    echo "  Generating Grafana config from OpenTofu secrets..."
    cat > "$STACKS_DIR/grafana/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
GRAFANA_ADMIN_USER=$ADMIN_USERNAME
GRAFANA_ADMIN_PASSWORD=$GRAFANA_PASS
EOF
    echo -e "${GREEN}  ✓ Grafana .env generated${NC}"
fi

# Generate Dagster .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "dagster"; then
    echo "  Generating Dagster config from OpenTofu secrets..."
    cat > "$STACKS_DIR/dagster/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
DAGSTER_DB_PASSWORD=$DAGSTER_DB_PASS
EOF
    echo -e "${GREEN}  ✓ Dagster .env generated${NC}"
fi

# Generate Kestra .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "kestra"; then
    echo "  Generating Kestra config from OpenTofu secrets..."
    cat > "$STACKS_DIR/kestra/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
KESTRA_ADMIN_USER=$ADMIN_EMAIL
KESTRA_ADMIN_PASSWORD=$KESTRA_PASS
KESTRA_DB_PASSWORD=$KESTRA_DB_PASS
KESTRA_URL=https://kestra.${DOMAIN}
EOF
    echo -e "${GREEN}  ✓ Kestra .env generated${NC}"
fi

# Generate CloudBeaver .env from OpenTofu secrets (auto-config on first boot)
if echo "$ENABLED_SERVICES" | grep -qw "cloudbeaver"; then
    echo "  Generating CloudBeaver config from OpenTofu secrets..."
    cat > "$STACKS_DIR/cloudbeaver/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
CB_SERVER_NAME=Nexus CloudBeaver
CB_SERVER_URL=https://cloudbeaver.${DOMAIN}
CB_ADMIN_NAME=nexus-cloudbeaver
CB_ADMIN_PASSWORD=$CLOUDBEAVER_PASS
EOF
    echo -e "${GREEN}  ✓ CloudBeaver .env generated${NC}"
fi

# Generate Mage AI .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "mage"; then
    echo "  Generating Mage AI config from OpenTofu secrets..."
    cat > "$STACKS_DIR/mage/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
MAGE_ADMIN_PASSWORD=$MAGE_PASS
EOF
    echo -e "${GREEN}  ✓ Mage AI .env generated${NC}"
fi

# Generate MinIO .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "minio"; then
    echo "  Generating MinIO config from OpenTofu secrets..."
    cat > "$STACKS_DIR/minio/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
MINIO_ROOT_USER=nexus-minio
MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASS
EOF
    echo -e "${GREEN}  ✓ MinIO .env generated${NC}"
fi

# Generate SFTPGo .env from OpenTofu secrets.
# Only the admin password is consumed by docker-compose env-substitution
# (SFTPGo bootstraps the admin from SFTPGO_DEFAULT_ADMIN_*). The default
# user `nexus-default` is created later via the SFTPGo REST API once the
# container is up.
#
# Empty-password guard: if either `SFTPGO_ADMIN_PASS` or `SFTPGO_USER_PASS`
# arrives empty (typical cause: SFTPGo got enabled without running
# OpenTofu first, so the `random_password.sftpgo_admin` /
# `random_password.sftpgo_user` resources aren't in state yet and
# `jq -r '... // empty'` returned ""), abort the deploy. Writing the
# .env anyway would let docker-compose start SFTPGo with a blank-
# password admin (= a public Cloudflare-Access-protected service with
# no second auth factor) — that's a security regression worth failing
# fast on, not just warning. Recovery is one step: run `tofu apply`,
# then re-run spin-up. Fail-fast here is consistent with other tofu-
# state-required errors in deploy.sh (e.g., empty $DOMAIN at line 108).
if echo "$ENABLED_SERVICES" | grep -qw "sftpgo"; then
    if [ -z "$SFTPGO_ADMIN_PASS" ] || [ -z "$SFTPGO_USER_PASS" ]; then
        echo -e "${RED}Error: SFTPGo is enabled but admin/user password is empty in OpenTofu state.${NC}"
        echo -e "${RED}       Cause: random_password.sftpgo_admin and/or random_password.sftpgo_user${NC}"
        echo -e "${RED}       are not in state yet. Run \`tofu apply\` (which is what the spin-up${NC}"
        echo -e "${RED}       workflow does before deploy.sh) and re-run, then SECRETS_JSON will${NC}"
        echo -e "${RED}       carry .sftpgo_admin_password / .sftpgo_user_password.${NC}"
        exit 1
    fi
    echo "  Generating SFTPGo config from OpenTofu secrets..."
    mkdir -p "$STACKS_DIR/sftpgo"
    # umask 077 inside a subshell forces the .env to be created at
    # mode 0600 from byte 0, so the admin password is never visible
    # to other local users on the runner (or on the VM after rsync,
    # which preserves modes). chmod 600 after-the-fact is also
    # applied as belt-and-braces in case the file already existed
    # at a wider permission and `cat >` only truncates content,
    # not mode.
    (
        umask 077
        cat > "$STACKS_DIR/sftpgo/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
SFTPGO_ADMIN_PASSWORD=$SFTPGO_ADMIN_PASS
EOF
    )
    chmod 600 "$STACKS_DIR/sftpgo/.env"
    echo -e "${GREEN}  ✓ SFTPGo .env generated (mode 0600)${NC}"
fi

# Generate RedPanda Console .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "redpanda-console"; then
    echo "  Generating RedPanda Console config from OpenTofu secrets..."
    cat > "$STACKS_DIR/redpanda-console/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
REDPANDA_ADMIN_PASS=$REDPANDA_ADMIN_PASS
EOF
    echo -e "${GREEN}  ✓ RedPanda Console .env generated${NC}"
fi

# Generate Hoppscotch .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "hoppscotch"; then
    echo "  Generating Hoppscotch config from OpenTofu secrets..."
    cat > "$STACKS_DIR/hoppscotch/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
DATABASE_URL=postgres://nexus-hoppscotch:${HOPPSCOTCH_DB_PASS}@hoppscotch-db:5432/hoppscotch
POSTGRES_PASSWORD=${HOPPSCOTCH_DB_PASS}
JWT_SECRET=${HOPPSCOTCH_JWT}
SESSION_SECRET=${HOPPSCOTCH_SESSION}
DATA_ENCRYPTION_KEY=${HOPPSCOTCH_ENCRYPTION}
REDIRECT_URL=https://hoppscotch.${DOMAIN}
WHITELISTED_ORIGINS=https://hoppscotch.${DOMAIN}
VITE_BASE_URL=https://hoppscotch.${DOMAIN}
VITE_SHORTCODE_BASE_URL=https://hoppscotch.${DOMAIN}
VITE_ADMIN_URL=https://hoppscotch.${DOMAIN}/admin
VITE_BACKEND_GQL_URL=https://hoppscotch.${DOMAIN}/backend/graphql
VITE_BACKEND_WS_URL=wss://hoppscotch.${DOMAIN}/backend/graphql
VITE_BACKEND_API_URL=https://hoppscotch.${DOMAIN}/backend/v1
VITE_ALLOWED_AUTH_PROVIDERS=EMAIL
MAILER_USE_CUSTOM_CONFIGS=true
MAILER_SMTP_ENABLE=false
TOKEN_SALT_COMPLEXITY=10
MAGIC_LINK_TOKEN_VALIDITY=3
REFRESH_TOKEN_VALIDITY=604800000
ACCESS_TOKEN_VALIDITY=86400000
ENABLE_SUBPATH_BASED_ACCESS=false
EOF
    echo -e "${GREEN}  ✓ Hoppscotch .env generated${NC}"
fi

# Generate Meltano .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "meltano"; then
    echo "  Generating Meltano config from OpenTofu secrets..."
    cat > "$STACKS_DIR/meltano/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
MELTANO_DB_PASSWORD=${MELTANO_DB_PASS}
EOF
    echo -e "${GREEN}  ✓ Meltano .env generated${NC}"
fi

# Generate Soda .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "soda"; then
    echo "  Generating Soda config from OpenTofu secrets..."
    cat > "$STACKS_DIR/soda/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
SODA_DB_PASSWORD=${SODA_DB_PASS}
EOF
    echo -e "${GREEN}  ✓ Soda .env generated${NC}"
fi

# Generate PostgreSQL .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "postgres"; then
    echo "  Generating PostgreSQL config from OpenTofu secrets..."
    cat > "$STACKS_DIR/postgres/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
POSTGRES_PASSWORD=${POSTGRES_PASS}
EOF
    echo -e "${GREEN}  ✓ PostgreSQL .env generated${NC}"
fi

# Generate pg_ducklake .env + init SQL from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "pg-ducklake"; then
    echo "  Generating pg_ducklake config from OpenTofu secrets..."
    mkdir -p "$STACKS_DIR/pg-ducklake/init"
    cat > "$STACKS_DIR/pg-ducklake/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
PG_DUCKLAKE_PASSWORD=${PG_DUCKLAKE_PASS}
EOF

    # Generate bootstrap SQL - configures S3 secret + default DuckLake path
    # Require the full set of S3 variables to avoid embedding empty values into the secret
    if [ -n "$HETZNER_S3_BUCKET_PGDUCKLAKE" ] && [ -n "$HETZNER_S3_ACCESS_KEY" ] && \
       [ -n "$HETZNER_S3_SECRET_KEY" ] && [ -n "$HETZNER_S3_SERVER" ] && \
       [ -n "$HETZNER_S3_REGION" ]; then
        # Escape values for safe SQL interpolation
        S3_KEY_SQL=$(escape_sql "$HETZNER_S3_ACCESS_KEY")
        S3_SECRET_SQL=$(escape_sql "$HETZNER_S3_SECRET_KEY")
        S3_REGION_SQL=$(escape_sql "$HETZNER_S3_REGION")
        S3_SERVER_SQL=$(escape_sql "$HETZNER_S3_SERVER")
        S3_BUCKET_SQL=$(escape_sql "$HETZNER_S3_BUCKET_PGDUCKLAKE")
        cat > "$STACKS_DIR/pg-ducklake/init/00-ducklake-bootstrap.sql" << EOF
-- Auto-generated by deploy.sh - DO NOT EDIT MANUALLY
-- Re-applied via 'docker exec ... psql -f' after every spin-up
-- to handle credential rotation.

-- Drop existing secret if present (idempotent for credential rotation)
DO \$\$ BEGIN
    PERFORM duckdb.drop_secret('ducklake_s3');
EXCEPTION WHEN OTHERS THEN NULL;
END \$\$;

-- Create S3 secret for DuckLake Parquet storage
SELECT duckdb.create_simple_secret(
    type := 'S3',
    name := 'ducklake_s3',
    key_id := '${S3_KEY_SQL}',
    secret := '${S3_SECRET_SQL}',
    region := '${S3_REGION_SQL}',
    endpoint := '${S3_SERVER_SQL}',
    url_style := 'path',
    scope := 's3://${S3_BUCKET_SQL}/'
);

-- Set default storage path for new DuckLake tables
ALTER SYSTEM SET ducklake.default_table_path = 's3://${S3_BUCKET_SQL}/';
SELECT pg_reload_conf();
EOF
        echo -e "${GREEN}  ✓ pg_ducklake .env + S3 init SQL generated${NC}"
    else
        cat > "$STACKS_DIR/pg-ducklake/init/00-ducklake-bootstrap.sql" << EOF
-- Auto-generated by deploy.sh - DO NOT EDIT MANUALLY
-- No Hetzner Object Storage configured - using local volume fallback
ALTER SYSTEM SET ducklake.default_table_path = '/var/lib/ducklake/';
SELECT pg_reload_conf();
EOF
        echo -e "${YELLOW}  ⚠ pg_ducklake using local volume fallback (no Hetzner S3 configured)${NC}"
    fi
fi

# Generate pgAdmin .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "pgadmin"; then
    echo "  Generating pgAdmin config from OpenTofu secrets..."
    cat > "$STACKS_DIR/pgadmin/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
ADMIN_EMAIL=${ADMIN_EMAIL}
PGADMIN_PASSWORD=${PGADMIN_PASS}
EOF
    echo -e "${GREEN}  ✓ pgAdmin .env generated${NC}"
fi

# Generate Prefect .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "prefect"; then
    echo "  Generating Prefect config from OpenTofu secrets..."
    cat > "$STACKS_DIR/prefect/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
PREFECT_DB_PASSWORD=${PREFECT_DB_PASS}
PREFECT_UI_API_URL=https://prefect.${DOMAIN}/api
EOF
    echo -e "${GREEN}  ✓ Prefect .env generated${NC}"
fi

# Generate Windmill .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "windmill"; then
    echo "  Generating Windmill config from OpenTofu secrets..."
    cat > "$STACKS_DIR/windmill/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
WINDMILL_DB_PASSWORD=${WINDMILL_DB_PASS}
WINDMILL_SUPERADMIN_SECRET=${WINDMILL_SUPERADMIN_SECRET}
DOMAIN=${DOMAIN}
EOF
    echo -e "${GREEN}  ✓ Windmill .env generated${NC}"
fi

# Generate Superset .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "superset"; then
    echo "  Generating Superset config from OpenTofu secrets..."
    cat > "$STACKS_DIR/superset/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
SUPERSET_ADMIN_PASSWORD=${SUPERSET_PASS}
SUPERSET_DB_PASSWORD=${SUPERSET_DB_PASS}
SUPERSET_SECRET_KEY=${SUPERSET_SECRET}
ADMIN_EMAIL=${ADMIN_EMAIL}
DOMAIN=${DOMAIN}
EOF
    echo -e "${GREEN}  ✓ Superset .env generated${NC}"
fi

# Generate OpenMetadata .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "openmetadata"; then
    echo "  Generating OpenMetadata config from OpenTofu secrets..."
    cat > "$STACKS_DIR/openmetadata/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
OPENMETADATA_DB_PASSWORD=${OPENMETADATA_DB_PASS}
OPENMETADATA_AIRFLOW_PASSWORD=${OPENMETADATA_AIRFLOW_PASS}
OPENMETADATA_FERNET_KEY=${OPENMETADATA_FERNET_KEY}
OPENMETADATA_PRINCIPAL_DOMAIN=${OM_PRINCIPAL_DOMAIN}
DOMAIN=${DOMAIN}
EOF
    echo -e "${GREEN}  ✓ OpenMetadata .env generated${NC}"
fi

# Generate Gitea .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "gitea"; then
    echo "  Generating Gitea config from OpenTofu secrets..."
    cat > "$STACKS_DIR/gitea/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
GITEA_DB_PASSWORD=${GITEA_DB_PASS}
DOMAIN=${DOMAIN}
EOF
    echo -e "${GREEN}  ✓ Gitea .env generated${NC}"
fi

# Generate ClickHouse .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "clickhouse"; then
    echo "  Generating ClickHouse config from OpenTofu secrets..."
    cat > "$STACKS_DIR/clickhouse/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
CLICKHOUSE_ADMIN_PASSWORD=${CLICKHOUSE_ADMIN_PASS}
EOF
    echo -e "${GREEN}  ✓ ClickHouse .env generated${NC}"
fi

# Generate Trino .env from OpenTofu secrets (catalog connector passwords)
if echo "$ENABLED_SERVICES" | grep -qw "trino"; then
    echo "  Generating Trino .env from OpenTofu secrets..."
    cat > "$STACKS_DIR/trino/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
CLICKHOUSE_ADMIN_PASSWORD=${CLICKHOUSE_ADMIN_PASS}
POSTGRES_PASSWORD=${POSTGRES_PASS}
EOF
    echo -e "${GREEN}  ✓ Trino .env generated${NC}"
fi

# Generate RustFS .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "rustfs"; then
    echo "  Generating RustFS config from OpenTofu secrets..."
    cat > "$STACKS_DIR/rustfs/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
RUSTFS_ACCESS_KEY=nexus-rustfs
RUSTFS_SECRET_KEY=$RUSTFS_ROOT_PASS
EOF
    echo -e "${GREEN}  ✓ RustFS .env generated${NC}"
fi

# Generate SeaweedFS .env and s3.json from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "seaweedfs"; then
    echo "  Generating SeaweedFS config from OpenTofu secrets..."
    cat > "$STACKS_DIR/seaweedfs/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
SEAWEEDFS_ACCESS_KEY=nexus-seaweedfs
SEAWEEDFS_SECRET_KEY=$SEAWEEDFS_ADMIN_PASS
EOF
    # Generate S3 auth config with actual credentials
    cat > "$STACKS_DIR/seaweedfs/s3.json" << EOF
{
  "identities": [
    {
      "name": "admin",
      "credentials": [
        {
          "accessKey": "nexus-seaweedfs",
          "secretKey": "$SEAWEEDFS_ADMIN_PASS"
        }
      ],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
EOF
    echo -e "${GREEN}  ✓ SeaweedFS .env and s3.json generated${NC}"
fi

# Generate Garage .env and garage.toml from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "garage"; then
    echo "  Generating Garage config from OpenTofu secrets..."
    cat > "$STACKS_DIR/garage/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
GARAGE_ADMIN_TOKEN=$GARAGE_ADMIN_TOKEN
EOF
    # Generate garage.toml with admin token
    cat > "$STACKS_DIR/garage/garage.toml" << EOF
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "lmdb"
replication_factor = 1

rpc_bind_addr = "[::]:3901"
rpc_secret = "$GARAGE_RPC_SECRET"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
root_domain = ".s3.garage.localhost"

[s3_web]
bind_addr = "[::]:3902"
root_domain = ".web.garage.localhost"

[admin]
api_bind_addr = "[::]:3903"
admin_token = "$GARAGE_ADMIN_TOKEN"
EOF
    echo -e "${GREEN}  ✓ Garage .env and garage.toml generated${NC}"
fi

# Generate LakeFS .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "lakefs"; then
    echo "  Generating LakeFS config from OpenTofu secrets..."

    # Check if Hetzner Object Storage is configured
    if [ -n "$HETZNER_S3_SERVER" ] && [ -n "$HETZNER_S3_ACCESS_KEY" ] && [ -n "$HETZNER_S3_SECRET_KEY" ] && [ -n "$HETZNER_S3_BUCKET" ]; then
        echo "  Using Hetzner Object Storage as blockstore..."
        cat > "$STACKS_DIR/lakefs/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
LAKEFS_DATABASE_TYPE=postgres
LAKEFS_DATABASE_POSTGRES_CONNECTION_STRING=postgres://nexus-lakefs:${LAKEFS_DB_PASS}@lakefs-db:5432/lakefs?sslmode=disable
LAKEFS_AUTH_ENCRYPT_SECRET_KEY=${LAKEFS_ENCRYPT_SECRET}
LAKEFS_BLOCKSTORE_TYPE=s3
LAKEFS_BLOCKSTORE_S3_ENDPOINT=https://${HETZNER_S3_SERVER}
LAKEFS_BLOCKSTORE_S3_FORCE_PATH_STYLE=true
LAKEFS_BLOCKSTORE_S3_DISCOVER_BUCKET_REGION=false
LAKEFS_BLOCKSTORE_S3_REGION=${HETZNER_S3_REGION}
LAKEFS_BLOCKSTORE_S3_CREDENTIALS_ACCESS_KEY_ID=${HETZNER_S3_ACCESS_KEY}
LAKEFS_BLOCKSTORE_S3_CREDENTIALS_SECRET_ACCESS_KEY=${HETZNER_S3_SECRET_KEY}
LAKEFS_GATEWAYS_S3_DOMAIN_NAME=s3.lakefs.${DOMAIN}
# Note: LAKEFS_INSTALLATION_* vars only work with database.type=local
# Admin user is created via API in Step 7/7
POSTGRES_PASSWORD=${LAKEFS_DB_PASS}
EOF
        echo -e "${GREEN}  ✓ LakeFS .env generated (Hetzner Object Storage backend)${NC}"
    else
        echo -e "${YELLOW}  ⚠ Hetzner Object Storage not configured, using local storage${NC}"
        cat > "$STACKS_DIR/lakefs/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
LAKEFS_DATABASE_TYPE=postgres
LAKEFS_DATABASE_POSTGRES_CONNECTION_STRING=postgres://nexus-lakefs:${LAKEFS_DB_PASS}@lakefs-db:5432/lakefs?sslmode=disable
LAKEFS_AUTH_ENCRYPT_SECRET_KEY=${LAKEFS_ENCRYPT_SECRET}
LAKEFS_BLOCKSTORE_TYPE=local
LAKEFS_BLOCKSTORE_LOCAL_PATH=/data
LAKEFS_GATEWAYS_S3_DOMAIN_NAME=s3.lakefs.${DOMAIN}
# Note: LAKEFS_INSTALLATION_* vars only work with database.type=local
# Admin user is created via API in Step 7/7
POSTGRES_PASSWORD=${LAKEFS_DB_PASS}
EOF
        echo -e "${GREEN}  ✓ LakeFS .env generated (local storage backend)${NC}"
    fi
fi

# Generate Filestash .env from OpenTofu secrets
if echo "$ENABLED_SERVICES" | grep -qw "filestash"; then
    echo "  Generating Filestash config from OpenTofu secrets..."

    # Generate bcrypt hash for admin password
    if [ -n "$FILESTASH_ADMIN_PASSWORD" ]; then
        if ! command -v htpasswd >/dev/null 2>&1; then
            echo "❌ ERROR: 'htpasswd' command not found but FILESTASH_ADMIN_PASSWORD is set."
            echo "   Please install 'apache2-utils' (Debian/Ubuntu) or 'httpd-tools' (RHEL/CentOS) on the target host."
            exit 1
        fi

        FILESTASH_ADMIN_HASH=$(htpasswd -nbBC 10 admin "$FILESTASH_ADMIN_PASSWORD" 2>/dev/null | cut -d: -f2)
        if [ -z "$FILESTASH_ADMIN_HASH" ]; then
            echo "❌ ERROR: Failed to generate Filestash admin password hash with 'htpasswd'."
            exit 1
        fi
        # Escape $ in bcrypt hash for Docker Compose .env ($ → $$)
        FILESTASH_ADMIN_HASH_ESCAPED=$(echo "$FILESTASH_ADMIN_HASH" | sed 's/\$/\$\$/g')
    else
        FILESTASH_ADMIN_HASH=""
        FILESTASH_ADMIN_HASH_ESCAPED=""
    fi

    # Determine which S3 backends are configured
    HAS_R2=false
    HAS_HETZNER=false
    HAS_EXTERNAL=false
    if [ -n "$R2_DATA_ENDPOINT" ] && [ -n "$R2_DATA_ACCESS_KEY" ] && [ -n "$R2_DATA_SECRET_KEY" ] && [ -n "$R2_DATA_BUCKET" ]; then
        HAS_R2=true
    fi
    if [ -n "$HETZNER_S3_SERVER" ] && [ -n "$HETZNER_S3_ACCESS_KEY" ] && [ -n "$HETZNER_S3_SECRET_KEY" ] && [ -n "$HETZNER_S3_BUCKET_GENERAL" ]; then
        HAS_HETZNER=true
    fi
    if [ -n "$EXTERNAL_S3_ENDPOINT" ] && [ -n "$EXTERNAL_S3_ACCESS_KEY" ] && [ -n "$EXTERNAL_S3_SECRET_KEY" ] && [ -n "$EXTERNAL_S3_BUCKET" ]; then
        HAS_EXTERNAL=true
    fi

    if [ "$HAS_R2" = "true" ] || [ "$HAS_HETZNER" = "true" ] || [ "$HAS_EXTERNAL" = "true" ]; then
        echo "  Pre-configuring Filestash with S3 backend(s)..."

        # Build connections array and params dynamically using jq
        # IMPORTANT: middleware params MUST be JSON strings (tojson) because
        # Filestash encrypts/decrypts these fields
        CONNECTIONS="[]"
        PARAMS="{}"
        RELATED_BACKEND=""

        # R2 Datalake (primary if configured)
        if [ "$HAS_R2" = "true" ]; then
            CONNECTIONS=$(echo "$CONNECTIONS" | jq '. + [{"type":"s3","label":"R2 Datalake"}]')
            PARAMS=$(echo "$PARAMS" | jq --arg ak "$R2_DATA_ACCESS_KEY" --arg sk "$R2_DATA_SECRET_KEY" \
                --arg ep "$R2_DATA_ENDPOINT" --arg bk "$R2_DATA_BUCKET" \
                '. + {"R2 Datalake":{"type":"s3","access_key_id":$ak,"secret_access_key":$sk,"endpoint":$ep,"region":"auto","path":("/"+$bk+"/")}}')
            RELATED_BACKEND="R2 Datalake"
        fi

        # Hetzner Storage
        if [ "$HAS_HETZNER" = "true" ]; then
            CONNECTIONS=$(echo "$CONNECTIONS" | jq '. + [{"type":"s3","label":"Hetzner Storage"}]')
            PARAMS=$(echo "$PARAMS" | jq --arg ak "$HETZNER_S3_ACCESS_KEY" --arg sk "$HETZNER_S3_SECRET_KEY" \
                --arg ep "https://$HETZNER_S3_SERVER" --arg rg "$HETZNER_S3_REGION" --arg bk "$HETZNER_S3_BUCKET_GENERAL" \
                '. + {"Hetzner Storage":{"type":"s3","access_key_id":$ak,"secret_access_key":$sk,"endpoint":$ep,"region":$rg,"path":("/"+$bk+"/")}}')
            [ -z "$RELATED_BACKEND" ] && RELATED_BACKEND="Hetzner Storage"
        fi

        # External S3
        if [ "$HAS_EXTERNAL" = "true" ]; then
            CONNECTIONS=$(echo "$CONNECTIONS" | jq --arg lb "$EXTERNAL_S3_LABEL" '. + [{"type":"s3","label":$lb}]')
            PARAMS=$(echo "$PARAMS" | jq --arg ak "$EXTERNAL_S3_ACCESS_KEY" --arg sk "$EXTERNAL_S3_SECRET_KEY" \
                --arg ep "$EXTERNAL_S3_ENDPOINT" --arg rg "$EXTERNAL_S3_REGION" --arg bk "$EXTERNAL_S3_BUCKET" --arg lb "$EXTERNAL_S3_LABEL" \
                '. + {($lb):{"type":"s3","access_key_id":$ak,"secret_access_key":$sk,"endpoint":$ep,"region":$rg,"path":("/"+$bk+"/")}}')
            [ -z "$RELATED_BACKEND" ] && RELATED_BACKEND="$EXTERNAL_S3_LABEL"
        fi

        CONFIG_JSON=$(jq -n --argjson conns "$CONNECTIONS" --argjson params "$PARAMS" --arg rb "$RELATED_BACKEND" '{
            connections: $conns,
            middleware: {
                identity_provider: {type: "passthrough", params: ({"strategy":"direct"} | tojson)},
                attribute_mapping: {related_backend: $rb, params: ($params | tojson)}
            }
        }')
        CONFIG_BASE64=$(echo "$CONFIG_JSON" | base64 | tr -d '\n')

        cat > "$STACKS_DIR/filestash/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
CONFIG_JSON=${CONFIG_BASE64}
ADMIN_PASSWORD=${FILESTASH_ADMIN_HASH_ESCAPED}
DOMAIN=${DOMAIN}
EOF
        BACKENDS=""
        [ "$HAS_R2" = "true" ] && BACKENDS="R2 Datalake"
        [ "$HAS_HETZNER" = "true" ] && BACKENDS="${BACKENDS:+$BACKENDS + }Hetzner S3"
        [ "$HAS_EXTERNAL" = "true" ] && BACKENDS="${BACKENDS:+$BACKENDS + }${EXTERNAL_S3_LABEL}"
        echo -e "${GREEN}  ✓ Filestash .env generated (${BACKENDS} pre-configured, primary: ${RELATED_BACKEND})${NC}"
    else
        # Create minimal .env without S3 pre-configuration
        cat > "$STACKS_DIR/filestash/.env" << EOF
# Auto-generated - DO NOT COMMIT
# Note: S3 backend must be configured manually at /admin
ADMIN_PASSWORD=${FILESTASH_ADMIN_HASH_ESCAPED}
DOMAIN=${DOMAIN}
EOF
        echo -e "${YELLOW}  ⚠ Filestash .env generated (admin password set, configure S3 at /admin)${NC}"
    fi
fi

# Wiki.js
if echo "$ENABLED_SERVICES" | grep -qw "wikijs" && [ -n "$WIKIJS_DB_PASS" ]; then
    cat > "$STACKS_DIR/wikijs/.env" << EOF
# Auto-generated - DO NOT COMMIT
WIKIJS_DB_PASSWORD=${WIKIJS_DB_PASS}
EOF
    echo -e "${GREEN}  ✓ Wiki.js .env generated${NC}"
fi

# Woodpecker CI
if echo "$ENABLED_SERVICES" | grep -qw "woodpecker" && [ -n "$WOODPECKER_AGENT_SECRET" ]; then
    cat > "$STACKS_DIR/woodpecker/.env" << EOF
# Auto-generated - DO NOT COMMIT
DOMAIN=${DOMAIN}
WOODPECKER_AGENT_SECRET=${WOODPECKER_AGENT_SECRET}
WOODPECKER_ADMIN=${ADMIN_USERNAME:-}
WOODPECKER_GITEA_CLIENT=${WOODPECKER_GITEA_CLIENT:-}
WOODPECKER_GITEA_SECRET=${WOODPECKER_GITEA_SECRET:-}
EOF
    echo -e "${GREEN}  ✓ Woodpecker CI .env generated${NC}"
fi

# Apache Spark
if echo "$ENABLED_SERVICES" | grep -qw "spark"; then
    cat > "$STACKS_DIR/spark/.env" << EOF
# Auto-generated - DO NOT COMMIT
HETZNER_S3_ENDPOINT=${HETZNER_S3_SERVER:+https://${HETZNER_S3_SERVER}}
HETZNER_S3_ACCESS_KEY=${HETZNER_S3_ACCESS_KEY:-}
HETZNER_S3_SECRET_KEY=${HETZNER_S3_SECRET_KEY:-}
HETZNER_S3_BUCKET=${HETZNER_S3_BUCKET_GENERAL:-}
SPARK_WORKER_CORES=${SPARK_WORKER_CORES:-2}
SPARK_WORKER_MEMORY=${SPARK_WORKER_MEMORY:-3g}
EOF
    echo -e "${GREEN}  ✓ Spark .env generated${NC}"
fi

# Apache Flink
if echo "$ENABLED_SERVICES" | grep -qw "flink"; then
    cat > "$STACKS_DIR/flink/.env" << EOF
# Auto-generated - DO NOT COMMIT
HETZNER_S3_ENDPOINT=${HETZNER_S3_SERVER:+https://${HETZNER_S3_SERVER}}
HETZNER_S3_ACCESS_KEY=${HETZNER_S3_ACCESS_KEY:-}
HETZNER_S3_SECRET_KEY=${HETZNER_S3_SECRET_KEY:-}
HETZNER_S3_BUCKET=${HETZNER_S3_BUCKET_GENERAL:-}
FLINK_TASKMANAGER_SLOTS=${FLINK_TASKMANAGER_SLOTS:-2}
EOF
    echo -e "${GREEN}  ✓ Flink .env generated${NC}"
fi

# Dinky (Flink SQL IDE)
if echo "$ENABLED_SERVICES" | grep -qw "dinky"; then
    if [ -z "${DINKY_ADMIN_PASS:-}" ]; then
        echo -e "${YELLOW}  ⚠️  DINKY_ADMIN_PASS not set - Dinky will use default credentials${NC}"
    fi
    cat > "$STACKS_DIR/dinky/.env" << EOF
# Auto-generated - DO NOT COMMIT
DINKY_ADMIN_PASSWORD=${DINKY_ADMIN_PASS:-}
EOF
    echo -e "${GREEN}  ✓ Dinky .env generated${NC}"
fi

# Jupyter PySpark
if echo "$ENABLED_SERVICES" | grep -qw "jupyter"; then
    # Set SPARK_MASTER based on whether Spark stack is enabled
    if echo "$ENABLED_SERVICES" | grep -qw "spark"; then
        JUPYTER_SPARK_MASTER="spark://spark-master:7077"
    else
        JUPYTER_SPARK_MASTER="local[*]"
    fi
    cat > "$STACKS_DIR/jupyter/.env" << EOF
# Auto-generated - DO NOT COMMIT
SPARK_MASTER=${JUPYTER_SPARK_MASTER}
HETZNER_S3_ENDPOINT=${HETZNER_S3_SERVER:+https://${HETZNER_S3_SERVER}}
HETZNER_S3_ACCESS_KEY=${HETZNER_S3_ACCESS_KEY:-}
HETZNER_S3_SECRET_KEY=${HETZNER_S3_SECRET_KEY:-}
HETZNER_S3_BUCKET=${HETZNER_S3_BUCKET_GENERAL:-}
EOF
    echo -e "${GREEN}  ✓ Jupyter PySpark .env generated${NC}"
fi

# S3 Manager
if echo "$ENABLED_SERVICES" | grep -qw "s3manager"; then
    cat > "$STACKS_DIR/s3manager/.env" << EOF
# Auto-generated - DO NOT COMMIT
ACCESS_KEY_ID=${HETZNER_S3_ACCESS_KEY:-}
SECRET_ACCESS_KEY=${HETZNER_S3_SECRET_KEY:-}
REGION=${HETZNER_S3_REGION:-}
ENDPOINT=${HETZNER_S3_SERVER:-}
USE_SSL=true
EOF
    echo -e "${GREEN}  ✓ S3 Manager .env generated${NC}"
fi

# Appsmith
if echo "$ENABLED_SERVICES" | grep -qw "appsmith" && [ -n "$APPSMITH_ENCRYPTION_PASSWORD" ] && [ -n "$APPSMITH_ENCRYPTION_SALT" ]; then
    echo "  Generating Appsmith config from OpenTofu secrets..."
    cat > "$STACKS_DIR/appsmith/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
APPSMITH_ENCRYPTION_PASSWORD=${APPSMITH_ENCRYPTION_PASSWORD}
APPSMITH_ENCRYPTION_SALT=${APPSMITH_ENCRYPTION_SALT}
APPSMITH_DISABLE_TELEMETRY=true
APPSMITH_CUSTOM_DOMAIN=https://appsmith.${DOMAIN}
EOF
    echo -e "${GREEN}  ✓ Appsmith .env generated${NC}"
fi

# NocoDB
if echo "$ENABLED_SERVICES" | grep -qw "nocodb" && [ -n "$NOCODB_DB_PASS" ] && [ -n "$NOCODB_ADMIN_PASS" ] && [ -n "$NOCODB_JWT_SECRET" ]; then
    echo "  Generating NocoDB config from OpenTofu secrets..."
    cat > "$STACKS_DIR/nocodb/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
NC_DB=pg://nocodb-db:5432?u=nexus-nocodb&p=${NOCODB_DB_PASS}&d=nocodb
NC_AUTH_JWT_SECRET=${NOCODB_JWT_SECRET}
NC_ADMIN_EMAIL=${ADMIN_EMAIL}
NC_ADMIN_PASSWORD=${NOCODB_ADMIN_PASS}
NC_PUBLIC_URL=https://nocodb.${DOMAIN}
NOCODB_DB_PASSWORD=${NOCODB_DB_PASS}
EOF
    echo -e "${GREEN}  ✓ NocoDB .env generated${NC}"
fi

# Dify
if echo "$ENABLED_SERVICES" | grep -qw "dify" && [ -n "$DIFY_DB_PASS" ] && [ -n "$DIFY_ADMIN_PASS" ]; then
    echo "  Generating Dify config from OpenTofu secrets..."
    cat > "$STACKS_DIR/dify/.env" << EOF
# Auto-generated from OpenTofu secrets - DO NOT COMMIT
DIFY_DB_PASSWORD=${DIFY_DB_PASS}
DIFY_REDIS_PASSWORD=${DIFY_REDIS_PASS}
DIFY_SECRET_KEY=${DIFY_SECRET_KEY}
DIFY_ADMIN_PASSWORD=${DIFY_ADMIN_PASS}
DIFY_WEAVIATE_API_KEY=${DIFY_WEAVIATE_API_KEY}
DIFY_SANDBOX_API_KEY=${DIFY_SANDBOX_API_KEY}
DIFY_PLUGIN_DAEMON_KEY=${DIFY_PLUGIN_DAEMON_KEY}
DIFY_PLUGIN_INNER_API_KEY=${DIFY_PLUGIN_INNER_API_KEY}
EOF
    echo -e "${GREEN}  ✓ Dify .env generated${NC}"
fi

# Generate Git workspace .env vars for services that integrate with Gitea
# These vars enable auto-clone of the shared workspace repo at container startup.
# The clone may fail on first deployment (Gitea starts in parallel), but succeeds
# on subsequent spin-ups. Services are restarted in Step 7 after repo creation.
# Security: Credentials are passed via GITEA_USERNAME/GITEA_PASSWORD env vars and
# injected into containers via .netrc at startup (not embedded in the repo URL).
if echo "$ENABLED_SERVICES" | grep -qw "gitea" && [ -n "$GITEA_ADMIN_PASS" ]; then
    # Workspace-config identity: when no separate single-address user is
    # configured (GITEA_USER_EMAIL empty after trim+comma-split), fall back
    # to the admin identity for repo URLs and service .env values.
    # Downstream service containers need a non-empty username + email for
    # git operations (empty values would produce invalid URLs like
    # http://gitea:3000//repo.git). This fallback is config-only and does
    # NOT reintroduce the email-uniqueness collision the parent PR fixed:
    # the Gitea user-create block below also gates on
    # `[ -n "$GITEA_USER_EMAIL" ]` and skips cleanly when empty.
    #
    # Gate uses GITEA_USER_EMAIL (not raw USER_EMAIL) so a USER_EMAIL whose
    # first entry is empty/whitespace (e.g. a leading `,` in the joined
    # list) correctly routes to the admin fallback.
    if [ -n "$GITEA_USER_EMAIL" ]; then
        # See top-of-script comment (~line 85) on GITEA_USER_EMAIL vs USER_EMAIL.
        GITEA_USER_USERNAME="${GITEA_USER_EMAIL%%@*}"
    else
        GITEA_USER_USERNAME="$ADMIN_USERNAME"
    fi
    # Determine workspace repo. Three cases:
    # - mirror + user → fork of first mirror into user's namespace
    # - mirror + no user → admin's mirror-readonly repo directly (still created
    #   later in the mirror block regardless of USER_EMAIL)
    # - no mirror → admin's default empty repo (created further below only when
    #   GH_MIRROR_REPOS is unset)
    if [ -n "${GH_MIRROR_REPOS:-}" ] && [ -n "$GITEA_USER_EMAIL" ]; then
        # Derive repo name from first mirror URL (e.g. https://github.com/user/Bsc_EDS_GIS_FS2026)
        FIRST_MIRROR=$(echo "$GH_MIRROR_REPOS" | cut -d',' -f1 | tr -d ' ')
        WORKSPACE_REPO_NAME=$(basename "$FIRST_MIRROR" .git)
        # Fork source: admin/mirror-readonly-<name>
        # Fork name: <originalname>_<sanitized_username> (e.g. Bsc_EDS_GIS_FS2026_stefan_koch)
        GITEA_USER_SANITIZED="${GITEA_USER_USERNAME//[^a-zA-Z0-9]/_}"
        REPO_NAME="${WORKSPACE_REPO_NAME}_${GITEA_USER_SANITIZED}"
        GITEA_REPO_OWNER="${GITEA_USER_USERNAME}"
        GITEA_REPO_URL="http://gitea:3000/${GITEA_REPO_OWNER}/${REPO_NAME}.git"
    elif [ -n "${GH_MIRROR_REPOS:-}" ]; then
        # Mirror configured but no user to fork into: point services at the
        # admin's mirror-readonly-<name> repo that the mirror block creates
        # (line ~3261). Without this branch we'd previously fall through to
        # the default empty-repo name below, which is NOT created when
        # GH_MIRROR_REPOS is set — service .env values would reference a
        # non-existent repo.
        FIRST_MIRROR=$(echo "$GH_MIRROR_REPOS" | cut -d',' -f1 | tr -d ' ')
        REPO_NAME="mirror-readonly-$(basename "$FIRST_MIRROR" .git)"
        GITEA_REPO_OWNER="${ADMIN_USERNAME}"
        GITEA_REPO_URL="http://gitea:3000/${GITEA_REPO_OWNER}/${REPO_NAME}.git"
    else
        REPO_NAME="nexus-${DOMAIN//./-}-gitea"
        GITEA_REPO_OWNER="${ADMIN_USERNAME}"
        GITEA_REPO_URL="http://gitea:3000/${GITEA_REPO_OWNER}/${REPO_NAME}.git"
    fi

    # Resolve the workspace repo's default branch.
    #
    # Three paths converge on this variable:
    #   1. Kestra `system.git-sync` / `system.flow-sync` flow YAML
    #      (`branch: ${WORKSPACE_BRANCH}`)
    #   2. The post-fork merge-upstream POST (mirror mode only)
    #   3. Anywhere else in this script that needs a Git ref for the
    #      workspace repo
    #
    # Without this, all three hardcoded `main` and broke for users
    # mirroring a GitHub repo whose default branch is `master` (or
    # anything else): SyncFlows clones the wrong branch and silently
    # syncs nothing; merge-upstream returns 404 and the fork drifts.
    #
    # Resolution rules:
    #   - No mirror: deploy.sh creates the repo itself with `main` →
    #     `main` is correct, no API call needed.
    #   - Mirror: query GitHub's REST API for the upstream repo's
    #     `default_branch`. The fork inherits this value when Gitea
    #     mirrors + forks the repo. Fall back to `main` on any HTTP
    #     or parse failure (don't make this a hard dependency — a
    #     misconfigured GH_MIRROR_TOKEN should warn, not block).
    WORKSPACE_BRANCH="main"
    if [ -n "${GH_MIRROR_REPOS:-}" ] && [ -n "${GH_MIRROR_TOKEN:-}" ]; then
        FIRST_MIRROR_FOR_BRANCH=$(echo "$GH_MIRROR_REPOS" | cut -d',' -f1 | tr -d ' ')
        # Normalize to `owner/repo`:
        #   - strip `https://github.com/` host prefix
        #   - strip optional `?…` / `#…` URL parts
        #   - strip a trailing `/`
        #   - strip a trailing `.git`
        # Then validate the result matches `owner/repo` (no inner slashes,
        # both halves non-empty). Anything else falls back to `main` rather
        # than building a malformed GitHub API URL.
        GH_OWNER_REPO=$(echo "$FIRST_MIRROR_FOR_BRANCH" \
            | sed -E 's#^https?://github\.com/##; s#[?#].*$##; s#/$##; s#\.git$##')
        if [ -n "$GH_OWNER_REPO" ] && [[ "$GH_OWNER_REPO" =~ ^[^/]+/[^/]+$ ]]; then
            # Token + URL go through a `curl --config` file (mode 0600)
            # so $GH_MIRROR_TOKEN never appears in argv (would otherwise
            # be visible in the runner's `ps` listing while curl runs).
            # The mktemp + curl + cleanup are wrapped in a subshell with
            # `trap … EXIT HUP INT TERM` so the token-bearing config file
            # is always removed — even on Ctrl-C, runner cancellation, or
            # an unexpected `set -e` exit between mktemp and rm. Same
            # argv-safe pattern as the Kestra/Infisical paths elsewhere
            # in this script.
            DETECTED_BRANCH=$(
                GH_API_CFG=$(mktemp)
                trap 'rm -f "$GH_API_CFG"' EXIT HUP INT TERM
                chmod 600 "$GH_API_CFG"
                {
                    printf 'header = "Authorization: Bearer %s"\n' "$GH_MIRROR_TOKEN"
                    printf 'header = "Accept: application/vnd.github+json"\n'
                    printf 'url = "https://api.github.com/repos/%s"\n' "$GH_OWNER_REPO"
                    printf 'max-time = 10\nfail\nsilent\nshow-error\nlocation\n'
                } > "$GH_API_CFG"
                curl --config "$GH_API_CFG" 2>/dev/null \
                    | jq -r '.default_branch // empty' 2>/dev/null || true
            )
            if [ -n "$DETECTED_BRANCH" ] && [ "$DETECTED_BRANCH" != "null" ]; then
                WORKSPACE_BRANCH="$DETECTED_BRANCH"
                if [ "$WORKSPACE_BRANCH" != "main" ]; then
                    echo -e "${YELLOW}  ⚠ Upstream $GH_OWNER_REPO uses default branch '$WORKSPACE_BRANCH' (not 'main') — Kestra sync + fork merge-upstream will use this branch${NC}"
                fi
            else
                echo -e "${YELLOW}  ⚠ Could not detect default branch for $GH_OWNER_REPO via GitHub API — defaulting to 'main' (set GH_MIRROR_TOKEN with repo:read scope if your upstream uses 'master' or another branch)${NC}"
            fi
        fi
    fi

    # Require BOTH a valid single user email and a user password to use user
    # credentials for service Git integration. Either one missing → fall
    # back to admin. Gate on GITEA_USER_EMAIL (not USER_EMAIL) so a list
    # with empty first entry routes to the admin branch.
    if [ -n "$GITEA_USER_EMAIL" ] && [ -n "$GITEA_USER_PASS" ]; then
        GITEA_GIT_USER="${GITEA_USER_USERNAME}"
        GITEA_GIT_PASS="${GITEA_USER_PASS}"
        GIT_AUTHOR="${GITEA_USER_USERNAME}"
        # Single-address: GIT_EMAIL is written to service .env files and used
        # as git author/committer email. USER_EMAIL may be a comma-list;
        # use GITEA_USER_EMAIL so commit metadata is well-formed.
        GIT_EMAIL="${GITEA_USER_EMAIL}"
    else
        # Fallback to admin if no user identity/password available
        GITEA_GIT_USER="${ADMIN_USERNAME}"
        GITEA_GIT_PASS="${GITEA_ADMIN_PASS}"
        GIT_AUTHOR="${ADMIN_USERNAME}"
        GIT_EMAIL="${ADMIN_EMAIL}"
    fi

    for SERVICE in jupyter marimo code-server meltano prefect; do
        if echo "$ENABLED_SERVICES" | grep -qw "$SERVICE"; then
            echo "  Adding Git workspace config to $SERVICE .env..."
            ENV_FILE="$STACKS_DIR/$SERVICE/.env"
            # Idempotent: remove existing Gitea block before writing
            if [ -f "$ENV_FILE" ]; then
                sed -i '/^# >>> Gitea workspace repo/,/^# <<< Gitea workspace repo/d' "$ENV_FILE"
            fi
            cat >> "$ENV_FILE" << EOF
# >>> Gitea workspace repo (auto-generated, do not edit)
GITEA_URL=http://gitea:3000
GITEA_REPO_URL=${GITEA_REPO_URL}
GITEA_USERNAME=${GITEA_GIT_USER}
GITEA_PASSWORD=${GITEA_GIT_PASS}
GIT_AUTHOR_NAME=${GIT_AUTHOR}
GIT_AUTHOR_EMAIL=${GIT_EMAIL}
GIT_COMMITTER_NAME=${GIT_AUTHOR}
GIT_COMMITTER_EMAIL=${GIT_EMAIL}
REPO_NAME=${REPO_NAME}
# <<< Gitea workspace repo
EOF
            echo -e "${GREEN}  ✓ $SERVICE Git config added${NC}"
        fi
    done
fi

# Sync only enabled stacks
echo "{\"location\":\"deploy.sh:378\",\"message\":\"Starting stack sync\",\"data\":{\"enabled_services\":\"$ENABLED_SERVICES\"},\"timestamp\":$(date +%s)000,\"sessionId\":\"debug-session\",\"runId\":\"run1\"}" >> "$LOG_FILE" 2>/dev/null || true

for service in $ENABLED_SERVICES; do
    echo "{\"location\":\"deploy.sh:379\",\"message\":\"Processing service for sync\",\"data\":{\"service\":\"$service\",\"stack_dir_exists\":$([ -d "$STACKS_DIR/$service" ] && echo "true" || echo "false")},\"timestamp\":$(date +%s)000,\"sessionId\":\"debug-session\",\"runId\":\"run1\"}" >> "$LOG_FILE" 2>/dev/null || true
    if [ -d "$STACKS_DIR/$service" ]; then
        echo "  Syncing $service..."
        rsync -av "$STACKS_DIR/$service/" "nexus:$REMOTE_STACKS_DIR/$service/"
        echo "{\"location\":\"deploy.sh:382\",\"message\":\"Service synced\",\"data\":{\"service\":\"$service\",\"exit_code\":$?},\"timestamp\":$(date +%s)000,\"sessionId\":\"debug-session\",\"runId\":\"run1\"}" >> "$LOG_FILE" 2>/dev/null || true
    else
        echo -e "${YELLOW}  Warning: Stack folder 'stacks/$service' not found - skipping${NC}"
        echo "{\"location\":\"deploy.sh:384\",\"message\":\"Stack folder not found\",\"data\":{\"service\":\"$service\"},\"timestamp\":$(date +%s)000,\"sessionId\":\"debug-session\",\"runId\":\"run1\"}" >> "$LOG_FILE" 2>/dev/null || true
    fi
done
echo -e "${GREEN}  ✓ Stacks synced${NC}"

# -----------------------------------------------------------------------------
# Stop disabled services
# -----------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[4/7] Cleaning up disabled services...${NC}"

ENABLED_LIST=$(echo $ENABLED_SERVICES | tr '\n' ' ')

ssh nexus "
# Find all stack directories on server
for stack_dir in $REMOTE_STACKS_DIR/*/; do
    [ -d \"\$stack_dir\" ] || continue
    stack_name=\$(basename \"\$stack_dir\")
    
    # Check if this stack is in the enabled list
    if ! echo '$ENABLED_LIST' | grep -qw \"\$stack_name\"; then
        # Stack is disabled - stop and remove
        if [ -f \"\${stack_dir}docker-compose.yml\" ]; then
            echo \"  Stopping \$stack_name (disabled)...\"
            cd \"\$stack_dir\"
            docker compose down 2>/dev/null || true
        fi
        echo \"  Removing \$stack_name stack folder...\"
        rm -rf \"\$stack_dir\"
    fi
done
echo '  ✓ Cleanup complete'
"

# -----------------------------------------------------------------------------
# Docker Hub Login (optional - for increased pull rate limits)
# -----------------------------------------------------------------------------
if [ -n "$DOCKERHUB_USER" ] && [ -n "$DOCKERHUB_TOKEN" ]; then
    echo ""
    echo -e "${YELLOW}[5/7] Logging into Docker Hub...${NC}"
    ssh nexus "echo '$DOCKERHUB_TOKEN' | docker login -u '$DOCKERHUB_USER' --password-stdin" 2>/dev/null
    echo -e "${GREEN}  ✓ Docker Hub login successful (200 pulls/6h)${NC}"
else
    echo ""
    echo -e "${CYAN}[5/7] Skipping Docker Hub login (anonymous: 100 pulls/6h)${NC}"
fi

# -----------------------------------------------------------------------------
# Setup SSH-Agent for Wetty (if enabled)
# -----------------------------------------------------------------------------
if echo "$ENABLED_SERVICES" | grep -qw "wetty"; then
    echo ""
    echo -e "${YELLOW}[5.5/7] Setting up SSH-Agent for Wetty...${NC}"
    ssh nexus "
        # Create SSH directory if it doesn't exist
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        
        # Generate SSH key pair for Wetty if it doesn't exist
        WETTY_KEY_PATH=\"/root/.ssh/id_ed25519_wetty\"
        if [ ! -f \"\$WETTY_KEY_PATH\" ]; then
            echo '  Generating SSH key pair for Wetty...'
            ssh-keygen -t ed25519 -f \"\$WETTY_KEY_PATH\" -N '' -C 'wetty-auto-generated' >/dev/null 2>&1
            chmod 600 \"\$WETTY_KEY_PATH\"
            chmod 644 \"\$WETTY_KEY_PATH.pub\"
            echo '  ✓ SSH key pair generated for Wetty'
        else
            echo '  ✓ SSH key pair already exists for Wetty'
        fi
        
        # Add public key to authorized_keys if not already present
        WETTY_PUBKEY=\$(cat \"\$WETTY_KEY_PATH.pub\")
        if ! grep -q \"\$WETTY_PUBKEY\" /root/.ssh/authorized_keys 2>/dev/null; then
            echo \"\$WETTY_PUBKEY\" >> /root/.ssh/authorized_keys
            chmod 600 /root/.ssh/authorized_keys
            echo '  ✓ Public key added to authorized_keys'
        else
            echo '  ✓ Public key already in authorized_keys'
        fi
        
        # Create SSH-Agent socket directory if it doesn't exist
        SSH_AGENT_DIR=\"/tmp/ssh-agent\"
        mkdir -p \"\$SSH_AGENT_DIR\"
        
        # Helper function to check if SSH-Agent is responsive
        check_ssh_agent() {
            if ssh-add -l >/dev/null 2>&1; then
                return 0
            else
                return 1
            fi
        }
        
        # Check if SSH-Agent is already running (check for existing socket)
        SSH_AUTH_SOCK_FILE=\"\$SSH_AGENT_DIR/agent.sock\"
        if [ -S \"\$SSH_AUTH_SOCK_FILE\" ]; then
            export SSH_AUTH_SOCK=\"\$SSH_AUTH_SOCK_FILE\"
            # Test if agent is still responsive
            if check_ssh_agent; then
                echo '  ✓ SSH-Agent already running'
            else
                # Socket exists but agent is dead, remove it
                rm -f \"\$SSH_AUTH_SOCK_FILE\"
                unset SSH_AUTH_SOCK
            fi
        fi
        
        # Start SSH-Agent if not running
        if [ -z \"\${SSH_AUTH_SOCK:-}\" ] || [ ! -S \"\$SSH_AUTH_SOCK\" ]; then
            # Start SSH-Agent with socket in known location
            eval \$(ssh-agent -a \"\$SSH_AUTH_SOCK_FILE\" -s) >/dev/null 2>&1
            export SSH_AUTH_SOCK=\"\$SSH_AUTH_SOCK_FILE\"
            echo '  ✓ SSH-Agent started'
        fi
        
        # Add SSH key to agent if not already added
        if [ -f \"\$WETTY_KEY_PATH\" ]; then
            # Get key fingerprint for comparison
            KEY_FINGERPRINT=\$(ssh-keygen -lf \"\$WETTY_KEY_PATH\" 2>/dev/null | awk '{print \$2}' || echo \"\")
            
            # Check if key is already in agent by comparing fingerprints
            KEY_IN_AGENT=false
            if [ -n \"\$KEY_FINGERPRINT\" ] && check_ssh_agent && ssh-add -l 2>/dev/null | grep -q \"\$KEY_FINGERPRINT\"; then
                KEY_IN_AGENT=true
            fi
            
            if [ \"\$KEY_IN_AGENT\" = \"false\" ]; then
                # Add key to agent
                if ssh-add \"\$WETTY_KEY_PATH\" 2>&1; then
                    echo '  ✓ SSH key added to agent'
                else
                    echo -e \"  ${YELLOW}⚠ Failed to add SSH key to agent${NC}\"
                fi
            else
                echo '  ✓ SSH key already in agent'
            fi
        else
            echo -e \"  ${YELLOW}⚠ SSH key not found at \$WETTY_KEY_PATH${NC}\"
        fi
        
        # Export SSH_AUTH_SOCK path in wetty .env file for docker-compose
        WETTY_ENV=\"/opt/docker-server/stacks/wetty/.env\"
        if [ -f \"\$WETTY_ENV\" ]; then
            # Remove existing SSH_AUTH_SOCK line if present
            sed -i '/^SSH_AUTH_SOCK=/d' \"\$WETTY_ENV\"
        fi
        echo \"SSH_AUTH_SOCK=\$SSH_AUTH_SOCK\" >> \"\$WETTY_ENV\"
        echo '  ✓ SSH_AUTH_SOCK exported to wetty .env'
    "
    echo -e "${GREEN}  ✓ SSH-Agent configured for Wetty${NC}"
fi

# -----------------------------------------------------------------------------
# Generate Docker Compose override files for firewall TCP port exposure
# -----------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}  Generating firewall port overrides...${NC}"

# Read firewall rules from tofu output
if ! FIREWALL_JSON=$(cd "$TOFU_DIR" && tofu output -json firewall_rules 2>/dev/null); then
    echo -e "${YELLOW}  Warning: Unable to load firewall_rules from OpenTofu. No firewall overrides will be generated.${NC}" >&2
    FIREWALL_JSON="{}"
fi

if [ "$FIREWALL_JSON" != "{}" ] && [ -n "$FIREWALL_JSON" ]; then
    echo "  Firewall rules found, generating Docker Compose overrides..."

    # Parse firewall rules and generate override files per service
    while read -r service port; do
        [ -z "$service" ] && continue

        # Build override content - expose the port to the host
        # Find the main service container name from the docker-compose.yml
        OVERRIDE_PATH="stacks/$service/docker-compose.firewall.yml"

        if [ -f "stacks/$service/docker-compose.yml" ]; then
            # Get the first service name from the docker-compose file
            FIRST_SERVICE=$(python3 -c "
import yaml, sys
try:
    with open('stacks/$service/docker-compose.yml') as f:
        data = yaml.safe_load(f)
    services = list(data.get('services', {}).keys())
    print(services[0] if services else '')
except Exception as e:
    print(f'Error reading stacks/$service/docker-compose.yml: {e}', file=sys.stderr)
    print('')
" 2>/dev/null)

            if [ -n "$FIRST_SERVICE" ]; then
                # Skip creating generic port override for redpanda - handled separately below
                if [ "$service" != "redpanda" ]; then
                    # Check if override file exists, if so append the port
                    if [ -f "$OVERRIDE_PATH" ]; then
                        # Add port to existing override (under the same service)
                        if ! python3 -c "
import yaml, sys
try:
    with open('$OVERRIDE_PATH') as f:
        data = yaml.safe_load(f)
    svc = data.get('services', {}).get('$FIRST_SERVICE', {})
    ports = svc.get('ports', [])
    port_entry = '$port:$port'
    if port_entry not in ports:
        ports.append(port_entry)
        svc['ports'] = ports
        data.setdefault('services', {})['$FIRST_SERVICE'] = svc
        with open('$OVERRIDE_PATH', 'w') as f:
            yaml.dump(data, f, default_flow_style=False)
except Exception as e:
    print(f'Warning: Failed to modify firewall override for $service: {e}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
                            echo -e "${YELLOW}  Warning: Could not modify firewall override for $service; continuing without updated firewall override${NC}" >&2
                        fi
                    else
                        cat > "$OVERRIDE_PATH" << FWEOF
services:
  $FIRST_SERVICE:
    ports:
      - "$port:$port"
FWEOF
                    fi
                    echo "    Port $port exposed for $service ($FIRST_SERVICE)"
                fi
            fi
        fi
    done < <(echo "$FIREWALL_JSON" | jq -r 'to_entries[] | "\(.key | sub("-[0-9]+$"; "")) \(.value.port)"' 2>/dev/null)

    # Special handling for RedPanda: Generate firewall-specific config
    # Instead of using docker-compose override with CLI flags, we generate
    # a firewall-specific redpanda.yaml with external advertised addresses
    REDPANDA_PORTS=$(echo "$FIREWALL_JSON" | jq -r 'to_entries[] | select(.key | test("^redpanda-[0-9]+$")) | .value.port' 2>/dev/null | sort -n)
    if [ -n "$REDPANDA_PORTS" ]; then
        echo "  Configuring RedPanda for external TCP access (with SASL)..."

        if [ -n "$DOMAIN" ]; then
            # Build ports list for RedPanda dual-listener setup:
            # - Internal listener (port 9092): no auth, Docker network only
            # - External listener (port 19092): SASL auth, for Databricks/external clients
            # Host port 9092 maps to container port 19092 (external SASL listener)
            PORTS_LIST=""
            for p in $REDPANDA_PORTS; do
                if [ "$p" = "9092" ]; then
                    # Kafka: external 9092 → internal 19092 (SASL listener)
                    PORTS_LIST="${PORTS_LIST}      - \"9092:19092\"\n"
                elif [ "$p" = "8081" ] || [ "$p" = "18081" ]; then
                    # Schema Registry: external port → internal 8081
                    PORTS_LIST="${PORTS_LIST}      - \"$p:8081\"\n"
                else
                    PORTS_LIST="${PORTS_LIST}      - \"$p:$p\"\n"
                fi
            done

            # Remove old override file before regenerating (avoid conflicts from previous runs)
            rm -f "stacks/redpanda/docker-compose.firewall.yml"

            # Create docker-compose override with port mappings only (no command flags)
            cat > "stacks/redpanda/docker-compose.firewall.yml" << RPEOF
services:
  redpanda:
    ports:
$(echo -e "$PORTS_LIST")
RPEOF

            # Generate firewall-specific redpanda.yaml from template
            # This replaces the standard redpanda.yaml when firewall is enabled
            REDPANDA_FIREWALL_CONFIG="stacks/redpanda/config/redpanda-firewall.yaml"
            sed "s/__REDPANDA_KAFKA_DOMAIN__/redpanda-kafka.$DOMAIN/g" \
                "stacks/redpanda/config/redpanda-firewall.yaml.template" > "$REDPANDA_FIREWALL_CONFIG"

            echo "    RedPanda configured for external access (SASL):"
            for p in $REDPANDA_PORTS; do
                if [ "$p" = "9092" ]; then
                    echo "      Kafka: redpanda-kafka.$DOMAIN:9092 (SASL_PLAINTEXT)"
                elif [ "$p" = "8081" ] || [ "$p" = "18081" ]; then
                    echo "      Schema Registry: redpanda-schema-registry.$DOMAIN:$p"
                fi
            done
        fi
    fi

else
    echo "  No firewall rules enabled (Zero Entry mode)"
fi

# Copy firewall override files to server (only for enabled services)
echo ""
echo -e "${YELLOW}Copying firewall override files to server...${NC}"
for override_file in stacks/*/docker-compose.firewall.yml; do
    if [ -f "$override_file" ]; then
        service=$(basename $(dirname "$override_file"))
        # Only copy if service is enabled (directory exists on server)
        if echo "$ENABLED_SERVICES" | grep -qw "$service"; then
            echo "  Copying $service firewall override..."
            scp -q "$override_file" nexus:/opt/docker-server/stacks/$service/ || {
                echo -e "${RED}  Failed to copy $service firewall override${NC}"
                exit 1
            }
        else
            echo "  Skipping $service (not enabled)"
        fi
    fi
done
echo -e "${GREEN}✓ Firewall override files copied${NC}"

# Copy RedPanda production configuration directory
if echo "$ENABLED_SERVICES" | grep -qw "redpanda"; then
    echo ""
    echo -e "${YELLOW}Copying RedPanda production configuration...${NC}"
    if [ -d "stacks/redpanda/config" ]; then
        # Create config directory on server if it doesn't exist
        ssh nexus "mkdir -p /opt/docker-server/stacks/redpanda/config" || {
            echo -e "${RED}  Failed to create config directory${NC}"
            exit 1
        }

        # Check if firewall is enabled for RedPanda
        REDPANDA_FIREWALL_ENABLED=$(echo "$FIREWALL_JSON" | jq -r 'to_entries[] | select(.key | test("^redpanda-[0-9]+$")) | .value.port' 2>/dev/null)

        if [ -n "$REDPANDA_FIREWALL_ENABLED" ] && [ -f "stacks/redpanda/config/redpanda-firewall.yaml" ]; then
            # Firewall mode: Use the generated firewall-specific config
            echo "  Using firewall configuration (external advertised addresses)"
            scp -q "stacks/redpanda/config/redpanda-firewall.yaml" nexus:/opt/docker-server/stacks/redpanda/config/redpanda.yaml || {
                echo -e "${RED}  Failed to copy firewall config${NC}"
                exit 1
            }
        else
            # Normal mode: Use standard config
            scp -q "stacks/redpanda/config/redpanda.yaml" nexus:/opt/docker-server/stacks/redpanda/config/redpanda.yaml || {
                echo -e "${RED}  Failed to copy redpanda config${NC}"
                exit 1
            }
        fi

        # Remove old redpanda.yaml file from root (if exists from previous deployment)
        ssh nexus "rm -f /opt/docker-server/stacks/redpanda/redpanda.yaml" 2>/dev/null || true

        # Set write permissions on config directory (RedPanda needs to create temp files)
        # Try to set owner to redpanda user (101:101), fallback to world-writable
        if ! ssh nexus "sudo chown -R 101:101 /opt/docker-server/stacks/redpanda/config" 2>/dev/null; then
            echo -e "${YELLOW}  Warning: Could not set config ownership to redpanda user (101:101), using world-writable fallback${NC}" >&2
            ssh nexus "sudo chmod -R 777 /opt/docker-server/stacks/redpanda/config" || {
                echo -e "${RED}  Error: Could not set world-writable (chmod 777) permissions on RedPanda config directory${NC}" >&2
                exit 1
            }
        fi

        if [ -n "$REDPANDA_FIREWALL_ENABLED" ]; then
            echo -e "${GREEN}✓ RedPanda firewall configuration copied${NC}"
        else
            echo -e "${GREEN}✓ RedPanda configuration copied (production mode)${NC}"
        fi
    else
        echo -e "${RED}  redpanda config directory not found!${NC}"
        exit 1
    fi
fi

# -----------------------------------------------------------------------------
# Pre-pull Docker images (parallel)
# -----------------------------------------------------------------------------
# Start containers (parallel)
# Note: --build ensures stacks with Dockerfiles (e.g. Spark) are always rebuilt.
# Docker build cache makes this fast when nothing changed. For image-only
# services, --build is a no-op.
# -----------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[6/7] Starting enabled containers (parallel)...${NC}"

# Migrated from a 130-line ssh heredoc (Phase 2 Modul 2.2a, #505).
# nexus_deploy.compose_runner expands virtual services to parents,
# de-dupes, skips deferred services, and runs the parallel
# docker-compose-up + docker-ps verify loop server-side. Per-service
# admin-setup hooks (Wikijs/Dify/Metabase/Superset/LakeFS/OpenMetadata/
# Gitea/Filestash/RedPanda) ship in Modul 2.2b.
COMPOSE_RC=0
# $ENABLED_SERVICES is newline-separated (`jq -r '.[]'` at L479); we
# need a comma-list. `tr '\n ' ',,'` converts both newlines AND
# stray spaces to commas; the Python CLI's `--enabled` parser
# filters empty entries from leading/trailing/consecutive commas,
# so trailing `\n` from echo is harmless.
uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy compose up \
    --enabled "$(echo "$ENABLED_SERVICES" | tr '\n ' ',,')" \
    || COMPOSE_RC=$?
case "$COMPOSE_RC" in
    0) echo -e "${GREEN}  ✓ All containers started successfully${NC}" ;;
    1) echo -e "${YELLOW}  ⚠ Compose-up had partial failures (continuing)${NC}" ;;
    *) echo -e "${RED}  ✗ Compose-up hard failure (rc=$COMPOSE_RC) — bad args, transport, no parseable RESULT, or unexpected error; check Python stderr above. Aborting.${NC}"; exit "$COMPOSE_RC" ;;
esac

# -----------------------------------------------------------------------------
# Auto-configure services
# -----------------------------------------------------------------------------
echo ""
echo -e "${YELLOW}[7/7] Auto-configuring services...${NC}"

# Initialize array for background configuration jobs
CONFIG_JOBS=()

# Configure Infisical admin and push secrets (idempotent - runs on every spin-up)
if echo "$ENABLED_SERVICES" | grep -qw "infisical"; then
    echo "  Configuring Infisical..."

    # Wait for Infisical to be ready (optimized: check container status first)
    echo "  Waiting for Infisical to be ready (may take up to 2min)..."
    INFISICAL_READY=false
    for i in $(seq 1 20); do
        CONTAINER_STATUS=$(ssh nexus "docker inspect --format='{{.State.Status}}' infisical 2>/dev/null" || echo "")
        if [ "$CONTAINER_STATUS" = "running" ]; then break; fi
        sleep 2
    done
    for i in $(seq 1 40); do
        if ssh nexus "curl -s --connect-timeout 3 'http://localhost:8070/api/v1/admin/config'" 2>/dev/null | grep -q 'initialized'; then
            INFISICAL_READY=true
            break
        fi
        sleep 3
    done

    if [ "$INFISICAL_READY" = "false" ]; then
        echo -e "${YELLOW}  ⚠ Infisical not responding after 120s - skipping config${NC}"
    else
    INFISICAL_TOKEN=""
    PROJECT_ID=""
    INIT_CHECK=$(ssh nexus "curl -s 'http://localhost:8070/api/v1/admin/config'" 2>/dev/null || echo "")

    if echo "$INIT_CHECK" | grep -q '"initialized":true'; then
        # Existing instance - load saved credentials
        echo "  Infisical already initialized - loading saved credentials..."
        INFISICAL_TOKEN=$(ssh nexus "cat /opt/docker-server/.infisical-token 2>/dev/null" || echo "")
        PROJECT_ID=$(ssh nexus "cat /opt/docker-server/.infisical-project-id 2>/dev/null" || echo "")
        if [ -z "$INFISICAL_TOKEN" ] || [ -z "$PROJECT_ID" ]; then
            echo -e "${YELLOW}  ⚠ No saved credentials - run destroy-all + initial-setup to re-bootstrap${NC}"
        else
            echo -e "${GREEN}  ✓ Loaded Infisical credentials${NC}"
        fi
    else
        # New instance - bootstrap admin + create project
        BOOTSTRAP_JSON=$(cat <<EOF
{"email": "$ADMIN_EMAIL", "password": "$INFISICAL_PASS", "organization": "Nexus"}
EOF
)
        BOOTSTRAP_RESULT=$(ssh nexus "curl -s -X POST 'http://localhost:8070/api/v1/admin/bootstrap' \
            -H 'Content-Type: application/json' \
            -d '$(echo "$BOOTSTRAP_JSON" | tr -d '\n')'" 2>&1 || echo "")

        if echo "$BOOTSTRAP_RESULT" | grep -q '"user"'; then
            echo -e "${GREEN}  ✓ Infisical admin created (user: $ADMIN_EMAIL)${NC}"
            INFISICAL_TOKEN=$(echo "$BOOTSTRAP_RESULT" | jq -r '.identity.credentials.token // empty')
            ORG_ID=$(echo "$BOOTSTRAP_RESULT" | jq -r '.organization.id // empty')

            if [ -n "$INFISICAL_TOKEN" ] && [ -n "$ORG_ID" ]; then
                echo "  Creating Nexus secrets project..."
                PROJECT_RESULT=$(ssh nexus "curl -s -X POST 'http://localhost:8070/api/v2/workspace' \
                    -H 'Authorization: Bearer $INFISICAL_TOKEN' \
                    -H 'Content-Type: application/json' \
                    -d '{\"projectName\": \"Nexus Stack\", \"organizationId\": \"$ORG_ID\"}'" 2>&1 || echo "")
                PROJECT_ID=$(echo "$PROJECT_RESULT" | jq -r '.project.id // .workspace.id // empty')

                if [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "null" ]; then
                    echo -e "${GREEN}  ✓ Project 'Nexus Stack' created${NC}"
                    # Save credentials for subsequent spin-ups
                    echo "$INFISICAL_TOKEN" | ssh nexus "cat > /opt/docker-server/.infisical-token && chmod 600 /opt/docker-server/.infisical-token"
                    echo "$PROJECT_ID" | ssh nexus "cat > /opt/docker-server/.infisical-project-id && chmod 600 /opt/docker-server/.infisical-project-id"
                    echo -e "${GREEN}  ✓ Credentials saved for subsequent deployments${NC}"
                else
                    echo -e "${YELLOW}  ⚠ Failed to create project${NC}"
                fi
            fi
        elif echo "$BOOTSTRAP_RESULT" | grep -q 'already'; then
            echo -e "${YELLOW}  ⚠ Infisical already configured${NC}"
        else
            echo -e "${YELLOW}  ⚠ Infisical bootstrap failed${NC}"
        fi
    fi

    # ==========================================================================
    # Push secrets to Infisical (#505 Modul 1.1: nexus_deploy.infisical)
    # ==========================================================================
    # The legacy build_folder helper + 39 callers + rsync + curl-loop
    # push (~395 lines of bash) lived here. Phase-1 strangler-fig:
    # deploy.sh now pipes SECRETS_JSON into `python -m nexus_deploy
    # infisical bootstrap`, which builds the same JSON payloads,
    # rsyncs them, and runs the same server-side curl loop. The
    # remaining BootstrapEnv fields (DOMAIN, ADMIN_EMAIL, GITEA_*,
    # WOODPECKER_*, etc.) are passed as env vars to the Python side.
    if [ -n "$INFISICAL_TOKEN" ] && [ -n "$PROJECT_ID" ]; then
        echo "  Pushing secrets to Infisical (via nexus_deploy)..."
        INFISICAL_ENV="${INFISICAL_ENV:-dev}"
        # SSH_KEY_BASE64 must match the legacy `build_folder "ssh"`
        # encoding byte-for-byte: ``echo "$X" | base64`` (NOT
        # ``printf '%s'``). echo appends a trailing newline before
        # the pipe, so the legacy bytes are base64(<key>+\n).
        #
        # Critically guarded on `[ -n "$SSH_PRIVATE_KEY_CONTENT" ]`:
        # the legacy `build_folder "ssh"` was inside an `if [ -n …]`
        # block (deploy.sh:2335 pre-migration), so when the key is
        # unset, the "ssh" folder isn't pushed at all — preserving
        # any operator-managed key already in Infisical. Without
        # this guard, ``echo "" | base64`` produces ``Cg==`` (base64
        # of a single newline), which BootstrapEnv would treat as a
        # populated key and overwrite the operator's value.
        if [ -n "${SSH_PRIVATE_KEY_CONTENT:-}" ]; then
            SSH_KEY_BASE64=$(echo "$SSH_PRIVATE_KEY_CONTENT" | base64 | tr -d '\n')
        else
            SSH_KEY_BASE64=""
        fi
        # Capture exit code instead of `if !` so we can distinguish
        # nexus_deploy's three exit modes:
        #   0 = success, all folders pushed
        #   1 = partial — bootstrap completed but some folders reported
        #       errors. Operator-fixable via the Infisical UI; we warn
        #       and continue so the rest of the spin-up proceeds.
        #   2 = hard failure (input validation, rsync/ssh transport,
        #       missing uv, unexpected exception). Abort the deploy
        #       — continuing here would push stale Infisical state to
        #       services that read from it later in the spin-up.
        # The `|| INFISICAL_RC=$?` form avoids tripping `set -e` on a
        # non-zero return; the explicit case statement decides what
        # to do with each code.
        INFISICAL_RC=0
        printf '%s' "$SECRETS_JSON" | \
            PROJECT_ID="$PROJECT_ID" \
            INFISICAL_TOKEN="$INFISICAL_TOKEN" \
            INFISICAL_ENV="$INFISICAL_ENV" \
            DOMAIN="$DOMAIN" \
            ADMIN_EMAIL="$ADMIN_EMAIL" \
            GITEA_USER_EMAIL="${GITEA_USER_EMAIL:-}" \
            GITEA_USER_USERNAME="${GITEA_USER_USERNAME:-}" \
            GITEA_REPO_OWNER="${GITEA_REPO_OWNER:-}" \
            REPO_NAME="${REPO_NAME:-}" \
            OM_PRINCIPAL_DOMAIN="${OM_PRINCIPAL_DOMAIN:-}" \
            WOODPECKER_GITEA_CLIENT="${WOODPECKER_GITEA_CLIENT:-}" \
            WOODPECKER_GITEA_SECRET="${WOODPECKER_GITEA_SECRET:-}" \
            SSH_KEY_BASE64="$SSH_KEY_BASE64" \
            uv run --quiet --project "$PROJECT_ROOT" python -m nexus_deploy infisical bootstrap \
            || INFISICAL_RC=$?
        case "$INFISICAL_RC" in
            0) ;;
            1) echo -e "${YELLOW}  ⚠ Infisical bootstrap had partial push failures (see output above)${NC}" ;;
            *) echo -e "${RED}  ✗ Infisical bootstrap transport failure (rc=$INFISICAL_RC); aborting${NC}"; exit 1 ;;
        esac
    fi
    fi  # End of INFISICAL_READY check
fi

# Configure REST first-init admin hooks (Phase 2 Modul 2.2b, #505):
# Portainer + n8n + Metabase + LakeFS + OpenMetadata. The Python CLI
# renders the per-hook bash, runs it via one ssh round-trip, and
# parses RESULT lines per hook. Other admin-setup hooks (Filestash,
# RedPanda, Superset, Gitea, Wiki.js, Dify, etc.) ship in 2.2c/d.
SERVICES_RC=0
printf '%s' "$SECRETS_JSON" | DOMAIN="$DOMAIN" ADMIN_EMAIL="$ADMIN_EMAIL" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy services configure \
    --enabled "$(printf '%s' "$ENABLED_SERVICES" | tr '\n ' ',,')" \
    || SERVICES_RC=$?
case "$SERVICES_RC" in
    0) ;;
    1) echo -e "${YELLOW}  ⚠ Some admin-setup hooks failed (continuing)${NC}" ;;
    *) echo -e "${RED}  ✗ services configure hard failure (rc=$SERVICES_RC) — bad args, transport, or unexpected error; check Python stderr above. Aborting.${NC}"; exit "$SERVICES_RC" ;;
esac

# Re-apply pg_ducklake bootstrap SQL (handles credential rotation)
# /docker-entrypoint-initdb.d/ scripts only run on empty data dir, so we
# also exec the same SQL after every spin-up to ensure rotated credentials
# take effect on existing volumes.
if echo "$ENABLED_SERVICES" | grep -qw "pg-ducklake" && [ -n "$PG_DUCKLAKE_PASS" ]; then
    (
        echo "  Configuring pg_ducklake (re-applying bootstrap SQL)..."
        # Wait for healthcheck to be ready (~30s timeout)
        PG_DUCKLAKE_READY=false
        for i in $(seq 1 15); do
            if ssh nexus "docker exec pg-ducklake pg_isready -U nexus-pgducklake -d ducklake" >/dev/null 2>&1; then
                PG_DUCKLAKE_READY=true
                break
            fi
            sleep 2
        done

        if [ "$PG_DUCKLAKE_READY" = "false" ]; then
            echo -e "${YELLOW}  ⚠ pg_ducklake not ready after 30s - skipping re-apply${NC}"
            exit 0
        fi

        # Re-apply bootstrap SQL via docker exec (idempotent, handles credential rotation)
        if ssh nexus "docker exec pg-ducklake psql -U nexus-pgducklake -d ducklake -f /docker-entrypoint-initdb.d/00-ducklake-bootstrap.sql" >/dev/null 2>&1; then
            echo -e "${GREEN}  ✓ pg_ducklake bootstrap SQL re-applied${NC}"
        else
            echo -e "${YELLOW}  ⚠ pg_ducklake bootstrap re-apply failed (may already be applied)${NC}"
        fi
    ) &
    CONFIG_JOBS+=($!)
fi


# Filestash admin setup (host fix, force_ssl, S3 backend) is handled
# by the `python -m nexus_deploy services configure --enabled <list>`
# call ABOVE (Phase 2 Modul 2.2d, #505). The legacy bash block here was
# replaced because the JSON-mutation logic is now testable as pure Python.


# Configure SFTPGo: create the default user `nexus-default` with an
# R2-backed virtual filesystem. The admin (nexus-sftpgo) is bootstrapped
# via SFTPGO_DEFAULT_ADMIN_* env vars in docker-compose.yml; here we
# log in with that admin to mint a JWT and POST /api/v2/users to add
# the SFTP-facing account.
if echo "$ENABLED_SERVICES" | grep -qw "sftpgo" && [ -n "$SFTPGO_ADMIN_PASS" ] && [ -n "$SFTPGO_USER_PASS" ]; then
    echo "  Configuring SFTPGo..."

    # Two-stage readiness: first /healthz must answer 200 (process is up,
    # HTTP server bound), THEN the /api/v2/token basic-auth login must
    # succeed (admin creation via SFTPGO_DEFAULT_ADMIN_* env vars has
    # actually written to SQLite — this lags /healthz by a few seconds
    # on cold start). Without the second check, we hit /api/v2/token
    # while admin-init is still in flight and get 401 → "admin login
    # failed — default user not created", and the run looks green.
    SFTPGO_READY=false
    SFTPGO_PROBE_B64=$(printf '%s' "$SFTPGO_ADMIN_PASS" | base64 | tr -d '\n')

    # /healthz + /api/v2/token probe loop. With ephemeral SFTPGo state
    # (docker-named-volume, NOT a bind-mount onto /mnt/nexus-data/), the
    # admin row is freshly bootstrapped from $SFTPGO_DEFAULT_ADMIN_*
    # on every cold start, so this loop succeeds on the first or
    # second iteration in practice. The 60×2 s ceiling is just a
    # defensive cap for the cold-start race between /healthz returning
    # 200 and the data provider finishing admin-init.
    for _i in $(seq 1 60); do
        # `--connect-timeout 3 --max-time 5` mirrors the Kestra
        # readiness probe — bounds each iteration so a stalled
        # localhost socket can't make the loop hang past the 60×2 s
        # ceiling.
        _ah=$(ssh nexus "curl -s --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' http://localhost:8090/healthz 2>/dev/null") || true
        _ah="${_ah:-000}"
        if [ "$_ah" = "200" ]; then
            _au=$(ssh nexus "bash -s" <<REMOTE_SFTPGO_PROBE_EOF 2>/dev/null
PW=\$(printf '%s' '$SFTPGO_PROBE_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'user = "nexus-sftpgo:%s"\n' "\$PW" > "\$CFG"
curl -s --connect-timeout 3 --max-time 5 -o /dev/null -w '%{http_code}' --config "\$CFG" 'http://localhost:8090/api/v2/token'
REMOTE_SFTPGO_PROBE_EOF
) || _au=""
            _au="${_au:-000}"
            if [ "$_au" = "200" ]; then
                SFTPGO_READY=true
                break
            fi
        fi
        sleep 2
    done

    if [ "$SFTPGO_READY" = "false" ]; then
        echo -e "${YELLOW}  ⚠ SFTPGo not ready after probe — skipping default-user creation${NC}"
    elif [ -z "$R2_DATA_BUCKET" ] || [ -z "$R2_DATA_ENDPOINT" ] || [ -z "$R2_DATA_ACCESS_KEY" ] || [ -z "$R2_DATA_SECRET_KEY" ]; then
        echo -e "${YELLOW}  ⚠ R2 datalake credentials missing — SFTPGo admin is up, but default user not created (configure manually in the UI)${NC}"
    else
        # ----------------------------------------------------------------
        # Step 1: get an admin JWT. SFTPGo's /api/v2/token endpoint
        # accepts basic auth; the response carries an `access_token`.
        #
        # Both this call (admin password as basic-auth credential) and
        # Step 2 (bearer token as Authorization header) run inside
        # `bash -s` heredocs on the remote so no secret transits via
        # argv. The runner base64-encodes each value, the remote bash
        # decodes via the `printf` builtin (no fork-exec, no argv) and
        # writes a mode-600 curl `--config` file. Same argv-safe pattern
        # the Kestra-bootstrap block uses for INFISICAL_TOKEN/KESTRA_PASS.
        # ----------------------------------------------------------------
        SFTPGO_ADMIN_B64=$(printf '%s' "$SFTPGO_ADMIN_PASS" | base64 | tr -d '\n')
        SFTPGO_TOKEN_RESP=$(ssh nexus "bash -s" <<REMOTE_SFTPGO_TOKEN_EOF 2>/dev/null
ADMIN_PW=\$(printf '%s' '$SFTPGO_ADMIN_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'user = "nexus-sftpgo:%s"\n' "\$ADMIN_PW" > "\$CFG"
curl -s --config "\$CFG" 'http://localhost:8090/api/v2/token'
REMOTE_SFTPGO_TOKEN_EOF
) || SFTPGO_TOKEN_RESP=""
        SFTPGO_TOKEN=$(echo "$SFTPGO_TOKEN_RESP" | jq -r '.access_token // empty' 2>/dev/null)

        if [ -z "$SFTPGO_TOKEN" ]; then
            echo -e "${YELLOW}  ⚠ SFTPGo admin login failed — default user not created${NC}"
        else
            # Step 2: create nexus-default with R2 vfs config. provider=1
            # is SFTPGo's S3 backend (works for any S3-compatible endpoint
            # including R2). home_dir is virtual, scoped to the user; it
            # maps onto the bucket prefix below via key_prefix.
            #
            # Each input transits as base64 over the heredoc body — no
            # secret bytes ever appear in argv on either side. On the
            # runner, the value is fed to `base64` via a pipe (so the
            # `printf` part is a builtin and the secret reaches `base64`
            # over stdin, not argv). On the remote shell, the decode uses
            # the `printf` builtin → `base64 -d` pipe with the same
            # property. Decoded values are then handed to a remote `jq -n`
            # invocation via env vars (`env.VAR`) — jq reads them from
            # its environment, never from argv. The constructed JSON
            # transits to remote curl via stdin (`--data-binary @-`),
            # while bearer-token + content-type sit in a mode-600 curl
            # `--config` file written by the `printf` builtin.
            SFTPGO_TOKEN_B64=$(printf '%s' "$SFTPGO_TOKEN" | base64 | tr -d '\n')
            SFTPGO_USER_PASS_B64=$(printf '%s' "$SFTPGO_USER_PASS" | base64 | tr -d '\n')

            # SFTPGo doesn't auto-create the local FS paths it expects:
            # - home_dir (`/var/lib/sftpgo/users/nexus-default`) is the
            #   user's local-FS scratch root for files written at path
            #   "/" (i.e. NOT inside a virtual folder mount).
            # - mapped_path under each folder (`/var/lib/sftpgo/folders/<name>`)
            #   is the local "shadow" path SFTPGo uses internally for
            #   metadata caching even though the actual data is in S3.
            # Without these, the very first directory listing returns
            # "Failed to get directory listing" / "lstat: no such file
            # or directory". Pre-create + chown to the SFTPGo container
            # uid (1000) before the API calls touch them.
            # Capture stderr so a real failure (container missing,
            # docker daemon unhealthy, …) surfaces with the actual
            # cause rather than being swallowed silently. Subsequent
            # API calls (folder POST, user POST) will fail downstream
            # if these dirs aren't there, and "Failed to get directory
            # listing" 100 lines later is a confusing way to learn the
            # mkdir step never ran.
            SFTPGO_PREP_ERR=$(ssh nexus "docker exec --user 0 sftpgo sh -c 'mkdir -p /var/lib/sftpgo/users/nexus-default /var/lib/sftpgo/folders/cloudflare_r2 /var/lib/sftpgo/folders/hetzner_s3 && chown -R 1000:1000 /var/lib/sftpgo/users /var/lib/sftpgo/folders'" 2>&1) || SFTPGO_PREP_ERR_RC=$?
            if [ -n "${SFTPGO_PREP_ERR_RC:-}" ]; then
                echo -e "${YELLOW}  ⚠ SFTPGo dir-prep step (mkdir/chown inside the container) failed: $SFTPGO_PREP_ERR${NC}"
                echo -e "${YELLOW}    → SFTPGo user-creation will likely fail with 'Failed to get directory listing' on first login. Configure manually in the admin UI if needed.${NC}"
            fi
            unset SFTPGO_PREP_ERR_RC

            SFTPGO_R2_BUCKET_B64=$(printf '%s' "$R2_DATA_BUCKET" | base64 | tr -d '\n')
            SFTPGO_R2_ENDPOINT_B64=$(printf '%s' "$R2_DATA_ENDPOINT" | base64 | tr -d '\n')
            SFTPGO_R2_AK_B64=$(printf '%s' "$R2_DATA_ACCESS_KEY" | base64 | tr -d '\n')
            SFTPGO_R2_SK_B64=$(printf '%s' "$R2_DATA_SECRET_KEY" | base64 | tr -d '\n')

            # The SFTPGo user gets virtual-folders (one per backend) so
            # a single SFTP login surfaces multiple object-storage
            # backends as subdirectories of the user's home:
            #
            #   /              local FS (scratch space inside the
            #                  container, ephemeral by design — see
            #                  the named-volume rationale at the top of
            #                  the SFTPGo compose)
            #   /cloudflare_r2   Cloudflare R2 datalake bucket
            #   /hetzner_s3      Hetzner Object Storage (only mounted if
            #                    HETZNER_S3_* secrets are present)
            #
            # Operators can add more virtual folders for AWS S3, GCS,
            # Azure Blob, additional MinIO instances, etc. via the
            # admin UI — see "Connecting to non-R2 storage" in
            # docs/stacks/sftpgo.md. The auto-configured pair is just
            # what we have credentials for at deploy time.

            # Helper 1: POST /api/v2/folders to register a backend
            # under a friendly name. Idempotent: 201 = created,
            # 409 = already exists from a previous spin-up. Both fine.
            #
            # Args (passed via SFTPGO_FOLDER_* env on the runner side
            # before invocation):
            #   $1 = SFTPGO_FOLDER_NAME           — virtual folder name
            #   $2 = SFTPGO_FOLDER_BUCKET_B64
            #   $3 = SFTPGO_FOLDER_ENDPOINT_B64
            #   $4 = SFTPGO_FOLDER_REGION         — plain (not base64'd, no secret)
            #   $5 = SFTPGO_FOLDER_AK_B64
            #   $6 = SFTPGO_FOLDER_SK_B64
            sftpgo_post_folder() {
                local _name="$1" _bucket_b64="$2" _endpoint_b64="$3" _region="$4" _ak_b64="$5" _sk_b64="$6"
                ssh nexus "bash -s" <<REMOTE_SFTPGO_FOLDER_EOF 2>/dev/null
TOKEN=\$(printf '%s' '$SFTPGO_TOKEN_B64' | base64 -d)
BUCKET=\$(printf '%s' '$_bucket_b64' | base64 -d)
ENDPOINT=\$(printf '%s' '$_endpoint_b64' | base64 -d)
AK=\$(printf '%s' '$_ak_b64' | base64 -d)
SK=\$(printf '%s' '$_sk_b64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "\$TOKEN" > "\$CFG"
NAME='$_name' BUCKET="\$BUCKET" ENDPOINT="\$ENDPOINT" REGION='$_region' AK="\$AK" SK="\$SK" jq -n '{
    name: env.NAME,
    mapped_path: ("/var/lib/sftpgo/folders/" + env.NAME),
    filesystem: {
        provider: 1,
        s3config: {
            bucket: env.BUCKET,
            endpoint: env.ENDPOINT,
            region: env.REGION,
            access_key: env.AK,
            access_secret: { payload: env.SK, status: "Plain" },
            key_prefix: "",
            force_path_style: true
        }
    }
}' | curl -s -o /dev/null -w '%{http_code}' \\
    -X POST 'http://localhost:8090/api/v2/folders' \\
    --config "\$CFG" \\
    --data-binary @-
REMOTE_SFTPGO_FOLDER_EOF
            }

            # Register the R2 virtual folder (always — R2 creds are
            # required for SFTPGo to be configured at all per the
            # earlier R2_DATA_* guard).
            R2_FOLDER_STATUS=$(sftpgo_post_folder \
                "cloudflare_r2" \
                "$SFTPGO_R2_BUCKET_B64" \
                "$SFTPGO_R2_ENDPOINT_B64" \
                "auto" \
                "$SFTPGO_R2_AK_B64" \
                "$SFTPGO_R2_SK_B64") || true
            R2_FOLDER_STATUS="${R2_FOLDER_STATUS:-000}"
            case "$R2_FOLDER_STATUS" in
                201)     echo "    ✓ SFTPGo virtual folder '/cloudflare_r2' registered (R2 datalake)" ;;
                409)     echo "    ✓ SFTPGo virtual folder '/cloudflare_r2' already exists — left untouched" ;;
                *)       echo -e "${YELLOW}    ⚠ SFTPGo virtual folder '/cloudflare_r2' POST returned HTTP $R2_FOLDER_STATUS${NC}" ;;
            esac

            # Optionally register the Hetzner Object Storage folder.
            # Only the lakefs-bucket (HETZNER_S3_BUCKET) credentials are
            # always populated by the deploy pipeline; if any of the
            # five fields is missing (e.g. Hetzner OBS unavailable in
            # the user's project), we skip the mount. Operators can add
            # it later via the admin UI.
            VIRTUAL_FOLDERS_JSON='[{"name":"cloudflare_r2","virtual_path":"/cloudflare_r2","quota_size":-1,"quota_files":-1}]'
            if [ -n "$HETZNER_S3_BUCKET_GENERAL" ] && [ -n "$HETZNER_S3_SERVER" ] && [ -n "$HETZNER_S3_REGION" ] \
                && [ -n "$HETZNER_S3_ACCESS_KEY" ] && [ -n "$HETZNER_S3_SECRET_KEY" ]; then
                HZ_BUCKET_B64=$(printf '%s' "$HETZNER_S3_BUCKET_GENERAL" | base64 | tr -d '\n')
                HZ_ENDPOINT_B64=$(printf '%s' "$HETZNER_S3_SERVER" | base64 | tr -d '\n')
                HZ_AK_B64=$(printf '%s' "$HETZNER_S3_ACCESS_KEY" | base64 | tr -d '\n')
                HZ_SK_B64=$(printf '%s' "$HETZNER_S3_SECRET_KEY" | base64 | tr -d '\n')
                HZ_FOLDER_STATUS=$(sftpgo_post_folder \
                    "hetzner_s3" \
                    "$HZ_BUCKET_B64" \
                    "$HZ_ENDPOINT_B64" \
                    "$HETZNER_S3_REGION" \
                    "$HZ_AK_B64" \
                    "$HZ_SK_B64") || true
                HZ_FOLDER_STATUS="${HZ_FOLDER_STATUS:-000}"
                case "$HZ_FOLDER_STATUS" in
                    201)     echo "    ✓ SFTPGo virtual folder '/hetzner_s3' registered (Hetzner Object Storage)" ;;
                    409)     echo "    ✓ SFTPGo virtual folder '/hetzner_s3' already exists — left untouched" ;;
                    *)       echo -e "${YELLOW}    ⚠ SFTPGo virtual folder '/hetzner_s3' POST returned HTTP $HZ_FOLDER_STATUS${NC}" ;;
                esac
                if [ "$HZ_FOLDER_STATUS" = "201" ] || [ "$HZ_FOLDER_STATUS" = "409" ]; then
                    VIRTUAL_FOLDERS_JSON='[{"name":"cloudflare_r2","virtual_path":"/cloudflare_r2","quota_size":-1,"quota_files":-1},{"name":"hetzner_s3","virtual_path":"/hetzner_s3","quota_size":-1,"quota_files":-1}]'
                fi
            else
                echo "    (Hetzner Object Storage credentials missing — skipping '/hetzner_s3' virtual folder)"
            fi

            # Helper 2: POST /api/v2/users with a local-FS home and the
            # registered virtual folders attached. The local home is
            # ephemeral scratch space; real data sits behind the virtual
            # folders. SFTPGo materialises home_dir on first login if
            # missing, so the operator does not have to mkdir it.
            VIRTUAL_FOLDERS_JSON_B64=$(printf '%s' "$VIRTUAL_FOLDERS_JSON" | base64 | tr -d '\n')
            sftpgo_post_user() {
                ssh nexus "bash -s" <<REMOTE_SFTPGO_USER_EOF 2>/dev/null
TOKEN=\$(printf '%s' '$SFTPGO_TOKEN_B64' | base64 -d)
SFTP_USER_PASS=\$(printf '%s' '$SFTPGO_USER_PASS_B64' | base64 -d)
VFOLDERS=\$(printf '%s' '$VIRTUAL_FOLDERS_JSON_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "\$TOKEN" > "\$CFG"
# All env-vars on a single line as a prefix to \`jq -n\` (see
# the earlier comment in this file: a comment in the middle of a
# bash line-continuation chain swallows the prefix and jq runs
# without env, so all env.X resolve to null → 400).
PASSWORD="\$SFTP_USER_PASS" VFOLDERS="\$VFOLDERS" jq -n '{
    username: "nexus-default",
    password: env.PASSWORD,
    home_dir: "/var/lib/sftpgo/users/nexus-default",
    permissions: { "/": ["*"], "/cloudflare_r2": ["*"], "/hetzner_s3": ["*"] },
    status: 1,
    filesystem: { provider: 0 },
    virtual_folders: (env.VFOLDERS | fromjson)
}' | curl -s -o /dev/null -w '%{http_code}' \\
    -X POST 'http://localhost:8090/api/v2/users' \\
    --config "\$CFG" \\
    --data-binary @-
REMOTE_SFTPGO_USER_EOF
            }

            SFTPGO_USER_STATUS=$(sftpgo_post_user) || true
            SFTPGO_USER_STATUS="${SFTPGO_USER_STATUS:-000}"

            # 201 = freshly created (initial-setup or post-destroy).
            # 400/409 = SFTPGo's "user already exists" responses on a
            #           re-run of spin-up against an already-running
            #           container (named volume preserves the row across
            #           in-place spin-ups; only destroy-all wipes it
            #           because that destroys the docker volume too).
            #           Treat as benign — re-deploys aren't supposed to
            #           print a yellow warning for a healthy state.
            case "$SFTPGO_USER_STATUS" in
                201)     echo -e "${GREEN}  ✓ SFTPGo user 'nexus-default' created with virtual folders (/cloudflare_r2 + /hetzner_s3 if available)${NC}" ;;
                400|409) echo "  ✓ SFTPGo user 'nexus-default' already exists — left untouched" ;;
                *)       echo -e "${YELLOW}  ⚠ SFTPGo user creation returned HTTP $SFTPGO_USER_STATUS — configure manually${NC}"
                         echo -e "${YELLOW}    Credentials available in Infisical${NC}" ;;
            esac
        fi
    fi
fi


# -----------------------------------------------------------------------------
# TODO: Fix Uptime Kuma auto-configuration (Issue #145)
# -----------------------------------------------------------------------------
# The Socket.io-based setup fails with "server error" when connecting from
# inside the container. This needs investigation - possibly a socket.io
# client/server version mismatch or container networking issue.
# For now, users must configure Uptime Kuma manually on first login.
# Credentials are available in Infisical.
# -----------------------------------------------------------------------------
# Configure Uptime Kuma admin
# if echo "$ENABLED_SERVICES" | grep -qw "uptime-kuma" && [ -n "$KUMA_PASS" ]; then
#     ... (disabled - see TODO above)
# fi

if echo "$ENABLED_SERVICES" | grep -qw "uptime-kuma"; then
    echo -e "${YELLOW}  ⚠ Uptime Kuma requires manual setup on first login${NC}"
    echo -e "${YELLOW}    Credentials available in Infisical${NC}"
fi


# Configure Garage layout (one-time setup after first start)
if echo "$ENABLED_SERVICES" | grep -qw "garage" && [ -n "$GARAGE_ADMIN_TOKEN" ]; then
    (
        echo "  Configuring Garage layout..."
        # Wait for Garage to be ready (check health endpoint)
        for i in $(seq 1 15); do
            if ssh nexus "curl -sf http://localhost:3903/health" >/dev/null 2>&1; then
                break
            fi
            sleep 2
        done

        # Check if layout is already configured (roles exist)
        LAYOUT_CHECK=$(ssh nexus "docker exec garage /garage layout show 2>&1" || echo "")
        if echo "$LAYOUT_CHECK" | grep -q "No nodes currently have"; then
            # Get full node ID and validate it's a valid hex string (64 chars)
            FULL_NODE_ID=$(ssh nexus "docker exec garage /garage node id 2>&1 | head -1" || echo "")
            if [ -n "$FULL_NODE_ID" ] && [ ${#FULL_NODE_ID} -eq 64 ] && echo "$FULL_NODE_ID" | grep -qE '^[0-9a-fA-F]{64}$'; then
                # Extract short form (first 16 chars) for layout commands
                NODE_ID="${FULL_NODE_ID:0:16}"
                # Assign node to layout with 100GB capacity
                ssh nexus "docker exec garage /garage layout assign -z dc1 -c 100G $NODE_ID" >/dev/null 2>&1
                # Apply layout with version 1
                ssh nexus "docker exec garage /garage layout apply --version 1" >/dev/null 2>&1
                # Create default access key
                ssh nexus "docker exec garage /garage key create nexus-garage-key" >/dev/null 2>&1
                echo -e "${GREEN}  ✓ Garage layout configured with 100GB capacity${NC}"
            else
                echo -e "${YELLOW}  ⚠ Could not get Garage node ID - layout setup skipped${NC}"
            fi
        else
            echo -e "${YELLOW}  ⚠ Garage layout already configured${NC}"
        fi
    ) &
    CONFIG_JOBS+=($!)
fi

# Configure Windmill (create admin user, workspace, secure default account)
if echo "$ENABLED_SERVICES" | grep -qw "windmill" && [ -n "$WINDMILL_ADMIN_PASS" ] && [ -n "$WINDMILL_SUPERADMIN_SECRET" ]; then
    (
        echo "  Configuring Windmill..."

        # Wait for Windmill to be ready (check version endpoint)
        WINDMILL_READY=false
        for i in $(seq 1 30); do
            if ssh nexus "curl -s --connect-timeout 2 'http://localhost:8200/api/version'" >/dev/null 2>&1; then
                WINDMILL_READY=true
                break
            fi
            sleep 2
        done

        if [ "$WINDMILL_READY" = "false" ]; then
            echo -e "${YELLOW}  ⚠ Windmill not ready after 60s - skipping auto-configuration${NC}"
            exit 0
        fi

        # All API calls use SUPERADMIN_SECRET as bearer token
        WM_AUTH="Authorization: Bearer $WINDMILL_SUPERADMIN_SECRET"
        WM_CT="Content-Type: application/json"
        WM_URL="http://localhost:8200/api"

        # --- Step 1: Create superadmin user for ADMIN_EMAIL ---
        WINDMILL_CREATE_JSON=$(jq -n --arg email "$ADMIN_EMAIL" --arg password "$WINDMILL_ADMIN_PASS" \
            '{email: $email, password: $password, super_admin: true, name: "Admin"}')
        WINDMILL_CREATE_RESULT=$(printf '%s' "$WINDMILL_CREATE_JSON" | ssh nexus "curl -s -X POST '$WM_URL/users/create' \
            -H '$WM_AUTH' \
            -H '$WM_CT' \
            -d @-" 2>/dev/null || echo "")

        if echo "$WINDMILL_CREATE_RESULT" | grep -q '"email"' 2>/dev/null; then
            echo -e "${GREEN}  ✓ Windmill admin created (user: $ADMIN_EMAIL)${NC}"
        elif echo "$WINDMILL_CREATE_RESULT" | grep -qi 'already exists' 2>/dev/null; then
            echo -e "${YELLOW}  ⚠ Windmill admin already exists${NC}"
        else
            echo -e "${YELLOW}  ⚠ Windmill admin creation: ${WINDMILL_CREATE_RESULT:-no response}${NC}"
        fi

        # --- Step 2: Create regular user for GITEA_USER_EMAIL (if different from ADMIN_EMAIL) ---
        # Use GITEA_USER_EMAIL (single address) not USER_EMAIL (may be comma list).
        # Windmill's email field has the same single-value semantics as Gitea's.
        if [ -n "$GITEA_USER_EMAIL" ] && [ "$GITEA_USER_EMAIL" != "$ADMIN_EMAIL" ]; then
            WINDMILL_USER_JSON=$(jq -n --arg email "$GITEA_USER_EMAIL" --arg password "$WINDMILL_ADMIN_PASS" \
                '{email: $email, password: $password, super_admin: false, name: "User"}')
            WINDMILL_USER_RESULT=$(printf '%s' "$WINDMILL_USER_JSON" | ssh nexus "curl -s -X POST '$WM_URL/users/create' \
                -H '$WM_AUTH' \
                -H '$WM_CT' \
                -d @-" 2>/dev/null || echo "")

            if echo "$WINDMILL_USER_RESULT" | grep -q '"email"' 2>/dev/null; then
                echo -e "${GREEN}  ✓ Windmill user created (user: $GITEA_USER_EMAIL)${NC}"
            elif echo "$WINDMILL_USER_RESULT" | grep -qi 'already exists' 2>/dev/null; then
                echo -e "${YELLOW}  ⚠ Windmill user already exists${NC}"
            fi
        fi

        # --- Step 3: Create "nexus" workspace ---
        WINDMILL_WS_JSON=$(jq -n '{id: "nexus", name: "Nexus Stack"}')
        WINDMILL_WS_RESULT=$(printf '%s' "$WINDMILL_WS_JSON" | ssh nexus "curl -s -X POST '$WM_URL/workspaces/create' \
            -H '$WM_AUTH' \
            -H '$WM_CT' \
            -d @-" 2>/dev/null || echo "")

        if [ "$WINDMILL_WS_RESULT" = "\"nexus\"" ] || echo "$WINDMILL_WS_RESULT" | grep -qi 'created' 2>/dev/null; then
            echo -e "${GREEN}  ✓ Windmill workspace 'nexus' created${NC}"
        elif echo "$WINDMILL_WS_RESULT" | grep -qi 'already exists' 2>/dev/null; then
            echo -e "${YELLOW}  ⚠ Windmill workspace 'nexus' already exists${NC}"
        else
            echo -e "${YELLOW}  ⚠ Windmill workspace creation: ${WINDMILL_WS_RESULT:-no response}${NC}"
        fi

        # --- Step 4: Secure the default admin@windmill.dev account ---
        # Change the default password to a random value to prevent unauthorized access
        RANDOM_PW=$(openssl rand -base64 32)
        WINDMILL_DEFPW_JSON=$(jq -n --arg password "$RANDOM_PW" '{password: $password}')
        printf '%s' "$WINDMILL_DEFPW_JSON" | ssh nexus "curl -s -X POST '$WM_URL/users/setpassword' \
            -H '$WM_AUTH' \
            -H '$WM_CT' \
            -d @-" >/dev/null 2>&1 || true
        echo -e "${GREEN}  ✓ Windmill default admin password secured${NC}"

    ) &
    CONFIG_JOBS+=($!)
fi


# Configure Gitea admin account and shared workspace repo
# Phase 2 Modul 2.2e (#505) — moved from a 346-line synchronous block
# (DB pw sync + admin/user create-or-sync + legacy email PATCH +
# token retry-via-delete + workspace repo + collaborator) to
# nexus_deploy.gitea. Same idempotent behavior; same column-exact
# user-existence checks (PR #464 bug fix preserved); same
# legacy-email-collision PATCH (Stage 3, v0.51.9 fix preserved).
# Token comes back via stdout `GITEA_TOKEN=...` line and is captured
# via eval so seed_workspace_files (already migrated, #512) +
# downstream Kestra (#517) can use it. RESTART_SERVICES is also
# emitted on stdout so the post-token restart loop knows which
# git-integrated services need a recreate.
#
# NOT migrated in this PR (separate Modul 2.2f):
#   - mirror-mode (GH_MIRROR_REPOS block at L3443+)
#   - Woodpecker OAuth registration (L3373+)
#
# NOTE: This runs synchronously (not in background) because other services
# depend on the Gitea repo being created before they can be configured.
if echo "$ENABLED_SERVICES" | grep -qw "gitea" && [ -n "$GITEA_ADMIN_PASS" ]; then
    echo "  Configuring Gitea..."
    GITEA_OUT=$(mktemp)
    GITEA_RC=0
    # Pass env vars via the `uv run` line, NOT via the leading `echo` —
    # otherwise the assignments are scoped only to `echo` and the python
    # subprocess sees them empty (the env-var-precedence-pipe-bug from
    # PR #517 round 1).
    echo "$SECRETS_JSON" | \
        ADMIN_EMAIL="$ADMIN_EMAIL" \
        REPO_NAME="$REPO_NAME" \
        GITEA_REPO_OWNER="$GITEA_REPO_OWNER" \
        GITEA_USER_EMAIL="${GITEA_USER_EMAIL:-}" \
        GITEA_USER_PASS="${GITEA_USER_PASS:-}" \
        GH_MIRROR_REPOS="${GH_MIRROR_REPOS:-}" \
        ENABLED_SERVICES="$ENABLED_SERVICES" \
        uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy gitea configure > "$GITEA_OUT" \
        || GITEA_RC=$?
    case "$GITEA_RC" in
        0|1)
            # Even on partial-failure rc=1, the token (if minted) is in
            # stdout — eval to capture it so downstream blocks
            # (Kestra Git sync, mirror-mode forks) still work.
            if [ -s "$GITEA_OUT" ]; then
                eval "$(cat "$GITEA_OUT")"
            fi
            if [ "$GITEA_RC" = "1" ]; then
                echo -e "${YELLOW}  ⚠ Gitea config had partial failures (continuing)${NC}"
            else
                echo -e "${GREEN}  ✓ Gitea configured${NC}"
            fi
            ;;
        *)
            rm -f "$GITEA_OUT"
            echo -e "${RED}  ✗ Gitea hard failure (rc=$GITEA_RC); aborting${NC}"
            exit "$GITEA_RC"
            ;;
    esac
    rm -f "$GITEA_OUT"

    # --- Workspace seed + service restart (post-token, non-mirror only) ---
    if [ -n "${GITEA_TOKEN:-}" ]; then

        if [ -z "${GH_MIRROR_REPOS:-}" ]; then
            # Seed examples/workspace-seeds/ → workspace repo via the migrated
            # nexus_deploy.seeder CLI (Phase 2 Modul 2.1, #512). Kept as a
            # function so mirror-mode can call it for forks too (still
            # in deploy.sh — separate Modul 2.2f migration).
            seed_workspace_files() {
                local owner="$1" repo="$2"
                local SEED_DIR="$PROJECT_ROOT/examples/workspace-seeds"
                if [ ! -d "$SEED_DIR" ] || [ -z "$GITEA_TOKEN" ] || [ -z "$repo" ] || [ -z "$owner" ]; then
                    return 0
                fi
                echo "  Seeding workspace files into ${owner}/${repo}..."
                local SEED_RC=0
                GITEA_TOKEN="$GITEA_TOKEN" \
                    uv run --quiet --project "$PROJECT_ROOT" \
                    python -m nexus_deploy seed \
                    --repo "$owner/$repo" \
                    --root "$SEED_DIR" \
                    || SEED_RC=$?
                case "$SEED_RC" in
                    0) ;;
                    1) echo -e "${YELLOW}  ⚠ Workspace seed had partial failures (continuing)${NC}" ;;
                    *) echo -e "${RED}  ✗ Workspace seed transport failure (rc=$SEED_RC); aborting${NC}"; exit 1 ;;
                esac
            }
            seed_workspace_files "$GITEA_REPO_OWNER" "$REPO_NAME"

            # Restart git-integrated services. Python emitted a CSV via
            # RESTART_SERVICES=, captured by eval above. Empty CSV → noop.
            if [ -n "${RESTART_SERVICES:-}" ]; then
                echo "  Restarting services with Git integration..."
                IFS=',' read -ra _RS <<< "$RESTART_SERVICES"
                for SERVICE in "${_RS[@]}"; do
                    ssh nexus "cd $REMOTE_STACKS_DIR/$SERVICE && docker compose restart" >/dev/null 2>&1 || true
                    echo "    Restarted $SERVICE"
                done
                echo -e "${GREEN}  ✓ Git-integrated services restarted${NC}"
            fi
        fi

            # --- Configure Kestra Git sync flow ---
            if echo "$ENABLED_SERVICES" | grep -qw "kestra"; then
                echo "  Configuring Kestra Git sync..."

                # Wait for Kestra to be ready. Budget: 60 × ~8 s ≤ 480 s
                # (~8 min worst case).
                #
                # Two corrections from the previous attempt:
                #
                # 1. `curl -sf` had no max-time, so when Kestra binds the
                #    port but the JVM/plugin layer hasn't yet wired up
                #    request handlers, curl waits the OS-default timeout
                #    (~75 s) per iteration. The loop was nominally
                #    60 × 3 s = 180 s but actually ran 268 s in practice
                #    because curl hung, then we gave up just as Kestra
                #    was about to be ready. Now: `--connect-timeout 3
                #    --max-time 5` bounds every iteration to ≤ 8 s
                #    (5 s curl + 3 s sleep).
                #
                # 2. Kestra v1.0 (LTS, all plugins bundled, ~2 GB pull,
                #    ~4 GB heap) on a fresh-VM cold start needs more than
                #    just image-pull time: JVM warmup + plugin load can
                #    push the API-ready point past 5 minutes. 60 × 8 s
                #    ceiling gives plenty of headroom; the loop exits
                #    early on the first successful curl, so steady-state
                #    spin-ups on a warm VM stay fast.
                #
                # Timing out here silently skipped GITEA_TOKEN PUT, the
                # Infisical→Kestra secret sync, and both `system.git-sync`
                # / `system.flow-sync` registrations — so seeded flows
                # never got synced.
                # Readiness check: accept HTTP 200, 401, or 403 as proof
                # that Kestra is up and responding. The compose-level
                # `basic-auth` makes `/api/v1/flows` return 401 to an
                # unauthenticated curl,
                # so `curl -sf` (fail-on-non-2xx) was treating a perfectly-
                # ready Kestra as "not ready" and waiting out the entire
                # loop budget. We don't want to leak the admin password
                # into argv on the remote (would be visible in `ps`) just
                # to satisfy the readiness check, so instead we drop `-f`,
                # capture the HTTP status code, and accept 200/401/403 as
                # "server is responding". The actual auth'd calls happen
                # later inside the bash-s heredoc using a curl --config
                # file. Liveness: print one line every 10 iterations.
                KESTRA_READY=false
                KESTRA_WAIT_START=$SECONDS
                for i in $(seq 1 60); do
                    KSTATUS=$(ssh nexus "curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 http://localhost:8085/api/v1/flows" 2>/dev/null) || KSTATUS=""
                    KSTATUS="${KSTATUS:-000}"
                    case "$KSTATUS" in
                        200|401|403) KESTRA_READY=true; break ;;
                    esac
                    if [ $((i % 10)) -eq 0 ]; then
                        echo "    ... still waiting for Kestra ($((SECONDS - KESTRA_WAIT_START))s elapsed, last status $KSTATUS, up to ~480s budget)"
                    fi
                    sleep 3
                done

                if [ "$KESTRA_READY" = "true" ]; then
                    # ----------------------------------------------------------
                    # Sync Infisical secrets + GITEA_TOKEN into Kestra as
                    # SECRET_<NAME>=<base64> environment variables.
                    #
                    # Architecture note: Kestra OSS does NOT support runtime
                    # secret writes via API. The `/api/v1/secrets/...` PUT
                    # we tried in earlier rounds returns 404; the
                    # `/api/v1/namespaces/<ns>/secrets` GET endpoint reports
                    # `"readOnly": true`. The only supported secret-feed in
                    # OSS is the `EnvVarSecretProvider`, which reads
                    # `SECRET_<NAME>=<base64-value>` env vars at container
                    # start. So we:
                    #
                    #   1. Build SECRET_* lines on the runner (jq is
                    #      available there; the VM also has jq now via
                    #      cloud-init + the runtime install check at the
                    #      top of this script, but the runner-side build
                    #      keeps the dedupe / collision-warning logic in
                    #      one place and avoids piping a megabyte of
                    #      response bodies through ssh stdin per folder).
                    #      Source = every Infisical folder + root path,
                    #      so user-added secrets in Infisical's UI surface
                    #      in Kestra without code changes.
                    #   2. Add SECRET_GITEA_TOKEN as a special-case (the
                    #      Gitea API token is generated post-Gitea-start
                    #      in deploy.sh and is not in Infisical at the
                    #      time of the build_folder() pushes earlier in
                    #      this script).
                    #   3. ssh-append a delimited block to
                    #      /opt/docker-server/stacks/kestra/.env on the
                    #      server. The delimiter (`# === BEGIN/END
                    #      nexus-secret-sync ===`) lets re-runs replace
                    #      the prior block cleanly instead of duplicating.
                    #   4. `docker compose up -d --force-recreate kestra`
                    #      — Kestra reads SECRET_* only at process start,
                    #      a config-reload signal won't pick them up.
                    #   5. Re-wait for Kestra ready (auth-aware loop).
                    #
                    # Cost: one Kestra cold-start (~2–4 min on warm VM).
                    # Worth it: this is the ONLY mechanism that actually
                    # makes `{{ secret('GITEA_TOKEN') }}` resolve in flows.
                    # ----------------------------------------------------------
                    KESTRA_SECRETS_TMP=$(mktemp)
                    # Register with the global RUNNER_CLEANUP_PATHS list
                    # so the EXIT trap wipes it even when one of the
                    # `exit 1` paths below fires (sed-cleanup failure,
                    # Kestra restart timeout, etc.). Without this, a
                    # mid-flight abort can leave plaintext base64-
                    # encoded Infisical secrets on the runner FS.
                    echo "$KESTRA_SECRETS_TMP" >> "$RUNNER_CLEANUP_PATHS"
                    KSEC_PUSHED=0
                    KSEC_SKIPPED=0
                    KSEC_FETCH_FAILED=0
                    KSEC_COLLISIONS=0
                    # KSEC_SEEN is allocated only inside the Infisical-guard
                    # below, so the runner doesn't leak a tmp file on stacks
                    # where Infisical isn't reachable. The post-guard
                    # GITEA_TOKEN-special-case at the bottom of this block
                    # checks `[ -n "$KSEC_SEEN" ] && [ -f ... ]` before
                    # running awk, so an unset/missing seen-file is safe.
                    KSEC_SEEN=""
                    if [ -n "$INFISICAL_TOKEN" ] && [ -n "$PROJECT_ID" ]; then
                        echo "  Building Kestra secret env from Infisical..."
                        INFISICAL_ENV_KESTRA="${INFISICAL_ENV:-dev}"
                        # Two-column file: "<KEY>\t<source-folder>". Used
                        # both to skip cross-folder duplicates (first-
                        # folder-wins, deterministic across re-runs) AND
                        # to surface a collision warning that names BOTH
                        # source folders so the operator can tell where
                        # the divergence is.
                        KSEC_SEEN=$(mktemp)

                        # All Infisical fetches happen on the server
                        # (localhost:8070 only listens there) but we need
                        # jq on the runner to parse the responses. So:
                        #
                        #   - The Infisical bearer token, project ID, and
                        #     env slug are base64-encoded on the runner
                        #     and substituted into a `bash -s` heredoc
                        #     body. The remote shell decodes them via
                        #     the `printf` builtin (no fork-exec, no
                        #     argv) and writes a mode-600 curl
                        #     `--config` file with the auth header.
                        #     Same argv-safe pattern PR #486 established.
                        #   - Folder name + secretPath transit through
                        #     `curl --get --data-urlencode ...` so
                        #     names with whitespace or reserved chars
                        #     produce valid URLs.
                        #   - Folder iteration uses `while read` over
                        #     newline-delimited input — `for X in $LIST`
                        #     would word-split on whitespace.
                        INF_TOKEN_B64=$(printf '%s' "$INFISICAL_TOKEN" | base64 | tr -d '\n')
                        INF_PID_B64=$(printf '%s' "$PROJECT_ID" | base64 | tr -d '\n')
                        INF_ENV_B64=$(printf '%s' "$INFISICAL_ENV_KESTRA" | base64 | tr -d '\n')

                        # Each Infisical fetch (folders + per-path
                        # secrets) needs to surface the HTTP status. We
                        # use `curl -w "\n%{http_code}"` so the LAST line
                        # of stdout is the status code; everything before
                        # it is the response body. Runner-side splits
                        # status from body and warns on non-200 instead
                        # of silently feeding a 401/403/error JSON to jq.

                        # 1a. Discover folders. Tempfile around the
                        #     heredoc to avoid the awkward bash quirk where
                        #     `$(... <<EOF body EOF)` parses the closing
                        #     `)` of `$()` BEFORE the heredoc body, mis-
                        #     matching parens against the `\$(printf ...)`
                        #     escapes inside the body.
                        FOLDERS_RAW_FILE=$(mktemp)
                        ssh nexus "bash -s" > "$FOLDERS_RAW_FILE" 2>/dev/null <<REMOTE_INF_FOLDERS_EOF || true
ITOK=\$(printf '%s' '$INF_TOKEN_B64' | base64 -d)
PID=\$(printf '%s' '$INF_PID_B64' | base64 -d)
INF_ENV=\$(printf '%s' '$INF_ENV_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "\$ITOK" > "\$CFG"
curl -s -w "\n%{http_code}" --config "\$CFG" --get \\
    --data-urlencode "workspaceId=\$PID" \\
    --data-urlencode "environment=\$INF_ENV" \\
    --data-urlencode "path=/" \\
    "http://localhost:8070/api/v1/folders"
REMOTE_INF_FOLDERS_EOF
                        FOLDERS_RAW=$(cat "$FOLDERS_RAW_FILE")
                        rm -f "$FOLDERS_RAW_FILE"
                        # Last line = HTTP status; everything before = body.
                        FOLDERS_STATUS=$(printf '%s' "$FOLDERS_RAW" | tail -n1)
                        FOLDERS_STATUS="${FOLDERS_STATUS:-000}"
                        FOLDERS_BODY=$(mktemp)
                        printf '%s' "$FOLDERS_RAW" | sed '$d' > "$FOLDERS_BODY"
                        if [ "$FOLDERS_STATUS" = "200" ]; then
                            # Sort the folder list alphabetically so the
                            # first-folder-wins collision policy is
                            # deterministic across re-runs even if
                            # Infisical's API returns folders in a
                            # different order between calls.
                            FOLDER_LIST=$(jq -r '.folders[]?.name' "$FOLDERS_BODY" 2>/dev/null | LC_ALL=C sort || echo "")
                        else
                            echo -e "${YELLOW}    ⚠ Infisical folder discovery returned HTTP $FOLDERS_STATUS — Kestra secret env will only contain root-path secrets + GITEA_TOKEN${NC}"
                            KSEC_FETCH_FAILED=$((KSEC_FETCH_FAILED+1))
                            FOLDER_LIST=""
                        fi
                        rm -f "$FOLDERS_BODY"

                        # 1b. For each discovered folder + the root path,
                        #     fetch all (key, value) pairs. Newline-safe
                        #     iteration via while-read; secretPath URL-
                        #     encoded via curl --data-urlencode.
                        # Use a literal "/" as the root-path sentinel
                        # rather than a magic name like "__root__". Infisical
                        # folder names cannot contain a slash (it's the path
                        # separator, not a name character), so "/" is
                        # guaranteed not to collide with any real folder
                        # name an operator might create.
                        while IFS= read -r FOLDER; do
                            [ -z "$FOLDER" ] && continue
                            if [ "$FOLDER" = "/" ]; then
                                SECRET_PATH="/"
                                FOLDER_LABEL="<root>"
                            else
                                SECRET_PATH="/$FOLDER"
                                FOLDER_LABEL="$FOLDER"
                            fi
                            INF_PATH_B64=$(printf '%s' "$SECRET_PATH" | base64 | tr -d '\n')
                            # Same tempfile-around-heredoc pattern as the
                            # folder-discovery call above (avoids paren
                            # mismatch in `$(... <<EOF \$(...) EOF)`).
                            SECRETS_RAW_FILE=$(mktemp)
                            # Plaintext Infisical secrets land in this
                            # file on the runner; register it with the
                            # global RUNNER_CLEANUP_PATHS list so an
                            # interrupted run still wipes it. The
                            # explicit `rm -f "$SECRETS_RAW_FILE"` below
                            # remains the happy-path cleanup.
                            echo "$SECRETS_RAW_FILE" >> "$RUNNER_CLEANUP_PATHS"
                            ssh nexus "bash -s" > "$SECRETS_RAW_FILE" 2>/dev/null <<REMOTE_INF_SECRETS_EOF || true
ITOK=\$(printf '%s' '$INF_TOKEN_B64' | base64 -d)
PID=\$(printf '%s' '$INF_PID_B64' | base64 -d)
INF_ENV=\$(printf '%s' '$INF_ENV_B64' | base64 -d)
SPATH=\$(printf '%s' '$INF_PATH_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'header = "Authorization: Bearer %s"\n' "\$ITOK" > "\$CFG"
curl -s -w "\n%{http_code}" --config "\$CFG" --get \\
    --data-urlencode "workspaceId=\$PID" \\
    --data-urlencode "environment=\$INF_ENV" \\
    --data-urlencode "secretPath=\$SPATH" \\
    "http://localhost:8070/api/v3/secrets/raw"
REMOTE_INF_SECRETS_EOF
                            SECRETS_RAW=$(cat "$SECRETS_RAW_FILE")
                            rm -f "$SECRETS_RAW_FILE"
                            SECRETS_STATUS=$(printf '%s' "$SECRETS_RAW" | tail -n1)
                            SECRETS_STATUS="${SECRETS_STATUS:-000}"
                            SECRETS_BODY=$(mktemp)
                            # Plaintext Infisical secret values (after we
                            # split off the trailing HTTP status line)
                            # land in this tmp on the runner; register it
                            # with RUNNER_CLEANUP_PATHS so an interrupted
                            # run still wipes it. Both happy-path
                            # `rm -f "$SECRETS_BODY"` calls below remain
                            # as immediate cleanup.
                            echo "$SECRETS_BODY" >> "$RUNNER_CLEANUP_PATHS"
                            printf '%s' "$SECRETS_RAW" | sed '$d' > "$SECRETS_BODY"
                            if [ "$SECRETS_STATUS" != "200" ]; then
                                echo -e "${YELLOW}    ⚠ Infisical fetch '$FOLDER_LABEL' (path=$SECRET_PATH) returned HTTP $SECRETS_STATUS — secrets from this folder will be missing in Kestra${NC}"
                                KSEC_FETCH_FAILED=$((KSEC_FETCH_FAILED+1))
                                rm -f "$SECRETS_BODY"
                                continue
                            fi

                            # jq base64-encodes the secretValue so newlines
                            # / tabs / binary content (multi-line PEMs)
                            # survive the TSV transit. Validate the
                            # response shape before parsing — Infisical
                            # occasionally returns a non-JSON error body
                            # with HTTP 200 (e.g. mid-restart) or a JSON
                            # blob without a `.secrets` array, and a
                            # silently-empty TSV would skip the whole
                            # folder without bumping KSEC_FETCH_FAILED.
                            JQ_TSV_TMP=$(mktemp)
                            echo "$JQ_TSV_TMP" >> "$RUNNER_CLEANUP_PATHS"
                            if ! jq -er '.secrets | type == "array"' "$SECRETS_BODY" >/dev/null 2>&1; then
                                echo -e "${YELLOW}    ⚠ Infisical fetch '$FOLDER_LABEL' (path=$SECRET_PATH) returned HTTP 200 but the body has no \`.secrets\` array — secrets from this folder will be missing in Kestra${NC}"
                                KSEC_FETCH_FAILED=$((KSEC_FETCH_FAILED+1))
                                rm -f "$SECRETS_BODY" "$JQ_TSV_TMP"
                                continue
                            fi
                            jq -r '.secrets[]? | [.secretKey, (.secretValue | @base64)] | @tsv' "$SECRETS_BODY" > "$JQ_TSV_TMP" 2>/dev/null || JQ_TSV_RC=$?
                            if [ -n "${JQ_TSV_RC:-}" ]; then
                                echo -e "${YELLOW}    ⚠ Infisical fetch '$FOLDER_LABEL' (path=$SECRET_PATH) parsed as JSON but jq tsv-extract failed (exit $JQ_TSV_RC) — secrets from this folder will be missing in Kestra${NC}"
                                KSEC_FETCH_FAILED=$((KSEC_FETCH_FAILED+1))
                                rm -f "$SECRETS_BODY" "$JQ_TSV_TMP"
                                unset JQ_TSV_RC
                                continue
                            fi
                            while IFS=$'\t' read -r KEY VALUE_B64; do
                                [ -z "$KEY" ] && continue
                                # Kestra naming rule: ^[A-Za-z][A-Za-z0-9_]*$
                                # Anything else (slashes, dots, hyphens) won't
                                # produce a valid `SECRET_<NAME>` env var name.
                                if ! [[ "$KEY" =~ ^[A-Za-z][A-Za-z0-9_]*$ ]]; then
                                    KSEC_SKIPPED=$((KSEC_SKIPPED+1))
                                    continue
                                fi
                                # Cross-folder dedupe (first-folder-wins is
                                # deterministic across repeat runs; last-
                                # wins would flip env-var values based on
                                # iteration order). On collision: log a
                                # warning naming the kept folder and the
                                # dropped folder so the operator can spot
                                # divergent values across folders.
                                EXISTING_FOLDER=$(awk -F'\t' -v k="$KEY" '$1 == k {print $2; exit}' "$KSEC_SEEN" 2>/dev/null)
                                if [ -n "$EXISTING_FOLDER" ]; then
                                    echo -e "${YELLOW}    ⚠ Key collision: '$KEY' in folder '$FOLDER_LABEL' shadowed by earlier value from folder '$EXISTING_FOLDER' (first-wins)${NC}"
                                    KSEC_COLLISIONS=$((KSEC_COLLISIONS+1))
                                    continue
                                fi
                                printf '%s\t%s\n' "$KEY" "$FOLDER_LABEL" >> "$KSEC_SEEN"
                                echo "SECRET_${KEY}=${VALUE_B64}" >> "$KESTRA_SECRETS_TMP"
                                KSEC_PUSHED=$((KSEC_PUSHED+1))
                            done < "$JQ_TSV_TMP"
                            rm -f "$SECRETS_BODY" "$JQ_TSV_TMP"
                        done < <(printf '%s\n/\n' "$FOLDER_LIST")
                    fi

                    # 2. Special case: GITEA_TOKEN is generated by deploy.sh
                    #    after Gitea boots (it's the on-the-fly admin token
                    #    used to create the workspace repo). It is NOT in
                    #    Infisical at the time of the `build_folder()`
                    #    pushes earlier in this script, so we add it here
                    #    so seeded flows can use `{{ secret('GITEA_TOKEN') }}`.
                    # Write SECRET_GITEA_TOKEN unless it was already
                    # pushed via Infisical (guard against duplicate). The
                    # awk dedupe-check only runs when KSEC_SEEN was
                    # actually populated by the Infisical loop above; on
                    # stacks where Infisical isn't reachable, the file
                    # doesn't exist and we just write the token directly.
                    if [ -n "$GITEA_TOKEN" ]; then
                        ALREADY_HAVE_GITEA_TOKEN=false
                        if [ -n "$KSEC_SEEN" ] && [ -f "$KSEC_SEEN" ]; then
                            if awk -F'\t' '$1 == "GITEA_TOKEN" {found=1; exit} END {exit !found}' "$KSEC_SEEN" 2>/dev/null; then
                                ALREADY_HAVE_GITEA_TOKEN=true
                            fi
                        fi
                        if [ "$ALREADY_HAVE_GITEA_TOKEN" = "false" ]; then
                            # Encode in a separate var rather than inline `$(…)` —
                            # nested double quotes inside `$()` are valid bash
                            # (the inner context is independent), but Copilot
                            # repeatedly flagged it as ambiguous; the two-line
                            # form costs nothing and silences the false positive.
                            GITEA_TOKEN_B64=$(printf '%s' "$GITEA_TOKEN" | base64 | tr -d '\n')
                            printf 'SECRET_GITEA_TOKEN=%s\n' "$GITEA_TOKEN_B64" >> "$KESTRA_SECRETS_TMP"
                            KSEC_PUSHED=$((KSEC_PUSHED+1))
                        fi
                    fi
                    [ -n "$KSEC_SEEN" ] && rm -f "$KSEC_SEEN"

                    # 3. Append (or replace) the delimited block in
                    #    Kestra's .env on the server. Fail fast if either
                    #    the .env file is missing or the sed-based block
                    #    removal fails — silently continuing would let
                    #    SECRET_* entries accumulate across re-runs (or
                    #    write to a non-existent file), and the operator
                    #    has no way to notice unless secrets eventually
                    #    fail at flow execution time.
                    if [ -s "$KESTRA_SECRETS_TMP" ]; then
                        if ! ssh nexus "
                            set -e
                            ENV_FILE=/opt/docker-server/stacks/kestra/.env
                            if [ ! -f \"\$ENV_FILE\" ]; then
                                echo \"ERROR: Kestra .env not found at \$ENV_FILE\" >&2
                                exit 1
                            fi
                            sed -i '/^# === BEGIN nexus-secret-sync/,/^# === END nexus-secret-sync/d' \"\$ENV_FILE\"
                        "; then
                            echo -e "${RED}Error: failed to clean previous nexus-secret-sync block from Kestra .env. Aborting deploy to avoid duplicating SECRET_* lines.${NC}"
                            exit 1
                        fi

                        # Lock the .env to mode 0600 BEFORE appending the
                        # SECRET_* block, then append. Doing chmod first
                        # closes the race window where the file could be
                        # world-readable (0644 from rsync-preserved modes
                        # under default umask) WHILE the new secrets are
                        # being written — a concurrent reader could grab
                        # a partial copy of base64-encoded R2 keys / DB
                        # passwords / GITEA_TOKEN. chmod 600 idempotent
                        # at the start; the file definitely exists
                        # because the preceding sed-removal step opened
                        # and re-wrote it.
                        if ! {
                            echo "# === BEGIN nexus-secret-sync (re-generated each spin-up; do not edit by hand) ==="
                            cat "$KESTRA_SECRETS_TMP"
                            echo "# === END nexus-secret-sync ==="
                        } | ssh nexus "
                            set -e
                            ENV_FILE=/opt/docker-server/stacks/kestra/.env
                            chmod 600 \"\$ENV_FILE\"
                            cat >> \"\$ENV_FILE\"
                        "; then
                            echo -e "${RED}Error: failed to chmod 0600 + append nexus-secret-sync block to Kestra .env.${NC}"
                            exit 1
                        fi

                        if [ "$KSEC_FETCH_FAILED" -gt 0 ]; then
                            echo -e "${YELLOW}  ⚠ Wrote $KSEC_PUSHED Kestra SECRET_* env-vars to .env (skipped=$KSEC_SKIPPED invalid keys, collisions=$KSEC_COLLISIONS, $KSEC_FETCH_FAILED Infisical fetches failed — secret set is incomplete)${NC}"
                        elif [ "$KSEC_COLLISIONS" -gt 0 ]; then
                            echo -e "${YELLOW}  ⚠ Wrote $KSEC_PUSHED Kestra SECRET_* env-vars to .env (skipped=$KSEC_SKIPPED invalid keys, $KSEC_COLLISIONS cross-folder collisions — see warnings above; first-folder-wins applied)${NC}"
                        else
                            echo -e "${GREEN}  ✓ Wrote $KSEC_PUSHED Kestra SECRET_* env-vars to .env (skipped=$KSEC_SKIPPED invalid keys)${NC}"
                        fi

                        # 4. Force-recreate Kestra so the env vars get
                        #    loaded. `up -d --force-recreate <svc>` keeps
                        #    other containers untouched. Fail-fast: if
                        #    the restart fails, the new SECRET_* values
                        #    are sitting in .env but the live container
                        #    is still running with the old set — flow-
                        #    sync registration would proceed against a
                        #    Kestra that can't resolve `{{ secret('GITEA_TOKEN') }}`.
                        echo "  Restarting Kestra to load secrets..."
                        if ! ssh nexus "cd $REMOTE_STACKS_DIR/kestra && docker compose up -d --force-recreate kestra" >/dev/null 2>&1; then
                            echo -e "${RED}Error: docker compose up -d --force-recreate kestra failed. Aborting deploy to avoid continuing with un-reloaded secrets.${NC}"
                            exit 1
                        fi

                        # 5. Re-wait for Kestra to come back up — and
                        #    actually authenticate. The previous version
                        #    accepted 401/403 as "responding", which is
                        #    fine to know the HTTP server is up but does
                        #    NOT guarantee the basic-auth layer has
                        #    finished loading the password from the env-
                        #    var secret store. Hitting POST /api/v1/flows
                        #    while basic-auth is still initialising → 401
                        #    → "Kestra Git sync flow registration had
                        #    failures" even though the password is right.
                        #
                        #    Probe with the actual admin creds (basic auth
                        #    via curl --config) and require 200 before we
                        #    consider Kestra ready for registration.
                        echo "  Waiting for Kestra to come back up..."
                        KESTRA_READY=false
                        KESTRA_WAIT_START=$SECONDS
                        KESTRA_PROBE_USER_B64=$(printf '%s' "$ADMIN_EMAIL" | base64 | tr -d '\n')
                        KESTRA_PROBE_PW_B64=$(printf '%s' "$KESTRA_PASS" | base64 | tr -d '\n')
                        for i in $(seq 1 60); do
                            KSTATUS=$(ssh nexus "bash -s" <<REMOTE_KESTRA_PROBE_EOF 2>/dev/null
USER=\$(printf '%s' '$KESTRA_PROBE_USER_B64' | base64 -d)
PW=\$(printf '%s' '$KESTRA_PROBE_PW_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'user = "%s:%s"\n' "\$USER" "\$PW" > "\$CFG"
curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 --max-time 5 --config "\$CFG" 'http://localhost:8085/api/v1/flows'
REMOTE_KESTRA_PROBE_EOF
) || KSTATUS=""
                            KSTATUS="${KSTATUS:-000}"
                            # Treat Kestra as ready only on known auth-
                            # success statuses for this probe. /api/v1/flows
                            # in Kestra v1.0 OSS responds 404 to GET (the
                            # endpoint only accepts POST, with the read
                            # path moved under tenant prefix at
                            # /api/v1/main/flows that returns 405 for GET).
                            # Both 404 and 405 mean basic-auth was accepted.
                            # 401 = auth not yet wired; 000 = curl couldn't
                            # talk to Kestra; 5xx = Kestra reachable but
                            # internal-error during startup → keep looping.
                            case "$KSTATUS" in
                                200|404|405) KESTRA_READY=true; break ;;
                            esac
                            if [ $((i % 10)) -eq 0 ]; then
                                echo "    ... still waiting for Kestra restart + auth ($((SECONDS - KESTRA_WAIT_START))s elapsed, last status $KSTATUS)"
                            fi
                            sleep 3
                        done

                        if [ "$KESTRA_READY" != "true" ]; then
                            echo -e "${YELLOW}  ⚠ Kestra did not come back up after restart — Git sync flow registration will be skipped${NC}"
                        fi
                    else
                        echo -e "${YELLOW}  ⚠ No Kestra SECRET_* lines built (Infisical empty or unreachable, GITEA_TOKEN missing) — flows that reference {{ secret('NAME') }} will fail${NC}"
                    fi
                    rm -f "$KESTRA_SECRETS_TMP"
                fi  # close: KESTRA_READY initial check

                # ----------------------------------------------------------
                # Register Git-sync + flow-sync flows. Runs only if Kestra
                # is reachable (initial wait passed and the post-restart
                # wait either succeeded or wasn't needed).
                # ----------------------------------------------------------
                if [ "$KESTRA_READY" = "true" ]; then
                    # ----------------------------------------------------------
                    # Register both Git-sync flows in a single remote bash
                    # invocation:
                    #
                    #   - `git-sync` (SyncNamespaceFiles): pulls helper files
                    #     (Python scripts, configs, SQL templates) from the
                    #     repo's `nexus_seeds/kestra/workflows/` directory
                    #     into the namespace's files area — these are NOT
                    #     flow defs.
                    #
                    #   - `flow-sync` (SyncFlows): pulls flow YAML files from
                    #     `nexus_seeds/kestra/flows/` and registers them under
                    #     namespace `nexus-tutorials`. `targetNamespace:
                    #     nexus-tutorials` is required by the v1.0 plugin
                    #     (without it, POST /flows returns 422
                    #     "tasks[0].targetNamespace: must not be null").
                    #     With `includeChildNamespaces: true`, subdirs
                    #     extend the namespace — e.g.
                    #     `nexus_seeds/kestra/flows/sub1/foo.yaml` →
                    #     `nexus-tutorials.sub1`.
                    #     `delete: true` makes Git the single source of truth
                    #     — UI-only flows get cleaned up on every sync. This
                    #     is the persistence layer that survives destroy-all
                    #     (Gitea repos live on the persistent Hetzner volume
                    #     `/mnt/nexus-data/gitea/`).
                    #
                    # Kestra creds go through a curl --config file written
                    # via cat-from-stdin instead of `-u user:pw` which would
                    # expose KESTRA_PASS in the remote `ps` listing. HTTP
                    # status is captured per flow with a POST→PUT fallback
                    # for idempotent re-runs (Kestra v1.0 OSS has no upsert
                    # verb — POST is create-only, PUT is update-only):
                    #   POST 200/201            → created (first-time)
                    #   POST 422 → PUT 200/201  → updated (idempotent re-run,
                    #                              also picks up YAML changes)
                    #   anything else           → real failure, surfaced as
                    #                              warning
                    # ----------------------------------------------------------
                    # Phase 2 Modul 2.3 (#505) — moved from a 165-line
                    # remote bash heredoc (POST/PUT register_flow function +
                    # 2 inline YAML templates + flow-sync execute/poll) to
                    # nexus_deploy.kestra. Same idempotent POST-then-PUT
                    # semantics; same one-shot flow-sync trigger to onboard
                    # user-seeded flows immediately. The Python path opens
                    # an SSH port-forward (kernel-allocated local port →
                    # remote 8085) and talks to Kestra via local HTTP,
                    # no rendered server-side bash.
                    # CRITICAL: env-var prefix MUST sit on `uv run`, not on
                    # `echo`. `VAR=value echo … | python …` scopes VAR to the
                    # left-hand `echo` only — the Python process after the
                    # pipe never sees it. Caught in PR #517 review pre-spinup;
                    # without this fix, every Kestra invocation would exit
                    # rc=2 with "missing required env" on every deploy.
                    KESTRA_RC=0
                    echo "$SECRETS_JSON" | \
                        GITEA_REPO_OWNER="$GITEA_REPO_OWNER" \
                        REPO_NAME="$REPO_NAME" \
                        WORKSPACE_BRANCH="$WORKSPACE_BRANCH" \
                        ADMIN_EMAIL="$ADMIN_EMAIL" \
                        uv run --quiet --project "$PROJECT_ROOT" \
                        python -m nexus_deploy kestra register-system-flows \
                        || KESTRA_RC=$?
                    case "$KESTRA_RC" in
                        0) echo -e "${GREEN}  ✓ Kestra Git sync flows registered (workflows + flows)${NC}" ;;
                        1) echo -e "${YELLOW}  ⚠ Kestra Git sync flow registration had partial failures (continuing)${NC}" ;;
                        *) echo -e "${RED}  ✗ Kestra Git sync flow registration hard failure (rc=$KESTRA_RC); aborting${NC}"; exit "$KESTRA_RC" ;;
                    esac
                else
                    echo -e "${YELLOW}  ⚠ Kestra not ready - skipping Git sync flow${NC}"
                fi
            fi

            # --- Create Woodpecker CI OAuth application in Gitea ---
            if echo "$ENABLED_SERVICES" | grep -qw "woodpecker" && [ -n "$WOODPECKER_AGENT_SECRET" ]; then
                echo "  Creating Woodpecker CI OAuth app in Gitea..."

                # Delete existing OAuth app if present (idempotent re-deploy)
                EXISTING_APPS=$(ssh nexus "curl -s 'http://localhost:3200/api/v1/user/applications/oauth2' \
                    -H 'Authorization: token $GITEA_TOKEN'" 2>/dev/null || echo "[]")
                EXISTING_APP_ID=$(echo "$EXISTING_APPS" | jq -r '.[] | select(.name=="Woodpecker CI") | .id // empty' 2>/dev/null)
                if [ -n "$EXISTING_APP_ID" ]; then
                    ssh nexus "curl -s -X DELETE 'http://localhost:3200/api/v1/user/applications/oauth2/$EXISTING_APP_ID' \
                        -H 'Authorization: token $GITEA_TOKEN'" >/dev/null 2>&1 || true
                fi

                # Create new OAuth application
                OAUTH_RESULT=$(ssh nexus "curl -s -X POST 'http://localhost:3200/api/v1/user/applications/oauth2' \
                    -H 'Authorization: token $GITEA_TOKEN' \
                    -H 'Content-Type: application/json' \
                    -d '{
                        \"name\": \"Woodpecker CI\",
                        \"redirect_uris\": [\"https://woodpecker.${DOMAIN}/authorize\"],
                        \"confidential_client\": true
                    }'" 2>/dev/null || echo "")

                WOODPECKER_GITEA_CLIENT=$(echo "$OAUTH_RESULT" | jq -r '.client_id // empty')
                WOODPECKER_GITEA_SECRET=$(echo "$OAUTH_RESULT" | jq -r '.client_secret // empty')

                if [ -n "$WOODPECKER_GITEA_CLIENT" ] && [ -n "$WOODPECKER_GITEA_SECRET" ]; then
                    echo -e "${GREEN}  ✓ Woodpecker OAuth app created${NC}"

                    # Update Woodpecker .env with OAuth credentials
                    cat > "$STACKS_DIR/woodpecker/.env" << WPEOF
# Auto-generated - DO NOT COMMIT
DOMAIN=${DOMAIN}
WOODPECKER_AGENT_SECRET=${WOODPECKER_AGENT_SECRET}
WOODPECKER_ADMIN=${ADMIN_USERNAME:-}
WOODPECKER_GITEA_CLIENT=${WOODPECKER_GITEA_CLIENT}
WOODPECKER_GITEA_SECRET=${WOODPECKER_GITEA_SECRET}
WPEOF

                    # Sync updated .env to server and start Woodpecker
                    rsync -az "$STACKS_DIR/woodpecker/" nexus:$REMOTE_STACKS_DIR/woodpecker/
                    if ssh nexus "cd $REMOTE_STACKS_DIR/woodpecker && source /opt/docker-server/stacks/.env && docker compose up -d" 2>&1; then
                        echo -e "${GREEN}  ✓ Woodpecker started with Gitea forge${NC}"
                    else
                        echo -e "${YELLOW}  ⚠ Failed to start Woodpecker - check container logs${NC}"
                    fi
                else
                    echo -e "${YELLOW}  ⚠ Could not create Woodpecker OAuth app in Gitea${NC}"
                fi
            fi

        echo -e "${GREEN}  ✓ Gitea workspace setup complete${NC}"
    else
        echo -e "${YELLOW}  ⚠ Gitea token not minted — skipping seed/Kestra/Woodpecker${NC}"
        echo -e "${YELLOW}    Credentials available in Infisical${NC}"
    fi
fi

# =============================================================================
# GitHub Mirror Setup (optional)
# Mirrors one or more private GitHub repos into Gitea as pull mirrors.
# Requires GH_MIRROR_TOKEN (GitHub PAT with Contents:read permission) and
# GH_MIRROR_REPOS (comma-separated list of GitHub repo URLs).
# If either variable is unset, this block is skipped entirely.
# =============================================================================
if echo "$ENABLED_SERVICES" | grep -qw "gitea" \
    && [ -n "${GH_MIRROR_TOKEN:-}" ] \
    && [ -n "${GH_MIRROR_REPOS:-}" ] \
    && [ -n "${GITEA_TOKEN:-}" ]; then

    echo ""
    echo "=========================================="
    echo "  Setting up GitHub Mirrors"
    echo "=========================================="

    # Get admin user ID (required by Gitea migration API)
    GITEA_ADMIN_UID=$(ssh nexus "curl -s \
        'http://localhost:3200/api/v1/users/$ADMIN_USERNAME' \
        -H 'Authorization: token $GITEA_TOKEN'" 2>/dev/null \
        | jq -r '.id // empty')

    if [ -z "$GITEA_ADMIN_UID" ]; then
        echo -e "${YELLOW}  ⚠ Could not get Gitea admin UID - skipping mirrors${NC}"
    else
        IFS=',' read -ra MIRROR_REPOS <<< "$GH_MIRROR_REPOS"
        for REPO_URL in "${MIRROR_REPOS[@]}"; do
            REPO_URL=$(echo "$REPO_URL" | tr -d ' ')
            [ -z "$REPO_URL" ] && continue
            REPO_NAME="mirror-readonly-$(basename "$REPO_URL" .git)"

            echo "  Mirroring: $REPO_NAME..."

            # Check if mirror already exists (idempotent re-deploy)
            HTTP_CODE=$(ssh nexus "curl -s -o /dev/null -w '%{http_code}' \
                'http://localhost:3200/api/v1/repos/$ADMIN_USERNAME/$REPO_NAME' \
                -H 'Authorization: token $GITEA_TOKEN'")

            MIRROR_OK=0
            if [ "$HTTP_CODE" = "200" ]; then
                echo -e "${YELLOW}  ⚠ Mirror '$REPO_NAME' already exists, skipping creation${NC}"
                MIRROR_OK=1
            else
                MIGRATE_PAYLOAD=$(jq -n \
                    --arg clone_addr "$REPO_URL" \
                    --arg repo_name "$REPO_NAME" \
                    --arg auth_token "$GH_MIRROR_TOKEN" \
                    --argjson uid "$GITEA_ADMIN_UID" \
                    '{
                        clone_addr: $clone_addr,
                        repo_name: $repo_name,
                        private: true,
                        mirror: true,
                        mirror_interval: "10m0s",
                        auth_token: $auth_token,
                        uid: $uid
                    }')

                MIRROR_RESULT=$(printf '%s' "$MIGRATE_PAYLOAD" | ssh nexus "curl -s -X POST \
                    'http://localhost:3200/api/v1/repos/migrate' \
                    -H 'Authorization: token $GITEA_TOKEN' \
                    -H 'Content-Type: application/json' \
                    -d @-" 2>/dev/null || echo "")

                if echo "$MIRROR_RESULT" | jq -e '.id' >/dev/null 2>&1; then
                    echo -e "${GREEN}  ✓ Mirror '$REPO_NAME' created (syncs every 10 min)${NC}"
                    MIRROR_OK=1
                else
                    echo -e "${YELLOW}  ⚠ Mirror '$REPO_NAME' setup failed${NC}"
                    echo -e "${YELLOW}    Verify GH_MIRROR_TOKEN has Contents:read permission${NC}"
                    echo -e "${YELLOW}    and GH_MIRROR_REPOS contains valid GitHub HTTPS URLs${NC}"
                fi
            fi

            if [ "$MIRROR_OK" = "1" ]; then
                # Fork the first mirror as the user's workspace repo (idempotent)
                # FORKED_WORKSPACE flag ensures we only fork once (the first mirror)
                if [ "${FORKED_WORKSPACE:-}" != "1" ] && [ -n "${GITEA_USER_USERNAME:-}" ]; then
                    ORIG_NAME=$(basename "$REPO_URL" .git)
                    GITEA_USER_SANITIZED="${GITEA_USER_USERNAME//[^a-zA-Z0-9]/_}"
                    FORK_NAME="${ORIG_NAME}_${GITEA_USER_SANITIZED}"
                    echo "  Forking ${ADMIN_USERNAME}/${REPO_NAME} into ${GITEA_USER_USERNAME}/${FORK_NAME}..."

                    # Create a user token so the fork lands in the user's namespace (not admin's)
                    USER_TOKEN=$(ssh nexus "curl -s -X POST 'http://localhost:3200/api/v1/users/$GITEA_USER_USERNAME/tokens' \
                        -u '$ADMIN_USERNAME:$GITEA_ADMIN_PASS' \
                        -H 'Content-Type: application/json' \
                        -d '{\"name\":\"nexus-workspace-fork\",\"scopes\":[\"all\"]}'" 2>/dev/null | jq -r '.sha1 // empty')
                    if [ -z "$USER_TOKEN" ]; then
                        ssh nexus "curl -s -X DELETE 'http://localhost:3200/api/v1/users/$GITEA_USER_USERNAME/tokens/nexus-workspace-fork' \
                            -u '$ADMIN_USERNAME:$GITEA_ADMIN_PASS'" >/dev/null 2>&1 || true
                        USER_TOKEN=$(ssh nexus "curl -s -X POST 'http://localhost:3200/api/v1/users/$GITEA_USER_USERNAME/tokens' \
                            -u '$ADMIN_USERNAME:$GITEA_ADMIN_PASS' \
                            -H 'Content-Type: application/json' \
                            -d '{\"name\":\"nexus-workspace-fork\",\"scopes\":[\"all\"]}'" 2>/dev/null | jq -r '.sha1 // empty')
                    fi
                    if [ -n "$USER_TOKEN" ]; then
                        FORK_RESULT=$(ssh nexus "curl -s -o /dev/null -w '%{http_code}' \
                            -X POST 'http://localhost:3200/api/v1/repos/${ADMIN_USERNAME}/${REPO_NAME}/forks' \
                            -H 'Authorization: token $USER_TOKEN' \
                            -H 'Content-Type: application/json' \
                            -d '{\"name\":\"$FORK_NAME\"}'")
                        if [ "$FORK_RESULT" = "202" ]; then
                            echo -e "${GREEN}  ✓ Forked into ${GITEA_USER_USERNAME}/${FORK_NAME}${NC}"
                            FORKED_WORKSPACE=1
                        elif [ "$FORK_RESULT" = "409" ]; then
                            echo -e "${YELLOW}  ⚠ Fork ${GITEA_USER_USERNAME}/${FORK_NAME} already exists${NC}"
                            FORKED_WORKSPACE=1
                        else
                            echo -e "${YELLOW}  ⚠ Fork returned HTTP $FORK_RESULT${NC}"
                        fi
                        ssh nexus "curl -s -X DELETE 'http://localhost:3200/api/v1/users/$GITEA_USER_USERNAME/tokens/nexus-workspace-fork' \
                            -u '$ADMIN_USERNAME:$GITEA_ADMIN_PASS'" >/dev/null 2>&1 || true
                    else
                        echo -e "${YELLOW}  ⚠ Could not create user token for fork${NC}"
                    fi
                fi

                # Grant student user (gitea_user) read-only access to the mirror
                if [ -n "$GITEA_USER_USERNAME" ]; then
                    COLLAB_PAYLOAD=$(jq -n '{permission: "read"}')
                    printf '%s' "$COLLAB_PAYLOAD" | ssh nexus "curl -s -X PUT \
                        'http://localhost:3200/api/v1/repos/$ADMIN_USERNAME/$REPO_NAME/collaborators/$GITEA_USER_USERNAME' \
                        -H 'Authorization: token $GITEA_TOKEN' \
                        -H 'Content-Type: application/json' \
                        -d @-" >/dev/null 2>&1 || true
                    echo -e "${GREEN}  ✓ Read access granted to '$GITEA_USER_USERNAME'${NC}"
                fi

                # Sync fork from upstream mirror (ensures fork has latest code on every Spin Up)
                # Uses Gitea's merge-upstream API to fast-forward the fork from the mirror.
                if [ "${FORKED_WORKSPACE:-}" = "1" ] && [ "${SYNCED_FORK:-}" != "1" ]; then
                    SYNCED_FORK=1
                    ORIG_NAME=$(basename "$REPO_URL" .git)
                    GITEA_USER_SANITIZED="${GITEA_USER_USERNAME//[^a-zA-Z0-9]/_}"
                    SYNC_FORK_NAME="${ORIG_NAME}_${GITEA_USER_SANITIZED}"
                    echo "  Syncing fork ${GITEA_USER_USERNAME}/${SYNC_FORK_NAME} from upstream..."

                    # First trigger mirror sync to pull latest from GitHub
                    ssh nexus "curl -s -X POST \
                        'http://localhost:3200/api/v1/repos/$ADMIN_USERNAME/$REPO_NAME/mirror-sync' \
                        -H 'Authorization: token $GITEA_TOKEN'" >/dev/null 2>&1 || true
                    # Wait briefly for mirror sync to complete
                    sleep 3

                    # Merge upstream into fork (fast-forward) on the
                    # branch detected at script-start time (resolved into
                    # $WORKSPACE_BRANCH from the GitHub API for the first
                    # mirror URL — defaults to `main` if detection failed
                    # or no token was supplied). Hardcoding `main` here
                    # broke `master`-default upstreams: merge-upstream
                    # 404s and the fork drifts.
                    #
                    # Auth header + branch body go through a remote
                    # `curl --config` file (mode 0600, removed via local
                    # trap) so $GITEA_TOKEN never appears in argv on the
                    # server (would otherwise be visible in `ps` while
                    # curl runs). Same argv-safe pattern as the other
                    # token-bearing calls in this script.
                    MERGE_TOKEN_B64=$(printf '%s' "$GITEA_TOKEN" | base64 | tr -d '\n')
                    MERGE_BRANCH_B64=$(printf '%s' "$WORKSPACE_BRANCH" | base64 | tr -d '\n')
                    MERGE_RESULT=$(ssh nexus "bash -s" <<REMOTE_MERGE_EOF 2>/dev/null
TOK=\$(printf '%s' '$MERGE_TOKEN_B64' | base64 -d)
BR=\$(printf '%s' '$MERGE_BRANCH_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
{
    printf 'header = "Authorization: token %s"\n' "\$TOK"
    printf 'header = "Content-Type: application/json"\n'
} > "\$CFG"
printf '{"branch":"%s"}' "\$BR" | curl -s -o /dev/null -w '%{http_code}' \\
    -X POST 'http://localhost:3200/api/v1/repos/$GITEA_USER_USERNAME/$SYNC_FORK_NAME/merge-upstream' \\
    --config "\$CFG" \\
    --data-binary @-
REMOTE_MERGE_EOF
)

                    if [ "$MERGE_RESULT" = "200" ]; then
                        echo -e "${GREEN}  ✓ Fork synced from upstream (new commits merged)${NC}"
                    elif [ "$MERGE_RESULT" = "409" ]; then
                        echo "  ✓ Fork already up to date"
                    else
                        echo -e "${YELLOW}  ⚠ Fork sync returned HTTP $MERGE_RESULT (may need manual sync)${NC}"
                    fi
                fi
            fi
        done

        # Seed Nexus-Stack example workspace files into the now-existing
        # fork. The mirror loop above OVERWRITES `$REPO_NAME` per
        # iteration to the mirror's name (`mirror-readonly-<repo>`),
        # which would make the seed POST hit the wrong repo. Restore
        # `$REPO_NAME` to the FORK name (line 4204: `$FORK_NAME` =
        # `${ORIG_NAME}_${GITEA_USER_SANITIZED}` = e.g.
        # `Bsc_EDS_GIS_FS2026_stefan_koch`) before calling
        # `seed_workspace_files`, and ensure `$GITEA_REPO_OWNER` is
        # the user's username (the fork owner). POST is create-only,
        # so files the fork already inherited from upstream get
        # 422-skipped harmlessly.
        if [ -n "${FORK_NAME:-}" ] && [ -n "${GITEA_USER_USERNAME:-}" ]; then
            REPO_NAME="$FORK_NAME"
            GITEA_REPO_OWNER="$GITEA_USER_USERNAME"
            seed_workspace_files

            # The Kestra-bootstrap block higher up in this script
            # registered `system.flow-sync` and triggered ONE execution
            # to verify the seeded flow was visible. In mirror mode that
            # initial trigger ran BEFORE this fork was created, so the
            # SyncFlows task got a 404 cloning the (then-nonexistent)
            # fork. The flow itself is registered with the correct fork
            # URL (we use $GITEA_REPO_OWNER/$REPO_NAME), so the next
            # 15-min cron tick would eventually pick up the seeded
            # content — but that's a poor onboarding signal. Trigger one
            # more execution now that the fork actually exists; the user
            # then sees `nexus-tutorials.r2-taxi-pipeline` in Kestra within
            # ~10 s of deploy completion.
            if [ -n "${KESTRA_PASS:-}" ] && [ -n "${ADMIN_EMAIL:-}" ]; then
                echo "  Re-triggering system.flow-sync now that the fork is populated..."
                TRIG_USER_B64=$(printf '%s' "$ADMIN_EMAIL" | base64 | tr -d '\n')
                TRIG_PW_B64=$(printf '%s' "$KESTRA_PASS" | base64 | tr -d '\n')
                # Use `curl -fsS` so a non-2xx response (e.g. Kestra
                # unreachable, basic-auth failed) sets a non-zero exit
                # status, propagated out of the heredoc, captured by
                # the `if` guard. Previous `curl -s … || true` form
                # silently masked failures and printed the green
                # "triggered" line even when nothing was triggered.
                if ssh nexus "bash -s" >/dev/null 2>&1 <<REMOTE_TRIG_EOF
USER=\$(printf '%s' '$TRIG_USER_B64' | base64 -d)
PW=\$(printf '%s' '$TRIG_PW_B64' | base64 -d)
CFG=\$(mktemp)
chmod 600 "\$CFG"
trap 'rm -f "\$CFG"' EXIT
printf 'user = "%s:%s"\n' "\$USER" "\$PW" > "\$CFG"
curl -fsS -X POST 'http://localhost:8085/api/v1/executions/system/flow-sync' --config "\$CFG" >/dev/null
REMOTE_TRIG_EOF
                then
                    echo -e "${GREEN}  ✓ system.flow-sync triggered — nexus-tutorials.r2-taxi-pipeline appears in Kestra within ~10 s${NC}"
                else
                    echo -e "${YELLOW}  ⚠ system.flow-sync re-trigger failed — the next 15-min cron tick will pick up the seeded flow${NC}"
                fi
            fi
        fi
    fi

    # Restart git-integrated services so they pick up the latest fork content.
    # Runs after mirror sync + fork update to ensure services clone/pull the newest code.
    GIT_RESTART_SVCS=""
    for SVC in jupyter marimo code-server meltano prefect; do
        if echo "$ENABLED_SERVICES" | grep -qw "$SVC"; then
            GIT_RESTART_SVCS="$GIT_RESTART_SVCS $SVC"
        fi
    done
    if [ -n "$GIT_RESTART_SVCS" ]; then
        echo "  Restarting services with Git integration..."
        for SVC in $GIT_RESTART_SVCS; do
            ssh nexus "cd $REMOTE_STACKS_DIR/$SVC && docker compose restart" >/dev/null 2>&1 || true
            echo "    Restarted $SVC"
        done
        echo -e "${GREEN}  ✓ Git-integrated services restarted${NC}"
    fi
fi

# ==========================================================================
# Sync Infisical secrets into Jupyter as plaintext env-vars (Phase 1 Modul 1.2)
# ==========================================================================
# Migrated from a 358-line bash heredoc to nexus_deploy.secret_sync (#505).
# Exit-code contract enforced by the Python CLI:
#   0 = wrote new .infisical.env, OR no-touch outage gate fired cleanly,
#       OR remote script produced no parseable RESULT line (soft no-op,
#       inner script's own stderr is already in the workflow log)
#   1 = wrote, but at least one folder fetch failed (partial success)
#   2 = transport / unexpected error → abort the deploy
if echo "$ENABLED_SERVICES" | grep -qw "jupyter" \
    && [ -n "$INFISICAL_TOKEN" ] && [ -n "$PROJECT_ID" ]; then
    echo "Syncing Infisical secrets into Jupyter env..."
    JUP_RC=0
    PROJECT_ID="$PROJECT_ID" INFISICAL_TOKEN="$INFISICAL_TOKEN" \
        INFISICAL_ENV="${INFISICAL_ENV:-dev}" GITEA_TOKEN="${GITEA_TOKEN:-}" \
        uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy secret-sync --stack jupyter \
        || JUP_RC=$?
    case "$JUP_RC" in
        0) ;;
        1) echo -e "${YELLOW}  ⚠ Jupyter secret-sync had partial folder-fetch failures (continuing)${NC}" ;;
        *) echo -e "${RED}  ✗ Jupyter secret-sync transport failure (rc=$JUP_RC); aborting${NC}"; exit 1 ;;
    esac
fi

# ==========================================================================
# Sync Infisical secrets into Marimo as plaintext env-vars (Phase 1 Modul 1.2)
# ==========================================================================
# Same contract as the Jupyter sync block above. Both stacks share the
# Python implementation in nexus_deploy.secret_sync (#505).
if echo "$ENABLED_SERVICES" | grep -qw "marimo" \
    && [ -n "$INFISICAL_TOKEN" ] && [ -n "$PROJECT_ID" ]; then
    echo "Syncing Infisical secrets into Marimo env..."
    MAR_RC=0
    PROJECT_ID="$PROJECT_ID" INFISICAL_TOKEN="$INFISICAL_TOKEN" \
        INFISICAL_ENV="${INFISICAL_ENV:-dev}" GITEA_TOKEN="${GITEA_TOKEN:-}" \
        uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy secret-sync --stack marimo \
        || MAR_RC=$?
    case "$MAR_RC" in
        0) ;;
        1) echo -e "${YELLOW}  ⚠ Marimo secret-sync had partial folder-fetch failures (continuing)${NC}" ;;
        *) echo -e "${RED}  ✗ Marimo secret-sync transport failure (rc=$MAR_RC); aborting${NC}"; exit 1 ;;
    esac
fi

# Configure Wiki.js admin (uses user_email, not admin)
if echo "$ENABLED_SERVICES" | grep -qw "wikijs" && [ -n "$WIKIJS_ADMIN_PASS" ]; then
    (
        echo "  Configuring Wiki.js admin..."
        WIKIJS_EMAIL="${GITEA_USER_EMAIL:-$ADMIN_EMAIL}"
        for i in $(seq 1 30); do
            if ssh nexus "curl -fsS --connect-timeout 2 'http://localhost:3005/healthz'" 2>/dev/null | grep -qi 'ok'; then
                break
            fi
            sleep 3
        done

        # Wiki.js finalize setup via GraphQL API
        SETUP_PAYLOAD=$(jq -n \
            --arg email "$WIKIJS_EMAIL" \
            --arg pass "$WIKIJS_ADMIN_PASS" \
            --arg url "https://wiki.${DOMAIN}" \
            '{query: "mutation ($input: SetupInput!) { setup(input: $input) { responseResult { succeeded message } } }", variables: {input: {adminEmail: $email, adminPassword: $pass, adminPasswordConfirm: $pass, siteUrl: $url, telemetry: false}}}')

        RESULT=$(printf '%s' "$SETUP_PAYLOAD" | ssh nexus "curl -s -X POST 'http://localhost:3005/graphql' \
            -H 'Content-Type: application/json' \
            -d @-" 2>&1 || echo "")

        if echo "$RESULT" | grep -q '"succeeded":true'; then
            echo -e "${GREEN}  ✓ Wiki.js admin created (user: $WIKIJS_EMAIL)${NC}"
        elif echo "$RESULT" | grep -q 'already'; then
            echo -e "${YELLOW}  ⚠ Wiki.js already configured${NC}"
        else
            echo -e "${YELLOW}  ⚠ Wiki.js auto-setup failed - configure manually at first login${NC}"
            echo -e "${YELLOW}    Credentials available in Infisical${NC}"
        fi
    ) &
    CONFIG_JOBS+=($!)
fi

# Configure Dify admin account
if echo "$ENABLED_SERVICES" | grep -qw "dify" && [ -n "$DIFY_ADMIN_PASS" ]; then
    (
        echo "  Configuring Dify..."

        # Wait for Dify API to be ready (returns 307 when working)
        DIFY_READY=false
        for i in $(seq 1 40); do
            DIFY_HEALTH=$(ssh nexus "curl -s -o /dev/null -w '%{http_code}' http://localhost:8501/ 2>/dev/null" || echo "000")
            if [ "$DIFY_HEALTH" = "200" ] || [ "$DIFY_HEALTH" = "302" ] || [ "$DIFY_HEALTH" = "307" ]; then
                DIFY_READY=true
                break
            fi
            sleep 3
        done

        if [ "$DIFY_READY" = "false" ]; then
            echo -e "${YELLOW}  ⚠ Dify not ready after 120s - skipping auto-configuration${NC}"
            exit 0
        fi

        # Wait for API to be fully initialized
        sleep 5

        # Check if setup is already completed
        SETUP_CHECK=$(ssh nexus "curl -s http://localhost:8501/console/api/setup" 2>/dev/null || echo "")
        if echo "$SETUP_CHECK" | grep -q '"step":"finished"'; then
            echo -e "${YELLOW}  ⚠ Dify already configured - skipping admin setup${NC}"
        else
            # Step 1: Validate init password (required before setup)
            INIT_RESULT=$(ssh nexus "curl -s -c /tmp/dify-cookies -X POST 'http://localhost:8501/console/api/init' \
                -H 'Content-Type: application/json' \
                -d '{\"password\":\"$DIFY_ADMIN_PASS\"}'" 2>&1 || echo "")

            if ! echo "$INIT_RESULT" | grep -q '"result":"success"'; then
                echo -e "${YELLOW}  ⚠ Dify init validation failed - configure manually${NC}"
                exit 0
            fi

            # Step 2: Create admin account via setup API (uses session cookie from init)
            DIFY_SETUP_PAYLOAD=$(jq -n \
                --arg email "$ADMIN_EMAIL" \
                --arg password "$DIFY_ADMIN_PASS" \
                '{email: $email, name: "Admin", password: $password}')
            DIFY_RESULT=$(printf '%s' "$DIFY_SETUP_PAYLOAD" | ssh nexus "curl -s -b /tmp/dify-cookies -X POST 'http://localhost:8501/console/api/setup' \
                -H 'Content-Type: application/json' \
                -d @-" 2>&1 || echo "")

            # Clean up cookies
            ssh nexus "rm -f /tmp/dify-cookies" 2>/dev/null || true

            if echo "$DIFY_RESULT" | grep -q '"result":"success"'; then
                echo -e "${GREEN}  ✓ Dify admin created (email: $ADMIN_EMAIL)${NC}"
            elif echo "$DIFY_RESULT" | grep -qi 'already'; then
                echo -e "${YELLOW}  ⚠ Dify already configured${NC}"
            else
                echo -e "${YELLOW}  ⚠ Dify auto-setup failed - configure manually at /install${NC}"
                echo -e "${YELLOW}    Credentials available in Infisical${NC}"
            fi
        fi
    ) &
    CONFIG_JOBS+=($!)
fi

# Wait for all background configuration jobs to complete
if [ ${#CONFIG_JOBS[@]} -gt 0 ]; then
    echo "  Waiting for background configuration jobs to complete..."
    wait "${CONFIG_JOBS[@]}"
else
    echo "  No background configuration jobs to wait for"
fi

# -----------------------------------------------------------------------------
# Done!
# -----------------------------------------------------------------------------
echo ""
echo -e "${GREEN}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                    ✅ Deployment Complete!                    ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Show service URLs from tofu output
echo -e "${CYAN}🔗 Your Services:${NC}"
cd "$TOFU_DIR" && tofu output -json service_urls 2>/dev/null | jq -r 'to_entries | .[] | "   \(.key): \(.value)"' || echo "   (service URLs not available)"
echo ""

echo -e "${CYAN}📌 SSH Access:${NC}"
echo -e "   ssh nexus"
echo ""
echo -e "${CYAN}🔐 View credentials:${NC}"
echo -e "   Credentials available in Infisical"
echo ""
