#!/bin/bash
# setup-yubikeys.sh — Program one or more YubiKeys with the same HMAC-SHA1 secret
# and write vault-pass.sh for ansible-vault integration.
#
# Usage:  scripts/setup-yubikeys.sh
#
# Requires: ykman (yubikey-manager — installed by 'make all')
# Run after: make all (installs yubikey-manager)
# Run before: ansible-vault encrypt group_vars/all/vault.yml
#
# Design:
#   - Generates a random 20-byte HMAC secret; programs every YubiKey with it
#   - Secret is delivered to ykman via stdin (ykman has no stdin placeholder;
#     piped without --force to avoid /proc/<pid>/cmdline exposure; fragile —
#     relies on Click reading key prompt then confirm prompt sequentially from stdin)
#   - Secret is NOT written to disk
#   - EXIT-trap zeroization is best-effort (bash heap; old allocation not zeroed);
#     effective suppression requires system_coredump_storage: none (not system_mask_abrt)
#   - Verifies each key produces identical challenge-response output
#   - Writes scripts/vault-pass.sh atomically with 700 permissions
#   - Prompts to add more keys until you say no
#   - HMAC-SHA1 vault derivation: 80-bit post-quantum security (Grover);
#     acceptable for 2025, monitor for HMAC-SHA256 YubiKey OTP support
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_PASS_SH="$SCRIPT_DIR/vault-pass.sh"
CHALLENGE="ansible-vault-laptop-setup"
# ykman otp calculate requires hex-encoded challenge (ykchalresp accepted raw ASCII)
_CHALRESP_HEX=$(printf '%s' "$CHALLENGE" | od -An -tx1 | tr -d ' \n')
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
# Zero-reassignment is best-effort: bash allocates a new string; the old heap
# allocation is not zeroed. Effective mitigation: system_coredump_storage: none.
_tmp='' _prog_err='' _ykcr_err=''
trap 'rm -f "$_tmp" "$_prog_err" "$_ykcr_err"
      HMAC_SECRET="0000000000000000000000000000000000000000"; unset HMAC_SECRET
      EXPECTED_OUTPUT="0000000000000000000000000000000000000000"; unset EXPECTED_OUTPUT
      ACTUAL_OUTPUT="0000000000000000000000000000000000000000"; unset ACTUAL_OUTPUT' EXIT
_cleanup_msg() {
  printf "\n\nInterrupted.\n" >&2
  printf "Any YubiKeys already programmed in this run have the new secret.\n" >&2
  printf "Re-run setup-yubikeys.sh to program remaining keys with the SAME secret.\n" >&2
  printf "(You will not see the secret again — re-run will generate a NEW one,\n" >&2
  printf " requiring you to re-program ALL keys from scratch.)\n" >&2
}
trap '_cleanup_msg; exit 130' INT
trap '_cleanup_msg; exit 143' TERM

# ── Prerequisites ─────────────────────────────────────────────────────────────
if command -v timeout &>/dev/null; then
  _timeout=timeout
elif command -v gtimeout &>/dev/null; then
  _timeout=gtimeout
else
  die "'timeout' not found. On macOS: brew install coreutils"
fi
for cmd in ykman openssl; do
  command -v "$cmd" &>/dev/null || die "'$cmd' not found. Run 'make all' first (installs yubikey-manager)."
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

  # Reject duplicate-key reprogramming by comparing serials before programming
  _current_serial=$("$_timeout" 10 ykman list --serials 2>/dev/null | head -n1) \
    || die "ykman timed out or failed listing devices — check USB connection"
  [[ -n "$_current_serial" ]] \
    || die "No YubiKey detected — insert YubiKey #$KEY_COUNT and retry"
  _key_count=$(ykman list --serials 2>/dev/null | wc -l)
  [[ "$_key_count" -le 1 ]] || die "Multiple YubiKeys detected ($_key_count) — remove extras and connect only one at a time"
  case ":$SEEN_SERIALS:" in
    *":$_current_serial:"*)
      die "Same YubiKey still inserted (serial $_current_serial) — remove it before programming key #$KEY_COUNT" ;;
  esac
  SEEN_SERIALS="${SEEN_SERIALS:+$SEEN_SERIALS:}$_current_serial"

  if ykman otp info 2>/dev/null | grep -q 'Slot 2: Programmed'; then
    warn "Slot 2 is already programmed on YubiKey #$KEY_COUNT — this will permanently overwrite it."
    printf "Overwrite slot 2? [y/N] "
    read -r SLOT2_CONFIRM
    [[ "${SLOT2_CONFIRM,,}" == "y" ]] || die "Aborted."
  fi

  # Program slot 2 — secret delivered via stdin to avoid /proc/<pid>/cmdline exposure.
  # ykman otp chalresp has no stdin placeholder for the key. Using --force requires
  # the key as a positional arg (visible in /proc/<pid>/cmdline and ps output). Instead
  # we pipe without --force: ykman reads the key from stdin (click_prompt), then reads
  # the confirmation from stdin (click.confirm uses err=True — prompt text goes to
  # stderr, but the answer is still read from stdin). Input order: <key>\n<y>\n.
  # This relies on Click's undocumented stdin behaviour and may break if ykman changes
  # its prompt order. The alternative is Option A: ykman otp chalresp --touch --force 2
  # "$HMAC_SECRET" — key in argv, /proc exposure accepted.
  #
  # Flag mapping from ykpersonalize:
  #   -ochal-resp/-ochal-hmac  → implicit (chalresp subcommand always sets these)
  #   -ohmac-lt64              → always on in ykman (not configurable, cannot be disabled)
  #   -ochal-btn-trig          → --touch
  #   -oserial-api-visible     → dropped (not exposed in ykman otp chalresp)
  #   -2                       → positional slot argument 2
  #   -y                       → --force (requires key in argv; avoided here via stdin)
  _prog_err=$(mktemp)
  if ! printf '%s\ny\n' "$HMAC_SECRET" | \
       ykman otp chalresp --touch 2 2>"$_prog_err"; then
    _err_msg=$(cat "$_prog_err")
    rm -f "$_prog_err"
    if printf '%s' "$_err_msg" | grep -qiE 'BACKEND_ERROR|write error|access.?code'; then
      _err_msg="${_err_msg} — Slot 2 may have an access code set. Clear it with: ykman otp delete 2"
    fi
    die_loop "ykman otp chalresp failed on YubiKey #$KEY_COUNT: ${_err_msg:-no error output — is a YubiKey inserted?}"
  fi
  rm -f "$_prog_err"
  ok "YubiKey #$KEY_COUNT: slot 2 programmed"
  PROGRAMMED_COUNT=$((PROGRAMMED_COUNT + 1))

  # Verify — with timeout so we don't hang if key is not touched
  printf "       Touch your YubiKey to verify (%ds timeout)... " "$CHALRESP_TIMEOUT"
  ACTUAL_OUTPUT=""
  _ykcr_err=$(mktemp)
  _ykcr_rc=0
  ACTUAL_OUTPUT=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>"$_ykcr_err") || _ykcr_rc=$?
  if [[ $_ykcr_rc -ne 0 ]]; then
    printf "\n"
    if [[ $_ykcr_rc -eq 124 ]]; then
      rm -f "$_ykcr_err"
      die_loop "Timed out waiting for YubiKey #$KEY_COUNT touch — touch the key when its light blinks."
    else
      _ykcr_msg=$(cat "$_ykcr_err")
      rm -f "$_ykcr_err"
      die_loop "ykman otp calculate failed on YubiKey #$KEY_COUNT (rc=$_ykcr_rc)${_ykcr_msg:+: $_ykcr_msg} — is the key still inserted?"
    fi
  fi
  rm -f "$_ykcr_err"
  printf "\n"

  # Guard: empty output means programming failure even with rc=0
  [[ -n "$ACTUAL_OUTPUT" ]] || die_loop "ykman otp calculate returned empty output on YubiKey #$KEY_COUNT — slot 2 programming may have failed"

  if [[ $KEY_COUNT -eq 1 ]]; then
    EXPECTED_OUTPUT="$ACTUAL_OUTPUT"
    ok "YubiKey #1 verified"
  else
    if [[ "$ACTUAL_OUTPUT" != "$EXPECTED_OUTPUT" ]]; then
      die_loop "YubiKey #$KEY_COUNT output differs from YubiKey #1 — programming failed"
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

# ── Require at least 2 keys (or backup password saved) ───────────────────────
if [[ $PROGRAMMED_COUNT -lt 2 ]]; then
  warn "Only 1 key was programmed. Backup YubiKey or password backup is REQUIRED."
  printf "\nChoose one of the following:\n"
  printf "  [1] Program a backup YubiKey now (recommended)\n"
  printf "  [2] Save vault password to password manager instead\n"
  printf "  [q] Quit and start over\n\n"
  printf "Choice [1/2/q]: "
  read -r BACKUP_CHOICE
  case "${BACKUP_CHOICE,,}" in
    1)
      printf "\nContinue programming more keys. Press Ctrl+C when done, or proceed below.\n"
      printf "Program another key? [y/N] "
      read -r ANOTHER_KEY
      if [[ "${ANOTHER_KEY,,}" == "y" ]]; then
        printf "\n"
        # Loop back to get another key
        KEY_COUNT=$((KEY_COUNT + 1))
        printf "── YubiKey #%d ──────────────────────────────────────\n" "$KEY_COUNT"
        printf "Remove the previous key. Insert YubiKey #%d and press Enter\n" "$KEY_COUNT"
        printf "(or press Enter with nothing inserted to finish): "
        read -r USER_INPUT
        if [[ -z "$USER_INPUT" ]]; then
          # User pressed Enter without key — require 2 keys total
          [[ $PROGRAMMED_COUNT -lt 2 ]] && die "At least 2 YubiKeys are required. Re-run to program another."
        else
          # Continue programming
          _current_serial=$("$_timeout" 10 ykman list --serials 2>/dev/null | head -n1) \
            || die "ykman timed out or failed listing devices — check USB connection"
          [[ -n "$_current_serial" ]] \
            || die "No YubiKey detected — insert YubiKey #$KEY_COUNT and retry"
          _key_count=$(ykman list --serials 2>/dev/null | wc -l)
          [[ "$_key_count" -le 1 ]] || die "Multiple YubiKeys detected ($_key_count) — remove extras and connect only one at a time"
          case ":$SEEN_SERIALS:" in
            *":$_current_serial:"*)
              die "Same YubiKey still inserted (serial $_current_serial) — remove it before programming key #$KEY_COUNT" ;;
          esac
          SEEN_SERIALS="${SEEN_SERIALS:+$SEEN_SERIALS:}$_current_serial"

          if ykman otp info 2>/dev/null | grep -q 'Slot 2: Programmed'; then
            warn "Slot 2 is already programmed on YubiKey #$KEY_COUNT — this will permanently overwrite it."
            printf "Overwrite slot 2? [y/N] "
            read -r SLOT2_CONFIRM
            [[ "${SLOT2_CONFIRM,,}" == "y" ]] || die "Aborted."
          fi

          _prog_err=$(mktemp)
          if ! printf '%s\ny\n' "$HMAC_SECRET" | \
               ykman otp chalresp --touch 2 2>"$_prog_err"; then
            _err_msg=$(cat "$_prog_err")
            rm -f "$_prog_err"
            if printf '%s' "$_err_msg" | grep -qiE 'BACKEND_ERROR|write error|access.?code'; then
              _err_msg="${_err_msg} — Slot 2 may have an access code set. Clear it with: ykman otp delete 2"
            fi
            die_loop "ykman otp chalresp failed on YubiKey #$KEY_COUNT: ${_err_msg:-no error output — is a YubiKey inserted?}"
          fi
          rm -f "$_prog_err"
          ok "YubiKey #$KEY_COUNT: slot 2 programmed"
          PROGRAMMED_COUNT=$((PROGRAMMED_COUNT + 1))

          printf "       Touch your YubiKey to verify (%ds timeout)... " "$CHALRESP_TIMEOUT"
          ACTUAL_OUTPUT=""
          _ykcr_err=$(mktemp)
          _ykcr_rc=0
          ACTUAL_OUTPUT=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>"$_ykcr_err") || _ykcr_rc=$?
          if [[ $_ykcr_rc -ne 0 ]]; then
            printf "\n"
            if [[ $_ykcr_rc -eq 124 ]]; then
              rm -f "$_ykcr_err"
              die_loop "Timed out waiting for YubiKey #$KEY_COUNT touch — touch the key when its light blinks."
            else
              _ykcr_msg=$(cat "$_ykcr_err")
              rm -f "$_ykcr_err"
              die_loop "ykman otp calculate failed on YubiKey #$KEY_COUNT (rc=$_ykcr_rc)${_ykcr_msg:+: $_ykcr_msg} — is the key still inserted?"
            fi
          fi
          rm -f "$_ykcr_err"
          printf "\n"

          [[ -n "$ACTUAL_OUTPUT" ]] || die_loop "ykman otp calculate returned empty output on YubiKey #$KEY_COUNT — slot 2 programming may have failed"

          if [[ "$ACTUAL_OUTPUT" != "$EXPECTED_OUTPUT" ]]; then
            die_loop "YubiKey #$KEY_COUNT output differs from YubiKey #1 — programming failed"
          fi
          ok "YubiKey #$KEY_COUNT verified (matches YubiKey #1)"
          printf "\n"

          # After programming second key, sufficient backup is in place
          [[ $PROGRAMMED_COUNT -ge 2 ]] && BACKUP_CHOICE="done"
        fi
      else
        die "2 YubiKeys required. Re-run to program a backup key."
      fi
      ;;
    2)
      # Will prompt for password backup below
      ;;
    q|*)
      die "Aborted. Program at least 2 YubiKeys before proceeding."
      ;;
  esac
fi

# ── Mandatory vault password backup (if only 1 YubiKey) ──────────────────────
if [[ $PROGRAMMED_COUNT -lt 2 && "${BACKUP_CHOICE:-}" != "done" ]]; then
  printf "\n%s\n" "═══════════════════════════════════════════════"
  printf "%s\n"   " VAULT PASSWORD BACKUP REQUIRED"
  printf "%s\n\n" "═══════════════════════════════════════════════"

  printf "You are using a single YubiKey without a backup.\n"
  printf "A backup of your vault password is REQUIRED to avoid permanent data loss.\n"
  printf "If your YubiKey is lost or damaged, this password is your only recovery path.\n\n"

  printf "Computing vault password from YubiKey...\n"
  # Get the vault password for display and backup
  VAULT_PASSWORD=""
  _vault_err=$(mktemp)
  _vault_rc=0
  VAULT_PASSWORD=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>"$_vault_err") || _vault_rc=$?

  if [[ $_vault_rc -ne 0 ]]; then
    printf "Touch YubiKey when ready (%ds timeout)... " "$CHALRESP_TIMEOUT"
    VAULT_PASSWORD=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>"$_vault_err") || _vault_rc=$?
  fi

  if [[ $_vault_rc -ne 0 ]]; then
    rm -f "$_vault_err"
    warn "Failed to compute vault password. Touch YubiKey and retry setup."
    die "YubiKey unavailable or timed out"
  fi
  rm -f "$_vault_err"

  if [[ -z "$VAULT_PASSWORD" ]]; then
    die "Vault password computation failed — empty response"
  fi

  printf "\n%s\n" "───────────────────────────────────────────────────"
  printf "%s\n"   " YOUR VAULT PASSWORD (save this immediately)"
  printf "%s\n"   "───────────────────────────────────────────────────"
  printf "\n"
  printf "%s\n" "$VAULT_PASSWORD"
  printf "\n"
  printf "%s\n" "───────────────────────────────────────────────────"
  printf "\nINSTRUCTIONS:\n\n"
  printf "1. COPY the password above (Ctrl+C to select, then Ctrl+Shift+C)\n"
  printf "2. Open your password manager (Bitwarden, 1Password, etc.)\n"
  printf "3. Create a new entry:\n"
  printf "   - Title: 'laptop-setup vault password' or similar\n"
  printf "   - Username: 'vault'\n"
  printf "   - Password: Paste from clipboard\n"
  printf "   - Notes: 'Ansible vault password for laptop-setup. Required if YubiKey is lost.'\n"
  printf "4. Save the entry\n"
  printf "5. Return here and confirm completion\n\n"

  printf "Have you saved the vault password to your password manager? [y/N]: "
  read -r BACKUP_CONFIRM

  if [[ "${BACKUP_CONFIRM,,}" != "y" ]]; then
    die "Backup password required. Save it to your password manager before continuing."
  fi

  ok "Vault password backup confirmed"
  printf "\n"
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
  cat > "$_tmp" << 'VAULTPASS'
#!/bin/bash
# scripts/vault-pass.sh — YubiKey HMAC-SHA1 vault password derivation
# Requires: ykman (yubikey-manager)  Touch YubiKey when its light blinks.
# ykman otp calculate requires hex-encoded challenge; pre-computed from ASCII.
set -euo pipefail
_CHALRESP_HEX=$(printf '%s' 'ansible-vault-laptop-setup' | od -An -tx1 | tr -d ' \n')
timeout 20 ykman otp calculate 2 "$_CHALRESP_HEX" 2>/dev/null || {
  echo "ERROR: YubiKey not available or touch timed out — insert YubiKey and retry" >&2
  exit 1
}
VAULTPASS
  mv "$_tmp" "$VAULT_PASS_SH"
  ok "vault-pass.sh written (mode 700, atomic rename)"

  # Update the SHA256 integrity reference to match the new YubiKey version
  sha256sum "$VAULT_PASS_SH" > "$SCRIPT_DIR/vault-pass.sh.sha256"
  ok "Updated vault-pass.sh.sha256 with new YubiKey version hash"

  # Verify vault-pass.sh works (requires a key still inserted)
  printf "       Touch YubiKey to verify vault-pass.sh (%ds)... " "$CHALRESP_TIMEOUT"
  VERIFY_OUTPUT=""
  if VERIFY_OUTPUT=$("$_timeout" "$CHALRESP_TIMEOUT" "$VAULT_PASS_SH" 2>/dev/null) \
     && [[ -n "$VERIFY_OUTPUT" ]] \
     && [[ "$VERIFY_OUTPUT" == "$EXPECTED_OUTPUT" ]]; then
    printf "\n"
    ok "vault-pass.sh verified — output matches"
  else
    printf "\n"
    warn "vault-pass.sh verification failed (YubiKey not inserted or removed?)"
    warn "vault-pass.sh was written correctly — verify manually with: scripts/vault-pass.sh"
    warn "If manual verify fails, re-run scripts/setup-yubikeys.sh to re-program all keys"
  fi
else
  warn "vault-pass.sh not updated. Update it manually (see SECURITY.md)."
fi

# ── Next steps ────────────────────────────────────────────────────────────────
printf "\n%s\n" "═══════════════════════════════════════════════"
printf " Done — %d YubiKey(s) programmed\n" "$PROGRAMMED_COUNT"
printf "%s\n\n" "═══════════════════════════════════════════════"
printf "Next steps:\n\n"
printf "  1. Set a strong FIDO2 PIN and enforce a minimum length (run in this order):\n"
printf "       ykman fido access set-min-length 12  # set 12-char floor first\n"
printf "                                            # ratchet: cannot shorten without full FIDO reset\n"
printf "                                            # (full reset wipes all resident keys)\n"
printf "       ykman fido access force-change        # invalidate any existing PIN shorter than floor\n"
printf "       ykman fido access change-pin          # set new PIN (must be >=12 chars)\n\n"
printf "  2. Generate your hardware SSH key (YubiKey must be inserted):\n"
printf "       # Without -O resident (portable; key stored as file referencing YubiKey):\n"
printf "       ssh-keygen -t ed25519-sk -O verify-required -f ~/.ssh/id_ed25519_sk\n"
printf "       # With -O resident (discoverable credential stored on YubiKey itself):\n"
printf "       ssh-keygen -t ed25519-sk -O resident -O verify-required -f ~/.ssh/id_ed25519_sk\n"
printf "     Add the public key to GitHub:\n"
printf "       gh ssh-key add ~/.ssh/id_ed25519_sk.pub --title 'YubiKey'\n\n"
printf "  3. Populate group_vars/all/vault.yml with your keys:\n"
if grep -qF "\$ANSIBLE_VAULT" "$SCRIPT_DIR/../group_vars/all/vault.yml" 2>/dev/null; then
  printf "       ansible-vault rekey group_vars/all/vault.yml    # re-setup: old YubiKey must still be available\n"
  printf "       ansible-vault edit group_vars/all/vault.yml\n\n"
else
  printf "       ansible-vault encrypt group_vars/all/vault.yml  # encrypt first\n"
  printf "       ansible-vault edit group_vars/all/vault.yml     # then paste keys\n\n"
fi
printf "  4. Deploy the keys:\n"
printf "       make ssh\n\n"
printf "  5. Optional: Set up PIV key for age-plugin-yubikey (EC P-384; stronger encryption):\n"
printf "       ykman piv keys generate --algorithm ECCP384 9a\n"
printf "       ykman piv certificates generate --subject 'age-yubikey' 9a\n"
printf "       age-plugin-yubikey  # follow prompts to get recipient string\n\n"
printf "  6. Reboot — SSH authorized_keys is now deployed; port 722 is safe.\n\n"
warn "Store each YubiKey in a different physical location."
if [[ $PROGRAMMED_COUNT -ge 2 ]]; then
  warn "Multiple YubiKeys programmed — vault is protected by hardware redundancy."
  warn "The HMAC secret was NOT saved, but you can use any YubiKey to access the vault."
else
  warn "Single YubiKey in use — vault password backup saved to password manager."
  warn "If YubiKey is lost, retrieve the vault password from your password manager."
fi
printf "\n"
