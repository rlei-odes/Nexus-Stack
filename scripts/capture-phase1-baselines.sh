#!/usr/bin/env bash
# =============================================================================
# capture-phase1-baselines.sh — gold-master capture for #505 Phase 1
# =============================================================================
# Captures pre-migration outputs of two deploy.sh sections (the third
# was section "1: SECRETS_JSON parsing" but Phase 1 Modul 1.3 has now
# replaced it with src/nexus_deploy/config.py — the byte-compare for
# that block lives at the unit-test layer, see
# tests/unit/__snapshots__/test_config.ambr):
#
#   1. build_folder JSON payloads (deploy.sh:2070–2340) → tests/fixtures/baselines/infisical-payloads/
#   2. .infisical.env files (deploy.sh:4860–5520)       → tests/fixtures/baselines/jupyter.infisical.env
#                                                       + tests/fixtures/baselines/marimo.infisical.env
#
# Plus, for context, a copy of the live tofu secrets output:
#   * tests/fixtures/baselines/secrets.json — usable as a real-world
#     fixture for config.py snapshot tests once captured.
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
# (1) Live SECRETS_JSON snapshot — informational, not a behavioural baseline
# -----------------------------------------------------------------------------
# The shell-vars.txt baseline that used to live here was made obsolete
# by Phase 1 Modul 1.3: deploy.sh no longer carries the `^[A-Z0-9_]+=
# $(echo "$SECRETS_JSON" | jq …)` lines, so re-running the legacy parser
# isn't possible after that PR landed. The byte-compare for config.py
# now lives at the unit-test layer (tests/unit/__snapshots__/test_config.ambr).
#
# We still capture the live SECRETS_JSON because it's a useful real-world
# fixture for snapshot tests of config.py — operators can drop it next
# to the synthetic `secrets_full.json` to expand test coverage.
echo "→ Capturing SECRETS_JSON via tofu output…"
(cd "$TOFU_DIR" && tofu output -json secrets) > "$DEST/secrets.json"
echo "  ✓ secrets.json ($(wc -c <"$DEST/secrets.json") bytes)"

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
