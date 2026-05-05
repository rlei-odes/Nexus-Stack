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
# [1/7] + [2/7] + jq + volume mount — Phase 3 Modul 3.4a (#505)
# -----------------------------------------------------------------------------
# Was 4 inline bash blocks (~270 LoC): ssh-config rendering with awk-
# dedup, Service-Token retry+backoff, SSH connectivity loop, jq
# bootstrap, persistent-volume mount with three-stage fallback.
# Replaced with `python -m nexus_deploy setup <subcommand>` invocations.
# Same semantics, same retry schedules; tests pin every Hardening-Round
# from the legacy block via injected sleep + probe runner.
#
# The EXIT-trap setup (REMOTE_CLEANUP_PATHS, RUNNER_CLEANUP_PATHS) stays
# in bash for now — other migrated modules' deploy.sh wrappers reference
# those paths to register their tmpfiles. Phase 3 Modul 3.4b
# (orchestrator.py) replaces the trap with Python contextlib.ExitStack
# once deploy.sh is just a thin wrapper.

echo -e "${YELLOW}[1/7] Configuring SSH access...${NC}"
SSH_HOST="$SSH_HOST" \
    CF_ACCESS_CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}" \
    CF_ACCESS_CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy setup ssh-config

echo ""
echo -e "${YELLOW}[2/7] Waiting for SSH via Cloudflare Tunnel...${NC}"
CF_ACCESS_CLIENT_ID="${CF_ACCESS_CLIENT_ID:-}" \
    CF_ACCESS_CLIENT_SECRET="${CF_ACCESS_CLIENT_SECRET:-}" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy setup wait-ssh

# EXIT-trap setup — managed in bash through Phase 3.4a; Phase 3.4b
# replaces with Python contextlib.ExitStack inside orchestrator.run_all.
# Round-4 PR #524: dropped the legacy SSH_ERR tmpfile (was used by
# the old bash retry-loop to capture ssh stderr; the loop is now in
# Python where the captured stderr lives in SSHReadinessResult.last_error
# and the tmpfile would just be dead state with a never-cleared rm-f
# in the trap).
REMOTE_CLEANUP_PATHS=$(mktemp)
RUNNER_CLEANUP_PATHS=$(mktemp)
trap 'if [ -s "$REMOTE_CLEANUP_PATHS" ]; then while IFS= read -r p; do [ -n "$p" ] && ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=3 -o ServerAliveCountMax=2 nexus "rm -f \"$p\"" 2>/dev/null || true; done < "$REMOTE_CLEANUP_PATHS"; fi; rm -f "$REMOTE_CLEANUP_PATHS"; if [ -s "$RUNNER_CLEANUP_PATHS" ]; then while IFS= read -r p; do [ -n "$p" ] && rm -f "$p"; done < "$RUNNER_CLEANUP_PATHS"; fi; rm -f "$RUNNER_CLEANUP_PATHS"' EXIT

uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy setup ensure-jq

PERSISTENT_VOLUME_ID=$(cd "$TOFU_DIR" && tofu output -raw persistent_volume_id 2>/dev/null || echo "0")
# Initialize MOUNT_RC=0 BEFORE the command (Round-3 PR #524 finding):
# `cmd || MOUNT_RC=$?` only assigns on failure, so a stale value
# inherited from the outer environment would survive a successful
# run and trigger a spurious abort in the case-block below.
MOUNT_RC=0
PERSISTENT_VOLUME_ID="$PERSISTENT_VOLUME_ID" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy setup mount-volume \
    || MOUNT_RC=$?
case "$MOUNT_RC" in
    0) ;;
    1) echo -e "${YELLOW}  ⚠ Persistent volume mount fallback failed (continuing)${NC}" ;;
    *) echo -e "${RED}  ✗ Persistent volume hard failure (rc=${MOUNT_RC}); aborting${NC}"; exit "${MOUNT_RC}" ;;
esac
unset MOUNT_RC

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


# Phase 3 Modul 3.4c (#505) — was 700+ LoC of bash heredocs.
# All per-service .env files (40+ stacks) now generated by Python's
# service-env CLI. Workspace-repo coords (GITEA_REPO_URL, REPO_NAME,
# GITEA_REPO_OWNER, WORKSPACE_BRANCH, GIT_AUTHOR, etc.) stay derived
# in bash here because they feed BOTH the service-env CLI (via
# env-vars for the optional Gitea workspace block append) AND the
# downstream [7/7] orchestrator (also via env-vars).

# Generate Git workspace .env vars for services that integrate with Gitea
if echo "$ENABLED_SERVICES" | grep -qw "gitea" && [ -n "$GITEA_ADMIN_PASS" ]; then
    # Workspace-config identity: when no separate single-address user is
    # configured (GITEA_USER_EMAIL empty after trim+comma-split), fall back
    # to the admin identity for repo URLs and service .env values.
    if [ -n "$GITEA_USER_EMAIL" ]; then
        GITEA_USER_USERNAME="${GITEA_USER_EMAIL%%@*}"
    else
        GITEA_USER_USERNAME="$ADMIN_USERNAME"
    fi
    # Determine workspace repo. Three cases:
    # - mirror + user → fork of first mirror into user's namespace
    # - mirror + no user → admin's mirror-readonly repo directly
    # - no mirror → admin's default empty repo
    if [ -n "${GH_MIRROR_REPOS:-}" ] && [ -n "$GITEA_USER_EMAIL" ]; then
        FIRST_MIRROR=$(echo "$GH_MIRROR_REPOS" | cut -d',' -f1 | tr -d ' ')
        WORKSPACE_REPO_NAME=$(basename "$FIRST_MIRROR" .git)
        GITEA_USER_SANITIZED="${GITEA_USER_USERNAME//[^a-zA-Z0-9]/_}"
        REPO_NAME="${WORKSPACE_REPO_NAME}_${GITEA_USER_SANITIZED}"
        GITEA_REPO_OWNER="${GITEA_USER_USERNAME}"
        GITEA_REPO_URL="http://gitea:3000/${GITEA_REPO_OWNER}/${REPO_NAME}.git"
    elif [ -n "${GH_MIRROR_REPOS:-}" ]; then
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
    # No mirror → 'main'. Mirror → query GitHub API for upstream's
    # default_branch, fall back to 'main' on any HTTP/parse failure.
    WORKSPACE_BRANCH="main"
    if [ -n "${GH_MIRROR_REPOS:-}" ] && [ -n "${GH_MIRROR_TOKEN:-}" ]; then
        FIRST_MIRROR_FOR_BRANCH=$(echo "$GH_MIRROR_REPOS" | cut -d',' -f1 | tr -d ' ')
        GH_OWNER_REPO=$(echo "$FIRST_MIRROR_FOR_BRANCH" \
            | sed -E 's#^https?://github\.com/##; s#[?#].*$##; s#/$##; s#\.git$##')
        if [ -n "$GH_OWNER_REPO" ] && [[ "$GH_OWNER_REPO" =~ ^[^/]+/[^/]+$ ]]; then
            # Token + URL go through curl --config (mode 0600) so the
            # token never appears in argv. Subshell trap ensures cleanup.
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

    # Workspace identity: USER if both email + password set, else ADMIN.
    if [ -n "$GITEA_USER_EMAIL" ] && [ -n "$GITEA_USER_PASS" ]; then
        GITEA_GIT_USER="${GITEA_USER_USERNAME}"
        GITEA_GIT_PASS="${GITEA_USER_PASS}"
        GIT_AUTHOR="${GITEA_USER_USERNAME}"
        GIT_EMAIL="${GITEA_USER_EMAIL}"
    else
        GITEA_GIT_USER="${ADMIN_USERNAME}"
        GITEA_GIT_PASS="${GITEA_ADMIN_PASS}"
        GIT_AUTHOR="${ADMIN_USERNAME}"
        GIT_EMAIL="${ADMIN_EMAIL}"
    fi
fi

# Generate per-service .env files via Python service-env CLI.
# When Gitea is enabled and the workspace coords above were derived,
# the CLI also appends the marker-wrapped Gitea workspace block to
# jupyter / marimo / code-server / meltano / prefect.
echo "  Generating per-service .env files..."
SERVICE_ENV_RC=0
printf '%s' "$SECRETS_JSON" | \
    DOMAIN="$DOMAIN" \
    ADMIN_EMAIL="$ADMIN_EMAIL" \
    GITEA_USER_EMAIL="${GITEA_USER_EMAIL:-}" \
    GITEA_USER_USERNAME="${GITEA_USER_USERNAME:-}" \
    GITEA_REPO_OWNER="${GITEA_REPO_OWNER:-}" \
    REPO_NAME="${REPO_NAME:-}" \
    GITEA_REPO_URL="${GITEA_REPO_URL:-}" \
    GITEA_USERNAME="${GITEA_GIT_USER:-}" \
    GITEA_PASSWORD="${GITEA_GIT_PASS:-}" \
    GIT_AUTHOR_NAME="${GIT_AUTHOR:-}" \
    GIT_AUTHOR_EMAIL="${GIT_EMAIL:-}" \
    OM_PRINCIPAL_DOMAIN="${OM_PRINCIPAL_DOMAIN:-}" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy service-env \
    --enabled "$(echo "$ENABLED_SERVICES" | tr '\n ' ',,')" \
    --stacks-dir "$STACKS_DIR" \
    || SERVICE_ENV_RC=$?
case "${SERVICE_ENV_RC:-0}" in
    0) echo -e "${GREEN}  ✓ Per-service .env files generated${NC}" ;;
    1) echo -e "${YELLOW}  ⚠ Some service .env renders failed (continuing)${NC}" ;;
    *) echo -e "${RED}  ✗ service-env hard failure (rc=${SERVICE_ENV_RC}); aborting${NC}"; exit "${SERVICE_ENV_RC}" ;;
esac
unset SERVICE_ENV_RC


# Phase 3 Modul 3.3 (#505) — was 44 lines: per-stack rsync loop +
# disabled-stack cleanup ssh heredoc. Both replaced by one CLI call.
# Same idempotent contract: rsync `stacks/<svc>/` → `:/opt/...stacks/<svc>/`,
# then docker-compose-down + rm -rf any folder NOT in $ENABLED_SERVICES.
echo ""
echo -e "${YELLOW}[3+4/7] Syncing stacks and cleaning up disabled ones...${NC}"
SYNC_RC=0
uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy stack-sync \
    --enabled "$(echo "$ENABLED_SERVICES" | tr '\n ' ',,')" \
    --stacks-dir "$STACKS_DIR" \
    || SYNC_RC=$?
case "$SYNC_RC" in
    0) echo -e "${GREEN}  ✓ Stacks synced and disabled stacks cleaned up${NC}" ;;
    1) echo -e "${YELLOW}  ⚠ Stack sync had partial failures (continuing)${NC}" ;;
    *) echo -e "${RED}  ✗ Stack sync hard failure (rc=$SYNC_RC); aborting${NC}"; exit "$SYNC_RC" ;;
esac

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
# Setup SSH-Agent for Wetty (if enabled) — Phase 3 Modul 3.4f (#505)
# -----------------------------------------------------------------------------
# Was a 100-line ssh heredoc; now a Python CLI that renders the same
# idempotent bash + parses RESULT_WETTY back into a typed result.
if echo "$ENABLED_SERVICES" | grep -qw "wetty"; then
    echo ""
    echo -e "${YELLOW}[5.5/7] Setting up SSH-Agent for Wetty...${NC}"
    WETTY_RC=0
    uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy setup wetty-ssh-agent \
        || WETTY_RC=$?
    case "$WETTY_RC" in
        0) echo -e "${GREEN}  ✓ SSH-Agent configured for Wetty${NC}" ;;
        1) echo -e "${YELLOW}  ⚠ Wetty SSH-Agent setup produced no parseable result (continuing — Wetty is non-critical)${NC}" ;;
        *) echo -e "${RED}  ✗ Wetty SSH-Agent setup failed (rc=$WETTY_RC); aborting${NC}"; exit "$WETTY_RC" ;;
    esac
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
    # Provision admin + project — Phase 3 Modul 3.4f (#505).
    # Was a 75-line bash block (readiness probe + admin-bootstrap +
    # project-create + cred-persist); now a Python CLI that emits
    # eval-able stdout: INFISICAL_TOKEN=...; PROJECT_ID=... (both
    # always emitted, may be empty when not-ready / soft-fail).
    INFISICAL_OUT=$(mktemp)
    echo "$INFISICAL_OUT" >> "$RUNNER_CLEANUP_PATHS"
    INFISICAL_PROVISION_RC=0
    ADMIN_EMAIL="$ADMIN_EMAIL" INFISICAL_PASS="$INFISICAL_PASS" \
        uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy infisical provision-admin > "$INFISICAL_OUT" \
        || INFISICAL_PROVISION_RC=$?
    case "$INFISICAL_PROVISION_RC" in
        0) eval "$(cat "$INFISICAL_OUT")"  # sets INFISICAL_TOKEN + PROJECT_ID
           echo -e "${GREEN}  ✓ Infisical provisioned${NC}" ;;
        1) eval "$(cat "$INFISICAL_OUT")"  # may set empty values; surface via shell vars
           echo -e "${YELLOW}  ⚠ Infisical provision soft-fail (see Python stderr above) — continuing without secret push${NC}" ;;
        *) echo -e "${RED}  ✗ Infisical provision hard failure (rc=$INFISICAL_PROVISION_RC); aborting${NC}"
           rm -f "$INFISICAL_OUT"
           exit "$INFISICAL_PROVISION_RC" ;;
    esac
    rm -f "$INFISICAL_OUT"

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
    # Skipped when provision-admin reported soft-fail (empty token /
    # project_id) — operator must investigate the Python stderr above.
    if [ -n "${INFISICAL_TOKEN:-}" ] && [ -n "${PROJECT_ID:-}" ]; then
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
    else
        echo -e "${YELLOW}  ⚠ INFISICAL_TOKEN / PROJECT_ID empty — skipping secret push${NC}"
    fi
fi

# Configure all admin-setup hooks via the Python services-configure CLI.
# Phase 2 (Modul 2.2b/c/d, #505) shipped the first 8: Portainer, n8n,
# Metabase, LakeFS, OpenMetadata, RedPanda, Superset, Filestash. Phase 3
# Modul 3.4d (#505) added the remaining 6: Uptime Kuma (warn-only stub
# for #145), Garage, Wiki.js, Dify, Windmill, SFTPGo. Total: 14 admin-
# setup hooks dispatched in one SSH round-trip per spin-up.
#
# The CLI renders the per-hook bash, runs it via SSH, and parses
# `RESULT hook=<name> status=<...>` lines into a SetupResult. Hook
# failures are non-aborting (rc=1); transport / unexpected errors
# return rc=2 and abort the deploy.
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

# pg-ducklake bootstrap SQL re-apply moved into the services-configure
# hook registry (Modul 3.4f, #505). The hook runs as part of the
# services configure CLI invocation ABOVE — `pg-ducklake` is now in
# the same dispatch loop as the other 14 admin-setup hooks. Same
# 30-second pg_isready probe + idempotent psql exec, just folded into
# a typed Python rendering layer.


# All 14 admin-setup bash blocks that used to live here (Filestash,
# SFTPGo, Garage, Wiki.js, Dify, Windmill, plus the Modul 2.2 set
# Portainer/n8n/Metabase/LakeFS/OpenMetadata/RedPanda/Superset and
# the Uptime Kuma stub) are now dispatched via the single
# `python -m nexus_deploy services configure --enabled <list>` call
# ABOVE (Modul 2.2b/c/d + 3.4d, #505). One SSH round-trip; per-hook
# rendered bash; idempotent; testable via pure-Python rendering.


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
    # Tempfile holds the eval-able `GITEA_TOKEN=<sha1>` line. chmod
    # 600 explicitly (mktemp's mode is umask-dependent — CI runners
    # often run with umask 022 → 644 by default, which would let
    # any other process on the runner read the token). Register
    # with the global RUNNER_CLEANUP_PATHS list so an interrupt
    # between mktemp and the explicit `rm -f` below still triggers
    # removal via the script's EXIT trap (see L324-331).
    GITEA_OUT=$(mktemp)
    chmod 600 "$GITEA_OUT"
    echo "$GITEA_OUT" >> "$RUNNER_CLEANUP_PATHS"
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

        # Seed examples/workspace-seeds/ → Gitea repo via the migrated
        # nexus_deploy.seeder CLI (Phase 2 Modul 2.1, #512). Defined
        # UNCONDITIONALLY (outside the GH_MIRROR_REPOS branch) so the
        # mirror-mode block later in this script can call it for
        # per-user forks too — Modul 2.2f will migrate that block.
        # Args default to $GITEA_REPO_OWNER/$REPO_NAME (matches the
        # legacy no-arg call site at L3403). Pass explicit owner/repo
        # when looping over multiple repos (e.g. mirror forks).
        seed_workspace_files() {
            local owner="${1:-$GITEA_REPO_OWNER}" repo="${2:-$REPO_NAME}"
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

        if [ -z "${GH_MIRROR_REPOS:-}" ]; then
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
                    # Phase 3 Modul 3.4f (#505) — replaces the 325-LoC
                    # build-SECRETS + dedup + chmod + force-recreate
                    # heredoc with the Python secret-sync CLI's kestra
                    # mode. The CLI does steps 1-4 (Infisical fetch +
                    # SECRET_<KEY>=<base64> append + force-recreate);
                    # the post-restart auth-aware wait below stays in
                    # bash (it's the bridge between the secret-sync
                    # restart and the flow-registration step).
                    if [ -n "${INFISICAL_TOKEN:-}" ] && [ -n "${PROJECT_ID:-}" ]; then
                        echo "  Syncing Infisical secrets into Kestra env..."
                        KESTRA_SS_RC=0
                        PROJECT_ID="$PROJECT_ID" INFISICAL_TOKEN="$INFISICAL_TOKEN" \
                            INFISICAL_ENV="${INFISICAL_ENV:-dev}" \
                            GITEA_TOKEN="${GITEA_TOKEN:-}" \
                            uv run --quiet --project "$PROJECT_ROOT" \
                            python -m nexus_deploy secret-sync --stack kestra \
                            || KESTRA_SS_RC=$?
                        case "$KESTRA_SS_RC" in
                            0) ;;
                            1) echo -e "${YELLOW}  ⚠ Kestra secret-sync had partial folder-fetch failures (continuing)${NC}" ;;
                            *) echo -e "${RED}  ✗ Kestra secret-sync transport failure (rc=$KESTRA_SS_RC); aborting${NC}"; exit "$KESTRA_SS_RC" ;;
                        esac
                    else
                        echo -e "${YELLOW}  ⚠ INFISICAL_TOKEN / PROJECT_ID empty — skipping Kestra secret-sync${NC}"
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
            # Phase 2 Modul 2.2f (#505) — list/delete/create OAuth app
            # via the migrated nexus_deploy.gitea woodpecker-oauth CLI.
            # Idempotent: existing "Woodpecker CI" app is deleted first
            # so the create always returns a fresh client_secret
            # (Gitea has no rotate-secret API).
            #
            # The CLI emits WOODPECKER_GITEA_CLIENT + WOODPECKER_GITEA_SECRET
            # to stdout in eval-able form (same handoff pattern as
            # `gitea configure` from #519). Tempfile mode 600 +
            # registered with $RUNNER_CLEANUP_PATHS for trap-driven
            # removal on interrupt/early-exit.
            if echo "$ENABLED_SERVICES" | grep -qw "woodpecker" && [ -n "$WOODPECKER_AGENT_SECRET" ]; then
                echo "  Creating Woodpecker CI OAuth app in Gitea..."
                # Clear any stale eval values from a prior iteration / parent
                # env BEFORE the CLI runs. Without this, an rc=1 (CLI failed
                # to mint fresh creds) followed by the `[ -n "${VAR:-}" ]`
                # gate below would happily rewrite Woodpecker's .env using
                # whatever credentials happened to be inherited from the
                # shell — potentially restarting Woodpecker with stale creds
                # that Gitea has already invalidated. (Copilot R1)
                unset WOODPECKER_GITEA_CLIENT WOODPECKER_GITEA_SECRET
                WP_OUT=$(mktemp)
                chmod 600 "$WP_OUT"
                echo "$WP_OUT" >> "$RUNNER_CLEANUP_PATHS"
                WP_RC=0
                DOMAIN="$DOMAIN" \
                    GITEA_TOKEN="$GITEA_TOKEN" \
                    ADMIN_USERNAME="${ADMIN_USERNAME:-admin}" \
                    uv run --quiet --project "$PROJECT_ROOT" \
                    python -m nexus_deploy gitea woodpecker-oauth > "$WP_OUT" \
                    || WP_RC=$?
                case "$WP_RC" in
                    0)
                        if [ -s "$WP_OUT" ]; then
                            eval "$(cat "$WP_OUT")"
                        fi
                        echo -e "${GREEN}  ✓ Woodpecker OAuth app created${NC}"
                        ;;
                    1)
                        echo -e "${YELLOW}  ⚠ Could not create Woodpecker OAuth app in Gitea${NC}"
                        ;;
                    *)
                        rm -f "$WP_OUT"
                        echo -e "${RED}  ✗ Woodpecker OAuth hard failure (rc=$WP_RC); aborting${NC}"
                        exit "$WP_RC"
                        ;;
                esac
                rm -f "$WP_OUT"

                if [ -n "${WOODPECKER_GITEA_CLIENT:-}" ] && [ -n "${WOODPECKER_GITEA_SECRET:-}" ]; then
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

    # Phase 2 Modul 2.2f part 2 (#505): the mirror loop (admin-UID
    # lookup + per-repo migrate + per-user fork via temp-token +
    # collab + mirror-sync + merge-upstream) is now in
    # nexus_deploy.gitea.run_mirror_setup. Same idempotent semantics
    # as before; CLI emits FORK_NAME=<name> + GITEA_REPO_OWNER=<user>
    # on stdout (eval-able) iff a fork was provisioned, so the
    # downstream seed_workspace_files (still in deploy.sh) hits the
    # user's fork rather than the per-iteration mirror name.
    #
    # Clear any stale eval values from prior iterations so an rc=1
    # (CLI failed pre-fork) doesn't leak old values into the seed
    # call below.
    unset FORK_NAME
    MIRROR_OUT=$(mktemp)
    chmod 600 "$MIRROR_OUT"
    echo "$MIRROR_OUT" >> "$RUNNER_CLEANUP_PATHS"
    MIRROR_RC=0
    ADMIN_USERNAME="$ADMIN_USERNAME" \
        GITEA_ADMIN_PASS="$GITEA_ADMIN_PASS" \
        GITEA_TOKEN="$GITEA_TOKEN" \
        GITEA_USER_USERNAME="${GITEA_USER_USERNAME:-}" \
        GH_MIRROR_REPOS="$GH_MIRROR_REPOS" \
        GH_MIRROR_TOKEN="$GH_MIRROR_TOKEN" \
        WORKSPACE_BRANCH="${WORKSPACE_BRANCH:-main}" \
        uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy gitea mirror-setup > "$MIRROR_OUT" \
        || MIRROR_RC=$?
    case "$MIRROR_RC" in
        0)
            if [ -s "$MIRROR_OUT" ]; then
                eval "$(cat "$MIRROR_OUT")"
            fi
            ;;
        1)
            if [ -s "$MIRROR_OUT" ]; then
                eval "$(cat "$MIRROR_OUT")"
            fi
            echo -e "${YELLOW}  ⚠ Mirror setup had partial failures (continuing)${NC}"
            ;;
        *)
            rm -f "$MIRROR_OUT"
            echo -e "${RED}  ✗ Mirror setup hard failure (rc=$MIRROR_RC); aborting${NC}"
            exit "$MIRROR_RC"
            ;;
    esac
    rm -f "$MIRROR_OUT"

    if [ "$MIRROR_RC" -le 1 ]; then

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
