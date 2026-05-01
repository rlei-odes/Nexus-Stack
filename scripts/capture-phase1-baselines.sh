#!/usr/bin/env bash
# =============================================================================
# capture-phase1-baselines.sh — gold-master capture for #505 Phase 1
# =============================================================================
# Captures pre-migration outputs of three deploy.sh sections:
#   1. SECRETS_JSON parsing block (L115–228) → tests/fixtures/baselines/secrets.json
#                                              + tests/fixtures/baselines/shell-vars.txt
#   2. build_folder JSON payloads (L2070–2340) → tests/fixtures/baselines/infisical-payloads/
#   3. .infisical.env files (L4860–5520)       → tests/fixtures/baselines/jupyter.infisical.env
#                                              + tests/fixtures/baselines/marimo.infisical.env
#
# Prereqs:
#   - A successful spin-up has been run with enabled_services=jupyter,marimo
#     so the server has /tmp/nexus-baselines/ populated by deploy.sh and
#     /opt/docker-server/stacks/{jupyter,marimo}/.infisical.env present.
#   - Local R2 backend configured (backend.hcl) so `tofu output` works
#     against the current state.
#   - `ssh nexus` works (Cloudflare Access service token in env).
#
# Usage:
#   bash scripts/capture-phase1-baselines.sh
#
# Output lands under tests/fixtures/baselines/, which is gitignored — the
# captured files contain RAW SECRET VALUES (Infisical passwords, API
# tokens, encryption keys). The Phase 1 module PRs are responsible for
# producing redacted, committable versions; the raw capture stays local.
# Re-running overwrites the directory — safe to invoke after every spin-
# up that needs to refresh the baselines.
# =============================================================================
set -euo pipefail

# Restrictive umask so any captured secret files (secrets.json,
# .infisical.env, payload JSONs) are 600 / dirs 700 — owner-only —
# even on shared workstations or if the repo dir has loose group perms.
umask 077

# C locale for every downstream `sort`, `grep`, `sed`. Without this the
# baseline files would byte-differ between macOS (en_US.UTF-8 default)
# and Linux runners (C.UTF-8 / LANG=C). The whole point of these
# fixtures is byte-compare, so ambient locale variance is fatal.
export LC_ALL=C

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/tests/fixtures/baselines"
TOFU_DIR="$REPO_ROOT/tofu/stack"

mkdir -p "$DEST"

# -----------------------------------------------------------------------------
# (1) SECRETS_JSON + shell-vars baseline (config.py)
# -----------------------------------------------------------------------------
echo "→ Capturing SECRETS_JSON via tofu output…"
(cd "$TOFU_DIR" && tofu output -json secrets) > "$DEST/secrets.json"
echo "  ✓ secrets.json ($(wc -c <"$DEST/secrets.json") bytes)"

# Extract ONLY the jq-parsing lines from deploy.sh (not the surrounding
# `tofu output` reassignment, $TOFU_DIR-dependent code, echos, or
# known_hosts cleanup) and run them in a clean `env -i` shell. The
# output is `declare -p` for exactly the parsed variables — no PATH,
# HOME, BASHOPTS, or any ambient credentials leak into the fixture.
#
# The two `EXTERNAL_S3_*=${VAR:-default}` fallback lines (deploy.sh
# L177–178) live right after the jq block and complete the parsed
# state, so they're included.
echo "→ Running deploy.sh's SECRETS_JSON parser standalone…"

# Names of every variable the parser assigns. Computed by grepping
# deploy.sh, so a new field added there is auto-picked-up. Wrapped in
# `{ grep || true; }` because under `set -euo pipefail`, a zero-match
# grep exits 1 and would kill the script BEFORE the explicit
# `count==0` drift-detection check below ever ran. With the wrap the
# pipeline yields empty stdout + exit 0 on no-match, the count check
# below still triggers, and we emit a clean error message instead of
# the cryptic shell-exit.
PARSER_VARS=$(
    { grep -oE '^[A-Z0-9_]+=\$\(echo "\$SECRETS_JSON"' "$REPO_ROOT/scripts/deploy.sh" || true; } \
        | sed 's/=.*//' | sort -u
)

# Word-count (not line-count: `echo "" | wc -l` is 1 even for empty input).
# Bail out if the grep pattern matched nothing — that's a deploy.sh refactor
# we want to know about immediately, not silently produce an empty baseline.
PARSER_VAR_COUNT=$(printf '%s\n' "$PARSER_VARS" | grep -c . || true)
if [ "$PARSER_VAR_COUNT" -eq 0 ]; then
    echo "ERROR: parser-var grep matched zero lines in deploy.sh — pattern drift?" >&2
    echo "  Expected pattern: ^[A-Z_]+=\\\$(echo \"\\\$SECRETS_JSON\" | jq" >&2
    exit 1
fi

mktemp_script=$(mktemp /tmp/parse-secrets-baseline.XXXXXX.sh)
trap 'rm -f "$mktemp_script"' EXIT

{
    echo '#!/usr/bin/env bash'
    # `-e` so a missing/broken jq fails fast (with `-uo` only, a piped
    # jq failure under pipefail leaves the var as "" silently, producing
    # an incomplete baseline that compares-equal-to-empty). The
    # `declare -p ... || true` lines below tolerate undefined vars
    # explicitly so they don't trip set -e.
    echo 'set -euo pipefail'
    echo 'SECRETS_JSON=$(cat)'
    grep -E '^[A-Z0-9_]+=\$\(echo "\$SECRETS_JSON" \| jq' "$REPO_ROOT/scripts/deploy.sh"
    grep -E '^EXTERNAL_S3_(LABEL|REGION)=\$\{' "$REPO_ROOT/scripts/deploy.sh"
    # Dump only the parser-assigned vars (no ambient env)
    for v in $PARSER_VARS; do
        printf 'declare -p %s 2>/dev/null || true\n' "$v"
    done
} > "$mktemp_script"

# `env -i` strips inherited env so PATH, HOME, CLOUDFLARE_*_TOKEN, etc.
# can't pollute or leak into the fixture. We restore a minimal PATH that
# resolves `cat`, `jq` (used by every parser line), and the bash built-
# ins on both macOS (homebrew) and Linux runners. LC_ALL=C must be
# threaded through too since `env -i` strips the script-level export.
env -i PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin LC_ALL=C \
    bash "$mktemp_script" \
    < "$DEST/secrets.json" \
    | sort > "$DEST/shell-vars.txt"
echo "  ✓ shell-vars.txt ($(wc -l <"$DEST/shell-vars.txt") vars from $PARSER_VAR_COUNT parser names)"

# -----------------------------------------------------------------------------
# (2) build_folder JSON payloads baseline (infisical.py)
# -----------------------------------------------------------------------------
echo "→ scp'ing /tmp/nexus-baselines/infisical-payloads-baseline from server…"
rm -rf "$DEST/infisical-payloads"
scp -rq nexus:/tmp/nexus-baselines/infisical-payloads-baseline "$DEST/infisical-payloads"
echo "  ✓ infisical-payloads/ ($(find "$DEST/infisical-payloads" -name '*.json' | wc -l | tr -d ' ') JSON files)"

# -----------------------------------------------------------------------------
# (3) .infisical.env files baseline (secret_sync.py)
# -----------------------------------------------------------------------------
for stack in jupyter marimo; do
    echo "→ scp'ing $stack/.infisical.env…"
    if ssh nexus "test -f /opt/docker-server/stacks/$stack/.infisical.env"; then
        scp -q "nexus:/opt/docker-server/stacks/$stack/.infisical.env" "$DEST/$stack.infisical.env"
        echo "  ✓ $stack.infisical.env ($(wc -l <"$DEST/$stack.infisical.env") lines)"
    else
        echo "  ✗ $stack/.infisical.env NOT FOUND on server — was the stack enabled in the spin-up?"
        exit 1
    fi
done

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo ""
echo "=== Baseline capture complete ==="
ls -la "$DEST"
echo ""
echo "⚠  These files contain RAW SECRET VALUES (passwords, API tokens,"
echo "   encryption keys). \$DEST is gitignored, so \`git add\` won't pick"
echo "   them up by accident. The Phase 1 module PRs (config.py,"
echo "   infisical.py, secret_sync.py) are responsible for producing"
echo "   redacted, committable fixture versions from these raw captures."
echo "   DO NOT \`git add -f tests/fixtures/baselines/\`."
