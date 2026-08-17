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
#   - Generates a random 20-byte HMAC secret; programs every YubiKey with it
#   - Secret is delivered to ykpersonalize via stdin (-a with no argument reads
#     from stdin per the man page), avoiding /proc/<pid>/cmdline exposure
#   - Secret is NOT written to disk
#   - Verifies each key produces identical challenge-response output
#   - Writes scripts/vault-pass.sh atomically with 700 permissions
#   - Prompts to add more keys until you say no
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_PASS_SH="$SCRIPT_DIR/vault-pass.sh"
CHALLENGE="ansible-vault-laptop-setup"
CHALRESP_TIMEOUT=20  # seconds to wait for YubiKey touch
RED='\033[0;31m' GRN='\033[0;32m' YLW='\033[0;33m' NC='\033[0m'

die()  { printf "${RED}ERROR: %s${NC}\n" "$*" >&2; exit 1; }
ok()   { printf "${GRN}OK:    %s${NC}\n" "$*"; }
warn() { printf "${YLW}WARN:  %s${NC}\n" "$*"; }
info() { printf "       %s\n" "$*"; }
die_loop() {
  warn "($((KEY_COUNT - 1)) key(s) verified before this failure; the secret is gone — re-run to start over with a new secret and re-program ALL keys)"
  die "$@"
}

# ── Ctrl+C / signal cleanup ───────────────────────────────────────────────────
_tmp='' _prog_err='' _interrupted=false
trap 'rm -f "$_tmp" "$_prog_err"' EXIT
trap '_interrupted=true; printf "\n\nInterrupted.\n" >&2
      printf "Any YubiKeys already programmed in this run have the new secret.\n" >&2
      printf "Re-run setup-yubikeys.sh to program remaining keys with the SAME secret.\n" >&2
      printf "(You will not see the secret again — re-run will generate a NEW one,\n" >&2
      printf " requiring you to re-program ALL keys from scratch.)\n" >&2
      HMAC_SECRET="0000000000000000000000000000000000000000"; unset HMAC_SECRET
      EXPECTED_OUTPUT="0000000000000000000000000000000000000000"; unset EXPECTED_OUTPUT
      ACTUAL_OUTPUT="0000000000000000000000000000000000000000"; unset ACTUAL_OUTPUT
      exit 130' INT TERM

# ── Prerequisites ─────────────────────────────────────────────────────────────
for cmd in ykman ykpersonalize ykchalresp openssl timeout; do
  command -v "$cmd" &>/dev/null || die "'$cmd' not found. Run 'make all' first (installs ykpers)."
done
[[ -t 0 ]] || die "stdin is not a terminal — this script must be run interactively"

printf "\n%s\n" "═══════════════════════════════════════════════"
printf "%s\n"   " YubiKey HMAC-SHA1 Setup"
printf "%s\n\n" "═══════════════════════════════════════════════"
printf "Programs one or more YubiKeys with the same HMAC-SHA1 secret so each\n"
printf "produces the same ansible-vault password for this machine.\n\n"
warn "Program ALL your YubiKeys now in a single run — the secret is never saved."
warn "Interrupting after key #1 is programmed means you must re-run and start over."
printf "\n"

# ── Generate and validate HMAC secret ────────────────────────────────────────
HMAC_SECRET=$(openssl rand -hex 20) || die "openssl rand failed"
[[ ${#HMAC_SECRET} -eq 40 ]] || die "openssl produced wrong-length secret (got ${#HMAC_SECRET} chars, want 40)"
[[ "$HMAC_SECRET" =~ ^[0-9a-f]{40}$ ]] || die "openssl produced non-hex secret — entropy source may be broken"

EXPECTED_OUTPUT=""
KEY_COUNT=0
SEEN_SERIALS=""
PROGRAMMED_COUNT=0

# ── Program loop ──────────────────────────────────────────────────────────────
while true; do
  KEY_COUNT=$((KEY_COUNT + 1))
  printf "── YubiKey #%d ──────────────────────────────────────\n" "$KEY_COUNT"

  if [[ $KEY_COUNT -eq 1 ]]; then
    printf "Insert your FIRST YubiKey and press Enter (or Ctrl+C to abort): "
  else
    printf "Remove the previous key. Insert YubiKey #%d and press Enter\n" "$KEY_COUNT"
    printf "(or press Enter with nothing inserted to finish): "
  fi
  read -r USER_INPUT

  # Empty Enter after key #2+ = done
  if [[ $KEY_COUNT -ge 2 && -z "$USER_INPUT" ]]; then
    info "No more keys — finishing."
    break
  fi

  # Reject duplicate-key reprogramming by comparing serials before ykpersonalize
  _current_serial=$(ykman list --serials 2>/dev/null | head -n1)
  [[ -n "$_current_serial" ]] \
    || die "No YubiKey detected — insert YubiKey #$KEY_COUNT and retry"
  case ":$SEEN_SERIALS:" in
    *":$_current_serial:"*)
      die "Same YubiKey still inserted (serial $_current_serial) — remove it before programming key #$KEY_COUNT" ;;
  esac
  SEEN_SERIALS="${SEEN_SERIALS:+$SEEN_SERIALS:}$_current_serial"

  # Program slot 2 — secret delivered via stdin to avoid /proc/<pid>/cmdline exposure
  _prog_err=$(mktemp)
  if ! printf '%s\n' "$HMAC_SECRET" | \
       ykpersonalize -2 -y -ochal-resp -ochal-hmac -ohmac-lt64 \
                     -oserial-api-visible -a 2>"$_prog_err"; then
    _err_msg=$(cat "$_prog_err")
    rm -f "$_prog_err"
    die_loop "ykpersonalize failed on YubiKey #$KEY_COUNT: ${_err_msg:-no error output — is a YubiKey inserted?}"
  fi
  rm -f "$_prog_err"
  ok "YubiKey #$KEY_COUNT: slot 2 programmed"
  PROGRAMMED_COUNT=$((PROGRAMMED_COUNT + 1))

  # Verify — with timeout so we don't hang if key is not touched
  printf "       Touch your YubiKey to verify (%ds timeout)... " "$CHALRESP_TIMEOUT"
  ACTUAL_OUTPUT=""
  _ykcr_err=$(mktemp)
  _ykcr_rc=0
  ACTUAL_OUTPUT=$(timeout "$CHALRESP_TIMEOUT" ykchalresp -2 "$CHALLENGE" 2>"$_ykcr_err") || _ykcr_rc=$?
  if [[ $_ykcr_rc -ne 0 ]]; then
    printf "\n"
    if [[ $_ykcr_rc -eq 124 ]]; then
      rm -f "$_ykcr_err"
      die_loop "Timed out waiting for YubiKey #$KEY_COUNT touch — touch the key when its light blinks."
    else
      _ykcr_msg=$(cat "$_ykcr_err")
      rm -f "$_ykcr_err"
      die_loop "ykchalresp failed on YubiKey #$KEY_COUNT (rc=$_ykcr_rc)${_ykcr_msg:+: $_ykcr_msg} — is the key still inserted?"
    fi
  fi
  rm -f "$_ykcr_err"
  printf "\n"

  # Guard: empty output means programming failure even with rc=0
  [[ -n "$ACTUAL_OUTPUT" ]] || die_loop "ykchalresp returned empty output on YubiKey #$KEY_COUNT — slot 2 programming may have failed"

  if [[ $KEY_COUNT -eq 1 ]]; then
    EXPECTED_OUTPUT="$ACTUAL_OUTPUT"
    ok "YubiKey #1 verified (baseline: ${ACTUAL_OUTPUT:0:8}…)"
  else
    if [[ "$ACTUAL_OUTPUT" != "$EXPECTED_OUTPUT" ]]; then
      die_loop "YubiKey #$KEY_COUNT output (${ACTUAL_OUTPUT:0:8}…) differs from YubiKey #1 (${EXPECTED_OUTPUT:0:8}…) — programming failed"
    fi
    ok "YubiKey #$KEY_COUNT verified (matches YubiKey #1)"
  fi

  printf "\n"
  if [[ $KEY_COUNT -ge 2 ]]; then
    printf "Program another key? [y/N] "
    read -r ANOTHER
    [[ "${ANOTHER,,}" == "y" ]] || break
    printf "\n"
  fi
done

# ── Require at least 2 keys ───────────────────────────────────────────────────
if [[ $PROGRAMMED_COUNT -lt 2 ]]; then
  warn "Only 1 key was programmed. A backup key is strongly recommended."
  printf "Continue with just 1 key? [y/N] "
  read -r CONTINUE
  [[ "${CONTINUE,,}" == "y" ]] || die "Aborted. Re-run to program more keys."
fi

# ── Write vault-pass.sh (atomic: temp file → chmod → rename) ─────────────────
printf "\n── Writing vault-pass.sh ─────────────────────────────\n"

_write_vault_pass=true
if [[ -f "$VAULT_PASS_SH" ]]; then
  if grep -q 'ci-dummy-vault-password' "$VAULT_PASS_SH"; then
    info "Replacing CI dummy stub with YubiKey implementation."
  else
    warn "vault-pass.sh already exists and is not the CI stub."
    printf "Overwrite it? [y/N] "
    read -r OVERWRITE
    [[ "${OVERWRITE,,}" == "y" ]] || _write_vault_pass=false
  fi
fi

if $_write_vault_pass; then
  # Write to temp file first (atomic: never a window where the real file
  # has wrong content or wrong permissions)
  _tmp=$(mktemp "${VAULT_PASS_SH}.XXXXXX")
  chmod 700 "$_tmp"
  cat > "$_tmp" << VAULTPASS
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykchalresp (ykpers package)  Touch YubiKey when its light blinks.
set -euo pipefail
CHALLENGE="$CHALLENGE"
ykchalresp -2 "$CHALLENGE" 2>/dev/null || {
  echo "ERROR: YubiKey not available — insert YubiKey and retry" >&2
  exit 1
}
VAULTPASS
  mv "$_tmp" "$VAULT_PASS_SH"
  ok "vault-pass.sh written (mode 700, atomic rename)"

  # Verify vault-pass.sh works (requires a key still inserted)
  printf "       Touch YubiKey to verify vault-pass.sh (%ds)... " "$CHALRESP_TIMEOUT"
  VERIFY_OUTPUT=""
  if VERIFY_OUTPUT=$(timeout "$CHALRESP_TIMEOUT" "$VAULT_PASS_SH" 2>/dev/null) \
     && [[ -n "$VERIFY_OUTPUT" ]] \
     && [[ "$VERIFY_OUTPUT" == "$EXPECTED_OUTPUT" ]]; then
    printf "\n"
    ok "vault-pass.sh verified — output matches"
  else
    printf "\n"
    warn "vault-pass.sh verification failed (YubiKey not inserted or removed?)"
    warn "vault-pass.sh was written correctly — verify manually with: scripts/vault-pass.sh"
  fi
else
  warn "vault-pass.sh not updated. Update it manually (see SECURITY.md)."
fi

# ── Next steps ────────────────────────────────────────────────────────────────
printf "\n%s\n" "═══════════════════════════════════════════════"
printf " Done — %d YubiKey(s) programmed\n" "$PROGRAMMED_COUNT"
printf "%s\n\n" "═══════════════════════════════════════════════"
printf "Next steps:\n\n"
printf "  1. Generate your hardware SSH key (YubiKey must be inserted):\n"
printf "       # Without -O resident (portable; key stored as file referencing YubiKey):\n"
printf "       ssh-keygen -t ed25519-sk -f ~/.ssh/id_ed25519_sk\n"
printf "       # With -O resident (discoverable credential stored on YubiKey itself):\n"
printf "       ssh-keygen -t ed25519-sk -O resident -f ~/.ssh/id_ed25519_sk\n"
printf "     Add the public key to GitHub:\n"
printf "       gh ssh-key add ~/.ssh/id_ed25519_sk.pub --title 'YubiKey'\n\n"
printf "  2. Populate group_vars/all/vault.yml with your keys:\n"
printf "       ansible-vault encrypt group_vars/all/vault.yml   # encrypt first\n"
printf "       ansible-vault edit group_vars/all/vault.yml      # then paste keys\n\n"
printf "  3. Deploy the keys:\n"
printf "       make ssh\n\n"
printf "  4. Reboot — SSH authorized_keys is now deployed; port 722 is safe.\n\n"
warn "Store each YubiKey in a different physical location."
warn "The HMAC secret was NOT saved. If all keys are lost: re-provision the machine."
printf "\n"

# ── Zeroize sensitive variables ───────────────────────────────────────────────
HMAC_SECRET='0000000000000000000000000000000000000000'; unset HMAC_SECRET
EXPECTED_OUTPUT='0000000000000000000000000000000000000000'; unset EXPECTED_OUTPUT
ACTUAL_OUTPUT='0000000000000000000000000000000000000000'; unset ACTUAL_OUTPUT
