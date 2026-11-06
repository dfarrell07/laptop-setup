#!/usr/bin/env bash
# setup-yubikeys.sh — Program one or more YubiKeys with the same HMAC-SHA1 secret
# and write vault-pass.sh for ansible-vault integration.
#
# Usage:  scripts/setup-yubikeys.sh
#
# Requires: ykman (yubikey-manager — installed by 'make bootstrap')
# Run after: make bootstrap, before make all (CLAUDE.md first-time steps)
# Run before: make vault-init (encrypts group_vars/all/vault.yml)
#
# Design:
#   - Generates a random 20-byte HMAC secret; programs every YubiKey with it
#   - Secret is delivered to ykman via stdin (ykman has no stdin placeholder;
#     piped without --force to avoid /proc/<pid>/cmdline exposure; fragile —
#     relies on Click reading key prompt then confirm prompt sequentially from stdin)
#   - Secret is NOT written to disk
#   - Verifies each key produces identical challenge-response output
#   - Writes scripts/vault-pass.sh atomically with 700 permissions
#   - Prompts for further keys; finish with Enter (no key inserted) or by answering n
#   - HMAC-SHA1 vault derivation: 80-bit post-quantum security (Grover);
#     acceptable for now (see SECURITY.md 'Vault password strength'); monitor for HMAC-SHA256 YubiKey OTP support
set -euo pipefail
# SECURITY: this script handles the HMAC secret and the vault password; never let xtrace record them.
{ set +x; } 2>/dev/null

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
# Status 0: a YubiKey is listed by ykman; 1: none listed; 2: ykman failed or timed out
_key_inserted() { local s; s=$("$_timeout" 10 ykman list --serials 2>/dev/null) || return 2; [[ -n "$s" ]]; }

# ── Ctrl+C / signal cleanup ───────────────────────────────────────────────────
_tmp='' _prog_err='' _ykcr_err=''
trap 'rm -f "$_tmp" "$_prog_err" "$_ykcr_err"' EXIT
_cleanup_msg() {
  printf "\n\nInterrupted.\n" >&2
  if [[ ${PROGRAMMED_COUNT:-0} -gt 0 || ${_slot_write_started:-false} == true ]]; then
    printf "Any YubiKeys already programmed in this run have the new secret.\n" >&2
    printf "The secret is not recoverable: re-running setup-yubikeys.sh generates a NEW\n" >&2
    printf "secret, so ALL keys must be re-programmed from scratch.\n" >&2
  else
    printf "No YubiKey was changed.\n" >&2
  fi
}
trap '_cleanup_msg; exit 130' INT
trap '_cleanup_msg; exit 143' TERM

_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }  # bash 3.2 has no ${var,,}

# ── Prerequisites ─────────────────────────────────────────────────────────────
if command -v timeout &>/dev/null; then
  _timeout=timeout
elif command -v gtimeout &>/dev/null; then
  _timeout=gtimeout
else
  die "'timeout' not found. On macOS: brew install coreutils"
fi
command -v ykman &>/dev/null || die "'ykman' not found. Install it: 'make bootstrap' or sudo dnf install -y yubikey-manager"
[[ -t 0 ]] || die "stdin is not a terminal — this script must be run interactively"

printf "\n%s\n" "═══════════════════════════════════════════════"
printf "%s\n"   " YubiKey HMAC-SHA1 Setup"
printf "%s\n\n" "═══════════════════════════════════════════════"
printf "Programs one or more YubiKeys with the same HMAC-SHA1 secret so each\n"
printf "produces the same ansible-vault password for this machine.\n\n"
warn "Program ALL your YubiKeys now in a single run — the secret is never saved."
warn "Interrupting after key #1 is programmed means you must re-run and start over."
printf "\n"

# ── Refuse to orphan an encrypted vault ──────────────────────────────────────
# The new secret replaces the one in slot 2, so a vault encrypted with the old password could
# never be decrypted again. Decrypt it first (with a currently programmed key), then re-run.
VAULT_FILE="$SCRIPT_DIR/../group_vars/all/vault.yml"
if head -c 14 "$VAULT_FILE" 2>/dev/null | grep -qF "\$ANSIBLE_VAULT"; then
  die "group_vars/all/vault.yml is encrypted with the current YubiKey password. Reprogramming
       would make it permanently undecryptable. First decrypt it with a currently programmed key:
         ansible-vault decrypt group_vars/all/vault.yml
       then re-run make setup-yubikeys and encrypt it again with the new keys."
fi

# Decide about an existing vault-pass.sh before anything irreversible happens
if [[ -f "$VAULT_PASS_SH" ]] && ! grep -qE "^pass=['\"]CHANGE_ME['\"]|ci-dummy-vault-password" "$VAULT_PASS_SH"; then
  warn "vault-pass.sh already exists and is not a placeholder stub."
  printf "Overwrite it with the new YubiKey implementation? [y/N] "
  read -r OVERWRITE
  [[ "$(_lc "$OVERWRITE")" == "y" ]] || die "Aborted: nothing was changed (reprogramming without updating vault-pass.sh would orphan it)."
fi

# ── Generate and validate HMAC secret ────────────────────────────────────────
HMAC_SECRET=$(od -An -N20 -tx1 /dev/urandom | tr -d ' \n') || die "reading /dev/urandom failed"
[[ "$HMAC_SECRET" =~ ^[0-9a-f]{40}$ ]] || die "malformed secret (want 40 hex chars) — entropy source may be broken"

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
    printf "(or press Enter with nothing inserted when you are done): "
  fi
  read -r _

  # After key #1, no key inserted = done (a single key needs a backup key or a saved password)
  _ks=0
  if [[ $KEY_COUNT -ge 2 ]]; then
    _key_inserted || _ks=$?
    if [[ $_ks -eq 2 ]]; then
      warn "ykman failed listing devices — check the USB connection and try again."
      KEY_COUNT=$((KEY_COUNT - 1))
      continue
    fi
  fi
  if [[ $KEY_COUNT -ge 2 && $_ks -eq 1 ]]; then
    if [[ $PROGRAMMED_COUNT -ge 2 ]]; then
      info "No more keys — finishing."
      break
    fi
    warn "Only 1 key was programmed. A backup YubiKey or a saved vault password is REQUIRED."
    printf "\n  [1] Program a backup YubiKey now (recommended)\n"
    printf "  [2] Save the vault password to a password manager instead\n"
    printf "  [q] Quit (the secret is lost; re-run and re-program all keys)\n\n"
    printf "Choice [1/2/q]: "
    read -r BACKUP_CHOICE
    case "$(_lc "$BACKUP_CHOICE")" in
      1) KEY_COUNT=$((KEY_COUNT - 1)); printf "\n"; continue ;;
      2) printf "Re-insert a programmed YubiKey and press Enter: "; read -r _; break ;;
      *) die_loop "Aborted. Re-run and program a backup YubiKey or save the vault password." ;;
    esac
  fi

  # Reject duplicate-key reprogramming by comparing serials before programming.
  # Nothing has been written to this key yet, so problems here re-prompt instead of aborting.
  _serials=$("$_timeout" 10 ykman list --serials 2>/dev/null) || {
    warn "ykman timed out or failed listing devices — check the USB connection and try again."
    KEY_COUNT=$((KEY_COUNT - 1))
    continue
  }
  _current_serial=$(printf '%s\n' "$_serials" | head -n1)
  if [[ -z "$_current_serial" ]]; then
    warn "No YubiKey detected — insert YubiKey #$KEY_COUNT and try again."
    KEY_COUNT=$((KEY_COUNT - 1))
    continue
  fi
  _key_count=$(printf '%s\n' "$_serials" | grep -c . || true)
  if [[ "$_key_count" -gt 1 ]]; then
    warn "Multiple YubiKeys detected ($_key_count) — remove extras and connect only one at a time."
    KEY_COUNT=$((KEY_COUNT - 1))
    continue
  fi
  case ":$SEEN_SERIALS:" in
    *":$_current_serial:"*)
      warn "Same YubiKey still inserted (serial $_current_serial) — remove it first (nothing was changed)."
      KEY_COUNT=$((KEY_COUNT - 1))
      continue ;;
  esac

  _otp_info=$("$_timeout" 10 ykman otp info 2>/dev/null) || {
    warn "ykman otp info failed — cannot check whether slot 2 is already programmed; try again."
    KEY_COUNT=$((KEY_COUNT - 1))
    continue
  }
  if printf '%s\n' "$_otp_info" | grep -qiE 'slot 2: *programmed'; then
    warn "Slot 2 is already programmed on YubiKey #$KEY_COUNT — this will permanently overwrite it."
    printf "Overwrite slot 2? [y/N] "
    read -r SLOT2_CONFIRM
    if [[ "$(_lc "$SLOT2_CONFIRM")" != "y" ]]; then
      warn "Slot 2 left unchanged; this key was skipped."
      KEY_COUNT=$((KEY_COUNT - 1))
      continue
    fi
  fi
  SEEN_SERIALS="${SEEN_SERIALS:+$SEEN_SERIALS:}$_current_serial"

  # Program slot 2 — secret delivered via stdin to avoid /proc/<pid>/cmdline exposure.
  # ykman otp chalresp has no stdin placeholder for the key. Using --force requires
  # the key as a positional arg (visible in /proc/<pid>/cmdline and ps output). Instead
  # we pipe without --force: ykman reads the key from stdin (click_prompt), then reads
  # the confirmation from stdin (click.confirm uses err=True — prompt text goes to
  # stderr, but the answer is still read from stdin). Input order: <key>\n<y>\n.
  # This relies on Click's undocumented stdin behaviour and may break if ykman changes its prompt order.
  _prog_err=$(mktemp)
  _slot_write_started=true  # an interrupt from here on may leave the new secret in slot 2
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
  _try=1
  while :; do
    _ykcr_rc=0
    ACTUAL_OUTPUT=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>"$_ykcr_err") || _ykcr_rc=$?
    [[ $_ykcr_rc -eq 124 && $_try -lt 2 ]] || break
    _try=$((_try + 1))
    printf "\n       Timed out. Touch the key when its light blinks (%ds)... " "$CHALRESP_TIMEOUT"
  done
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
    [[ "$(_lc "$ANOTHER")" == "y" ]] || break
    printf "\n"
  fi
done

# ── Mandatory vault password backup (if only 1 YubiKey) ──────────────────────
if [[ $PROGRAMMED_COUNT -lt 2 ]]; then
  printf "\n%s\n" "═══════════════════════════════════════════════"
  printf "%s\n"   " VAULT PASSWORD BACKUP REQUIRED"
  printf "%s\n\n" "═══════════════════════════════════════════════"

  printf "You are using a single YubiKey without a backup.\n"
  printf "A backup of your vault password is REQUIRED to avoid permanent data loss.\n"
  printf "If your YubiKey is lost or damaged, this password is your only recovery path.\n\n"

  printf "Computing vault password from YubiKey (touch it if it blinks)...\n"
  # Get the vault password for display and backup
  VAULT_PASSWORD=""
  _vault_rc=0
  VAULT_PASSWORD=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>/dev/null) || _vault_rc=$?

  if [[ $_vault_rc -ne 0 ]]; then
    printf "Touch YubiKey when ready (%ds timeout)... " "$CHALRESP_TIMEOUT"
    _vault_rc=0
    VAULT_PASSWORD=$("$_timeout" "$CHALRESP_TIMEOUT" ykman otp calculate 2 "$_CHALRESP_HEX" 2>/dev/null) || _vault_rc=$?
  fi

  if [[ $_vault_rc -ne 0 ]]; then
    warn "Failed to compute the vault password. The key is programmed: re-insert it, touch it when it blinks, and run: ykman otp calculate 2 $_CHALRESP_HEX"
    die "YubiKey unavailable or timed out; vault-pass.sh was NOT rewritten (re-run to start over)"
  fi

  if [[ -z "$VAULT_PASSWORD" ]]; then
    die "Vault password computation failed (empty response); vault-pass.sh was NOT rewritten (re-run to start over)"
  fi
  [[ "$VAULT_PASSWORD" == "$EXPECTED_OUTPUT" ]] \
    || die "The inserted YubiKey does not return the vault password programmed in this run — insert the key you just programmed"

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

  if [[ "$(_lc "$BACKUP_CONFIRM")" != "y" ]]; then
    die "Backup password required. Save it to your password manager before continuing. The key already holds the new secret and vault-pass.sh was NOT rewritten: re-run to start over."
  fi

  ok "Vault password backup confirmed"
  printf "\n"
fi

# ── Write vault-pass.sh (atomic: temp file → chmod → rename) ─────────────────
printf "\n── Writing vault-pass.sh ─────────────────────────────\n"

if [[ -f "$VAULT_PASS_SH" ]] && grep -qE "^pass=['\"]CHANGE_ME['\"]|ci-dummy-vault-password" "$VAULT_PASS_SH"; then
  info "Replacing placeholder stub with YubiKey implementation."
fi

# Write to temp file first (atomic: never a window where the real file
# has wrong content or wrong permissions)
_tmp=$(mktemp "${VAULT_PASS_SH}.XXXXXX")
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
ok "vault-pass.sh written (mode 700, atomic rename)"

# Update the SHA256 integrity reference to match the new YubiKey version
# Use relative path (basename only) so sha256sum -c works from any clone location
if command -v sha256sum &>/dev/null; then _sha256=(sha256sum); else _sha256=(shasum -a 256); fi
(cd "$SCRIPT_DIR" && "${_sha256[@]}" "$(basename "$VAULT_PASS_SH")" > vault-pass.sh.sha256)
ok "Updated vault-pass.sh.sha256 with new YubiKey version hash (relative path)"

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
  _verify_failed=true
  warn "vault-pass.sh verification failed (YubiKey not inserted or removed?)"
  warn "vault-pass.sh was written; verify it manually with: scripts/vault-pass.sh"
  warn "If a manual verify with the key touched also fails, re-run scripts/setup-yubikeys.sh (new secret, re-program all keys)"
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
printf "       ykman fido access force-change        # only if a PIN is already set: invalidates one shorter than the floor\n"
printf "       ykman fido access change-pin          # set (or change) the PIN (must be >=12 chars)\n\n"
printf "  2. Generate your hardware SSH key (YubiKey must be inserted):\n"
printf "       # Without -O resident (portable; key stored as file referencing YubiKey):\n"
printf "       ssh-keygen -t ed25519-sk -O verify-required -f ~/.ssh/id_ed25519_sk\n"
printf "       # With -O resident (discoverable credential stored on YubiKey itself):\n"
printf "       ssh-keygen -t ed25519-sk -O resident -O verify-required -f ~/.ssh/id_ed25519_sk\n"
printf "     Add the public key to GitHub (token needs the admin:public_key scope: gh auth refresh -h github.com -s admin:public_key):\n"
printf "       gh ssh-key add ~/.ssh/id_ed25519_sk.pub --title 'YubiKey'\n"
printf "     Optional commit-signing key: SECURITY.md § SSH Key Rotation.\n\n"
printf "  3. Populate group_vars/all/vault.yml with your keys (from now on every Ansible make target needs the key inserted and a touch):\n"
printf "       make vault-init  # skip-worktree (the committed vault stays a stub), encrypt, then paste keys\n"
printf "                        # (multi-line format: see the file header; later edits: make vault-edit)\n\n"
printf "  4. Deploy the keys (CLAUDE.md steps 3a-3c first: usb-add, preflight, backup):\n"
printf "       make chunk-ssh_client\n\n"
printf "  5. Last, at the console with a second session open: make chunk-ssh_authorized_keys (see docs/rollout-plan.md).\n"
printf "     Reboot only after it wrote authorized_keys; otherwise SSH login on port 722 fails.\n\n"
warn "Store each YubiKey in a different physical location."
if [[ $PROGRAMMED_COUNT -ge 2 ]]; then
  warn "Multiple YubiKeys programmed — any of them opens the vault (the HMAC secret was NOT saved)."
else
  warn "Single YubiKey in use — the vault password backup is in your password manager (confirmed at the prompt)."
  warn "If the YubiKey is lost, retrieve the vault password from your password manager."
fi
printf "\n"
[[ "${_verify_failed:-false}" == false ]] || { warn "Exiting non-zero: the new vault-pass.sh could not be verified."; exit 1; }
