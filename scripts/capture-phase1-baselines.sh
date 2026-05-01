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
# Output is committed to tests/fixtures/baselines/. Re-running overwrites
# the directory — safe to invoke after every spin-up that needs to refresh
# the baselines (e.g. after deploy.sh's secret-set changes).
# =============================================================================
set -euo pipefail

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

# Extract deploy.sh's L115–228 jq parsing block into a standalone
# script and run it against the captured secrets.json. The output is
# `set -o posix` — every shell var the deploy.sh produces, in a form
# the Python `dump_shell()` test can byte-compare against.
echo "→ Running deploy.sh's SECRETS_JSON parser standalone…"
{
    # Lines 115–228 are the jq-parsing-into-globals block. The block
    # itself reads $SECRETS_JSON, so we define it before sourcing.
    echo "#!/usr/bin/env bash"
    echo "set -uo pipefail"
    echo "SECRETS_JSON=\$(cat \"\$1\")"
    awk 'NR>=115 && NR<=228' "$REPO_ROOT/scripts/deploy.sh"
    echo 'set -o posix; set | grep -E "^[A-Z][A-Z0-9_]*=" | sort'
} > /tmp/parse-secrets-baseline.sh
chmod +x /tmp/parse-secrets-baseline.sh
bash /tmp/parse-secrets-baseline.sh "$DEST/secrets.json" > "$DEST/shell-vars.txt"
rm /tmp/parse-secrets-baseline.sh
echo "  ✓ shell-vars.txt ($(wc -l <"$DEST/shell-vars.txt") vars)"

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
echo "Next: review the captured fixtures, redact any genuinely-secret values"
echo "you don't want in git, then commit on the clean baselines branch."
