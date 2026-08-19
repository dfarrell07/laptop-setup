#!/bin/bash
set -euo pipefail
# CI-only dummy vault password. NEVER use this to encrypt group_vars/all/vault.yml —
# the real vault must be encrypted with the YubiKey HMAC-SHA1 script (vault-pass.sh).
#
# This stub is used ONLY during 'make bootstrap' to create the initial vault-pass.sh.
# After bootstrap, users MUST replace vault-pass.sh with a secure password provider
# (YubiKey HMAC-SHA1, encrypted file, system keyring, etc.) BEFORE encrypting vault.yml.
#
# SECURITY: Encrypting vault.yml with this trivial password exposes ALL secrets:
# - SSH authentication keys
# - Git commit signing keys
# - Container registry credentials
# - Encrypted notes master password
#
# SECURITY: Disable shell tracing to prevent password exposure in CI logs
_t=0; [[ $- == *x* ]] && _t=1; set +x
printf '%s\n' "ci-dummy-vault-password"
[ "$_t" != "0" ] && set -x || true
