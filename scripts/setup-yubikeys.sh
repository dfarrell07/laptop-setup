#!/bin/bash
# setup-yubikeys.sh — Program one or more YubiKeys with the same HMAC-SHA1 secret
# and write vault-pass.sh for ansible-vault integration.
#
# Usage:  scripts/setup-yubikeys.sh
#
# Requires: ykpersonalize, ykchalresp (ykpers package — installed by 'make all')
# Run after: make all (installs ykpers + yubikey-manager)
# Run before: ansible-vault encrypt group_vars/all/vault.yml
#
# Design:
#   - Generates a random 20-byte HMAC secret once; programs every YubiKey with it
#   - Secret stays in memory only; never written to disk
#   - Verifies each key produces identical challenge-response output
#   - Writes scripts/vault-pass.sh on success
#   - Prompts to add more keys until you say no
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_PASS_SH="$SCRIPT_DIR/vault-pass.sh"
CHALLENGE="ansible-vault-laptop-setup"
RED='\033[0;31m' GRN='\033[0;32m' YLW='\033[0;33m' NC='\033[0m'

die()  { printf "${RED}ERROR: %s${NC}\n" "$*" >&2; exit 1; }
ok()   { printf "${GRN}OK:    %s${NC}\n" "$*"; }
warn() { printf "${YLW}WARN:  %s${NC}\n" "$*"; }
info() { printf "       %s\n" "$*"; }

# ── Prerequisites ────────────────────────────────────────────────────────────
for cmd in ykpersonalize ykchalresp openssl; do
  command -v "$cmd" &>/dev/null || die "'$cmd' not found. Run 'make all' first (installs ykpers)."
done

printf "\n%s\n" "═══════════════════════════════════════════════"
printf "%s\n"   " YubiKey HMAC-SHA1 Setup"
printf "%s\n\n" "═══════════════════════════════════════════════"
printf "This script programs one or more YubiKeys with the same HMAC-SHA1 secret\n"
printf "so each produces the same ansible-vault password for this machine.\n\n"
warn "The secret is generated fresh and kept in memory only — never written to disk."
warn "Program ALL your YubiKeys now in a single run. You cannot retrieve the secret later."
printf "\n"

# ── Generate fresh HMAC secret ───────────────────────────────────────────────
HMAC_SECRET=$(openssl rand -hex 20)
EXPECTED_OUTPUT=""
KEY_COUNT=0

# ── Program loop ─────────────────────────────────────────────────────────────
while true; do
  KEY_COUNT=$((KEY_COUNT + 1))
  printf "── YubiKey #%d ──────────────────────────────────────\n" "$KEY_COUNT"

  if [[ $KEY_COUNT -eq 1 ]]; then
    printf "Insert your FIRST YubiKey and press Enter (or Ctrl+C to abort): "
  else
    printf "Remove the previous key. Insert YubiKey #%d and press Enter\n" "$KEY_COUNT"
    printf "(or press Enter with no key inserted if you are done): "
  fi
  read -r USER_INPUT

  # Allow user to finish after key #2 or later by pressing Enter with no input
  if [[ $KEY_COUNT -ge 2 && -z "$USER_INPUT" ]]; then
    info "No more keys — finishing."
    break
  fi

  # Detect YubiKey presence
  if ! ykpersonalize -2 -y -ochal-resp -ochal-hmac -ohmac-lt64 \
                     -oserial-api-visible \
                     -a "$HMAC_SECRET" 2>/dev/null; then
    die "ykpersonalize failed. Is the YubiKey inserted and slot 2 unprotected?"
  fi
  ok "YubiKey #$KEY_COUNT: slot 2 programmed"

  # Verify output
  printf "       Touch your YubiKey for verification... "
  ACTUAL_OUTPUT=""
  if ! ACTUAL_OUTPUT=$(ykchalresp -2 "$CHALLENGE" 2>/dev/null); then
    die "ykchalresp failed on YubiKey #$KEY_COUNT. Touch the key when its light blinks."
  fi
  printf "\n"

  if [[ $KEY_COUNT -eq 1 ]]; then
    EXPECTED_OUTPUT="$ACTUAL_OUTPUT"
    ok "YubiKey #1 verification passed (baseline established)"
  else
    if [[ "$ACTUAL_OUTPUT" != "$EXPECTED_OUTPUT" ]]; then
      die "YubiKey #$KEY_COUNT produces a DIFFERENT output than YubiKey #1. Programming failed."
    fi
    ok "YubiKey #$KEY_COUNT verification passed (matches YubiKey #1)"
  fi

  printf "\n"
  if [[ $KEY_COUNT -ge 2 ]]; then
    printf "Program another key? [y/N] "
    read -r ANOTHER
    [[ "${ANOTHER,,}" == "y" ]] || break
    printf "\n"
  fi
done

# ── Safety check ─────────────────────────────────────────────────────────────
if [[ $KEY_COUNT -lt 2 ]]; then
  warn "Only 1 key was programmed. Strongly recommend programming a second (backup) key."
  printf "Continue anyway? [y/N] "
  read -r CONTINUE
  [[ "${CONTINUE,,}" == "y" ]] || die "Aborted. Re-run to program more keys."
fi

# ── Write vault-pass.sh ───────────────────────────────────────────────────────
printf "\n── Writing vault-pass.sh ─────────────────────────────\n"

if [[ -f "$VAULT_PASS_SH" ]]; then
  # Check if already a real implementation (not the CI stub)
  if grep -q 'ci-dummy-vault-password' "$VAULT_PASS_SH"; then
    info "Replacing CI dummy stub with YubiKey implementation."
  else
    warn "vault-pass.sh already exists and is not the CI stub."
    printf "Overwrite it? [y/N] "
    read -r OVERWRITE
    [[ "${OVERWRITE,,}" == "y" ]] || { warn "Skipping vault-pass.sh write. Update it manually."; }
  fi
fi

cat > "$VAULT_PASS_SH" << 'VAULTPASS'
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykchalresp (ykpers package)  Touch YubiKey when its light blinks.
set -euo pipefail
CHALLENGE="ansible-vault-laptop-setup"
ykchalresp -2 "$CHALLENGE" 2>/dev/null || {
  echo "ERROR: YubiKey not available — insert YubiKey and retry" >&2
  exit 1
}
VAULTPASS
chmod 700 "$VAULT_PASS_SH"
ok "vault-pass.sh written and chmod 700"

# ── Verify vault-pass.sh works ────────────────────────────────────────────────
printf "       Touch YubiKey to verify vault-pass.sh... "
if VERIFY_OUTPUT=$("$VAULT_PASS_SH" 2>/dev/null) && [[ "$VERIFY_OUTPUT" == "$EXPECTED_OUTPUT" ]]; then
  printf "\n"
  ok "vault-pass.sh verified — output matches"
else
  printf "\n"
  die "vault-pass.sh produced unexpected output. Check YubiKey insertion."
fi

# ── Next steps ────────────────────────────────────────────────────────────────
printf "\n%s\n" "═══════════════════════════════════════════════"
printf " Done — %d YubiKey(s) programmed\n" "$KEY_COUNT"
printf "%s\n\n" "═══════════════════════════════════════════════"
printf "Next steps:\n\n"
printf "  1. Generate your hardware SSH keys (requires YubiKey inserted):\n"
printf "       ssh-keygen -t ed25519-sk -O resident -f ~/.ssh/id_ed25519_sk\n"
printf "       ssh-keygen -t ed25519-sk -O resident -f ~/.ssh/id_ed25519_sk_signing\n"
printf "     Then add the public key to GitHub:\n"
printf "       gh ssh-key add ~/.ssh/id_ed25519_sk.pub --title 'YubiKey'\n\n"
printf "  2. Populate group_vars/all/vault.yml with your SSH keys, then encrypt:\n"
printf "       ansible-vault edit group_vars/all/vault.yml   # paste key content\n"
printf "       # OR encrypt the existing plaintext stub first, then edit:\n"
printf "       ansible-vault encrypt group_vars/all/vault.yml\n"
printf "       ansible-vault edit group_vars/all/vault.yml\n\n"
printf "  3. Deploy the keys:\n"
printf "       make ssh\n\n"
printf "  4. Reboot (SAFE now — authorized_keys will be written by make ssh).\n\n"
warn "Store each programmed YubiKey in a different physical location."
warn "The HMAC secret was NOT saved anywhere. If all YubiKeys are lost, re-provision."
printf "\n"
