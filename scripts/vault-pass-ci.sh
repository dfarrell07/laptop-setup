#!/bin/bash
set -euo pipefail
# CI-only dummy vault password. NEVER use this to encrypt group_vars/all/vault.yml —
# the real vault must be encrypted with the YubiKey HMAC-SHA1 script (vault-pass.sh).
echo "ci-dummy-vault-password"
