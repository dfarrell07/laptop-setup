#!/usr/bin/env bash
# write-vault-pass.sh — write scripts/vault-pass.sh, the YubiKey HMAC-SHA1 vault password helper, and its
# vault-pass.sh.sha256 baseline. The helper holds no secret: every machine whose keys share the slot 2 secret
# (make setup-yubikeys) uses this same file, so a new machine writes it here instead of copying it from another.
# Callers: scripts/setup-yubikeys.sh (after programming keys), scripts/vault-import.sh (reusing a vault).
# SECURITY: the helper it writes prints the vault password; never let xtrace record it.
set -euo pipefail
{ set +x; } 2>/dev/null
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_PASS_SH="$SCRIPT_DIR/vault-pass.sh"

# Write to a temp file first: never a window where the real file has wrong content or permissions
_tmp=$(mktemp "${VAULT_PASS_SH}.XXXXXX")
trap 'rm -f "$_tmp"' EXIT
chmod 700 "$_tmp"
cat > "$_tmp" << 'VAULTPASS'
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykman (yubikey-manager)  Touch YubiKey when its light blinks.
# ykman otp calculate requires hex-encoded challenge; pre-computed from ASCII.
# SECURITY: this script prints the vault password; never let xtrace record it.
unset BASH_ENV
{ set +x; } 2>/dev/null
set -euo pipefail
_CHALRESP_HEX=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n')
_to=timeout; command -v timeout >/dev/null 2>&1 || _to=gtimeout
"$_to" 20 ykman otp calculate 2 "$_CHALRESP_HEX" || {
  echo "ERROR: vault password unavailable: insert exactly one programmed YubiKey and touch it when it blinks (ykman message above)" >&2
  exit 1
}
VAULTPASS
mv "$_tmp" "$VAULT_PASS_SH"

# Relative path (basename only), so sha256sum -c works from any clone location
if command -v sha256sum &>/dev/null; then _sha256=(sha256sum); else _sha256=(shasum -a 256); fi
(cd "$SCRIPT_DIR" && "${_sha256[@]}" "$(basename "$VAULT_PASS_SH")" > vault-pass.sh.sha256)
