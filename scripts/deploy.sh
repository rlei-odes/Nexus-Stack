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
# Phase 4b1+4b2 (#505) — orchestrator wire-up
# -----------------------------------------------------------------------------
# What was [3/7]–[6/7] (~480 LoC bash glue around per-CLI invocations
# with eval-tempfile state-handoff) and [7/7] (~600 LoC bash glue +
# tempfile-eval dance for gitea/woodpecker/mirror) is now two
# orchestrator-runner CLI calls:
#
#   1. python -m nexus_deploy run-pre-bootstrap → 8-phase pipeline:
#      workspace-coords → service-env → firewall-configure →
#      stack-sync → firewall-sync → global-env → compose-up →
#      infisical-provision. Emits eval-able stdout: INFISICAL_TOKEN +
#      PROJECT_ID + REPO_NAME + GITEA_REPO_OWNER + WORKSPACE_BRANCH.
#
#   2. python -m nexus_deploy run-all → 14-phase post-bootstrap
#      pipeline: infisical-bootstrap → services-configure →
#      gitea-configure → compose-restart → kestra-secret-sync →
#      kestra-register → seed (mirror-skip) → woodpecker-oauth →
#      woodpecker-apply → mirror-setup → mirror-seed-rerun →
#      mirror-finalize → secret-sync-jupyter → secret-sync-marimo.
#      Emits eval-able stdout: RESTART_SERVICES +
#      WOODPECKER_GITEA_CLIENT + WOODPECKER_GITEA_SECRET (consumed by
#      surviving deploy.sh glue, though most have moved into the
#      orchestrator's own compose-restart / woodpecker-apply phases —
#      kept for back-compat).
#
# Tofu output reads stay in bash (Python doesn't run tofu).
# Docker Hub login stays in bash (~10 LoC, Phase 4c follow-up).
# Wetty SSH-Agent setup stays as a per-stack Python CLI gated by bash.
# Done banner / service URL display stays in bash (Phase 4c).
# -----------------------------------------------------------------------------

echo ""
echo -e "${YELLOW}[3/7] Loading enabled services + firewall rules from OpenTofu...${NC}"

# Get enabled services from tofu output — bash because Python doesn't run tofu.
TOFU_ERR=$(mktemp)
echo "$TOFU_ERR" >> "$RUNNER_CLEANUP_PATHS"
if ! ENABLED_SERVICES_JSON=$(cd "$TOFU_DIR" && tofu output -json enabled_services 2>"$TOFU_ERR"); then
    echo -e "${RED}  Error: Failed to read enabled_services from OpenTofu state${NC}"
    cat "$TOFU_ERR" >&2
    exit 1
fi
ENABLED_SERVICES=$(echo "$ENABLED_SERVICES_JSON" | jq -r '.[]')
ENABLED_SERVICES_CSV=$(echo "$ENABLED_SERVICES" | tr '\n ' ',,')

# Read firewall_rules JSON from Tofu — required by run-pre-bootstrap.
# Same fail-fast contract as before: a transient Tofu read failure
# MUST NOT fall through to "{}" because the Python firewall module
# treats "{}" as intentional zero-entry mode (deletes existing
# overrides). PR #532 R5 #2 made FIREWALL_RULES_JSON a required env
# var in the CLI handler so a missing value aborts there too.
if ! FIREWALL_JSON=$(cd "$TOFU_DIR" && tofu output -json firewall_rules 2>/dev/null); then
    echo -e "${RED}  ✗ Failed to load firewall_rules from OpenTofu — refusing to fall through to zero-entry mode (would delete every existing firewall override). Aborting; investigate Tofu state.${NC}" >&2
    exit 1
fi
echo -e "${GREEN}  ✓ Tofu inputs loaded${NC}"

# -----------------------------------------------------------------------------
# Docker Hub Login (optional - for increased pull rate limits)
# -----------------------------------------------------------------------------
# Stays in bash for Phase 4b — pure ssh wrapper, low-value migration.
# Runs BEFORE run-pre-bootstrap so compose-up's docker pull benefits.
if [ -n "${DOCKERHUB_USER:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ]; then
    echo ""
    echo -e "${YELLOW}[4/7] Logging into Docker Hub...${NC}"
    ssh nexus "echo '$DOCKERHUB_TOKEN' | docker login -u '$DOCKERHUB_USER' --password-stdin" 2>/dev/null
    echo -e "${GREEN}  ✓ Docker Hub login successful (200 pulls/6h)${NC}"
else
    echo ""
    echo -e "${CYAN}[4/7] Skipping Docker Hub login (anonymous: 100 pulls/6h)${NC}"
fi

# -----------------------------------------------------------------------------
# Setup SSH-Agent for Wetty (if enabled) — Phase 3 Modul 3.4f (#530)
# -----------------------------------------------------------------------------
# Already a Python CLI; conditional gate stays in bash. Runs before
# run-pre-bootstrap so the SSH-Agent is up by the time Wetty's
# container starts in compose-up.
if echo "$ENABLED_SERVICES" | grep -qw "wetty"; then
    echo ""
    echo -e "${YELLOW}[4.5/7] Setting up SSH-Agent for Wetty...${NC}"
    WETTY_RC=0
    uv run --quiet --project "$PROJECT_ROOT" \
        python -m nexus_deploy setup wetty-ssh-agent \
        || WETTY_RC=$?
    case "$WETTY_RC" in
        0) echo -e "${GREEN}  ✓ SSH-Agent configured for Wetty${NC}" ;;
        1) echo -e "${YELLOW}  ⚠ Wetty SSH-Agent setup soft-failed — see stderr above (continuing — Wetty is non-critical)${NC}" ;;
        *) echo -e "${RED}  ✗ Wetty SSH-Agent setup failed (rc=$WETTY_RC); aborting${NC}"; exit "$WETTY_RC" ;;
    esac
    unset WETTY_RC
fi

# -----------------------------------------------------------------------------
# [5/7] Pre-bootstrap pipeline
# -----------------------------------------------------------------------------
# 8 phases (workspace-coords → service-env → firewall-configure →
# stack-sync → firewall-sync → global-env → compose-up →
# infisical-provision). Emits eval-able stdout for the surviving glue.
echo ""
echo -e "${YELLOW}[5/7] Running pre-bootstrap pipeline (8 phases)...${NC}"
PRE_BOOT_OUT=$(mktemp)
chmod 600 "$PRE_BOOT_OUT"
echo "$PRE_BOOT_OUT" >> "$RUNNER_CLEANUP_PATHS"
PRE_BOOT_RC=0
printf '%s' "$SECRETS_JSON" | \
    DOMAIN="$DOMAIN" \
    ADMIN_EMAIL="$ADMIN_EMAIL" \
    ADMIN_USERNAME="$ADMIN_USERNAME" \
    USER_EMAIL="${USER_EMAIL:-}" \
    GITEA_ADMIN_PASS="${GITEA_ADMIN_PASS:-}" \
    GITEA_USER_EMAIL="${GITEA_USER_EMAIL:-}" \
    GITEA_USER_PASS="${GITEA_USER_PASS:-}" \
    GH_MIRROR_REPOS="${GH_MIRROR_REPOS:-}" \
    GH_MIRROR_TOKEN="${GH_MIRROR_TOKEN:-}" \
    INFISICAL_PASS="${INFISICAL_PASS:-}" \
    FIREWALL_RULES_JSON="$FIREWALL_JSON" \
    IMAGE_VERSIONS_JSON="${IMAGE_VERSIONS_JSON:-{}}" \
    ENABLED_SERVICES="$ENABLED_SERVICES_CSV" \
    OM_PRINCIPAL_DOMAIN="${OM_PRINCIPAL_DOMAIN:-}" \
    PROJECT_ROOT="$PROJECT_ROOT" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy run-pre-bootstrap > "$PRE_BOOT_OUT" \
    || PRE_BOOT_RC=$?
case "$PRE_BOOT_RC" in
    0|1)
        # rc=0 → all phases ok or skipped; rc=1 → at least one
        # phase produced 'partial' (operator sees yellow warn in
        # stderr, deploy continues). Both cases produce parseable
        # stdout — eval to capture INFISICAL_TOKEN + PROJECT_ID +
        # REPO_NAME + GITEA_REPO_OWNER + WORKSPACE_BRANCH.
        if [ -s "$PRE_BOOT_OUT" ]; then
            eval "$(cat "$PRE_BOOT_OUT")"
        fi
        if [ "$PRE_BOOT_RC" = "1" ]; then
            echo -e "${YELLOW}  ⚠ Pre-bootstrap had partial failures (continuing)${NC}"
        else
            echo -e "${GREEN}  ✓ Pre-bootstrap pipeline complete${NC}"
        fi
        ;;
    *)
        echo -e "${RED}  ✗ Pre-bootstrap hard failure (rc=$PRE_BOOT_RC); aborting${NC}" >&2
        exit "$PRE_BOOT_RC"
        ;;
esac
unset PRE_BOOT_RC

# -----------------------------------------------------------------------------
# [6/7] Post-bootstrap pipeline
# -----------------------------------------------------------------------------
# 14 phases. Reads INFISICAL_TOKEN + PROJECT_ID + workspace-coords from
# the run-pre-bootstrap stdout above; emits RESTART_SERVICES +
# WOODPECKER_GITEA_CLIENT + WOODPECKER_GITEA_SECRET for downstream
# (kept for back-compat; both are also consumed in-orchestrator now).
echo ""
echo -e "${YELLOW}[6/7] Running post-bootstrap pipeline (14 phases)...${NC}"
RUN_ALL_OUT=$(mktemp)
chmod 600 "$RUN_ALL_OUT"
echo "$RUN_ALL_OUT" >> "$RUNNER_CLEANUP_PATHS"
RUN_ALL_RC=0

# SSH_KEY_BASE64 must match the legacy `build_folder "ssh"` encoding
# byte-for-byte: `echo "$X" | base64` (NOT `printf '%s'`). echo
# appends a trailing newline before the pipe, so the legacy bytes
# are base64(<key>+\n). Critically guarded on
# `[ -n "$SSH_PRIVATE_KEY_CONTENT" ]` — without this guard,
# `echo "" | base64` produces `Cg==` (base64 of a single newline),
# which BootstrapEnv would treat as a populated key and overwrite
# the operator's value.
if [ -n "${SSH_PRIVATE_KEY_CONTENT:-}" ]; then
    SSH_KEY_BASE64=$(echo "$SSH_PRIVATE_KEY_CONTENT" | base64 | tr -d '\n')
else
    SSH_KEY_BASE64=""
fi

printf '%s' "$SECRETS_JSON" | \
    DOMAIN="$DOMAIN" \
    ADMIN_EMAIL="$ADMIN_EMAIL" \
    ADMIN_USERNAME="$ADMIN_USERNAME" \
    REPO_NAME="${REPO_NAME:-}" \
    GITEA_REPO_OWNER="${GITEA_REPO_OWNER:-}" \
    WORKSPACE_BRANCH="${WORKSPACE_BRANCH:-main}" \
    PROJECT_ID="${PROJECT_ID:-}" \
    INFISICAL_TOKEN="${INFISICAL_TOKEN:-}" \
    INFISICAL_ENV="${INFISICAL_ENV:-dev}" \
    GH_MIRROR_REPOS="${GH_MIRROR_REPOS:-}" \
    GH_MIRROR_TOKEN="${GH_MIRROR_TOKEN:-}" \
    GITEA_USER_USERNAME="${GITEA_USER_USERNAME:-}" \
    GITEA_USER_EMAIL="${GITEA_USER_EMAIL:-}" \
    GITEA_USER_PASS="${GITEA_USER_PASS:-}" \
    WOODPECKER_AGENT_SECRET="${WOODPECKER_AGENT_SECRET:-}" \
    SSH_KEY_BASE64="$SSH_KEY_BASE64" \
    ENABLED_SERVICES="$ENABLED_SERVICES_CSV" \
    OM_PRINCIPAL_DOMAIN="${OM_PRINCIPAL_DOMAIN:-}" \
    uv run --quiet --project "$PROJECT_ROOT" \
    python -m nexus_deploy run-all > "$RUN_ALL_OUT" \
    || RUN_ALL_RC=$?
case "$RUN_ALL_RC" in
    0|1)
        if [ -s "$RUN_ALL_OUT" ]; then
            eval "$(cat "$RUN_ALL_OUT")"
        fi
        if [ "$RUN_ALL_RC" = "1" ]; then
            echo -e "${YELLOW}  ⚠ Post-bootstrap had partial failures (see per-phase log above)${NC}"
        else
            echo -e "${GREEN}  ✓ Post-bootstrap pipeline complete${NC}"
        fi
        ;;
    *)
        echo -e "${RED}  ✗ Post-bootstrap hard failure (rc=$RUN_ALL_RC); aborting${NC}" >&2
        exit "$RUN_ALL_RC"
        ;;
esac
unset RUN_ALL_RC SSH_KEY_BASE64
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
