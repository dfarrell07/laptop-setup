#!/bin/bash
set -euo pipefail
# CI-only dummy vault password. NEVER use this to encrypt group_vars/all/vault.yml —
# the real vault must be encrypted with the YubiKey HMAC-SHA1 script (vault-pass.sh).
#
# SECURITY: Disable shell tracing to prevent password exposure in CI logs
_t=0; [[ $- == *x* ]] && _t=1; set +x
printf '%s\n' "ci-dummy-vault-password"
[ "$_t" = "1" ] && set -x
