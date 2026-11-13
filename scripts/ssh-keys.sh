#!/usr/bin/env bash
# ssh-keys.sh (make ssh-keys) — after make setup-yubikeys, one YubiKey at a time: set its FIDO2 PIN (6-character
# floor) if none is set, then make a resident ed25519-sk SSH key. The first key file is ~/.ssh/id_ed25519_sk, later ones
# (backup YubiKeys) id_ed25519_sk_backup, _backup2, ... A YubiKey that already has a key file (comment yubikey-<serial>)
# is skipped, so a re-run only adds new keys. Next: make vault-init.
set -euo pipefail

die()  { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33mWARN:  %s\033[0m\n' "$*"; }

[[ -t 0 ]] || die "stdin is not a terminal: it asks for the YubiKey PIN and a touch"
for tool in ykman ssh-keygen; do command -v "$tool" >/dev/null || die "$tool not found: run make bootstrap"; done
SSH_DIR=$HOME/.ssh
install -d -m 700 "$SSH_DIR"
# A key made by hand has no yubikey-<serial> comment, so its YubiKey would get a second (or overwritten) credential.
if [[ -e $SSH_DIR/id_ed25519_sk || -e $SSH_DIR/id_ed25519_sk.pub ]] && ! grep -qs ' yubikey-[0-9][0-9]*$' "$SSH_DIR/id_ed25519_sk.pub"; then
  die "$SSH_DIR/id_ed25519_sk was not made by make ssh-keys: move id_ed25519_sk* aside (or end its .pub line with yubikey-<serial>)"
fi

done_serials='' added=()
while :; do
  printf '\nInsert one YubiKey (the next one to set up) and press Enter; press Enter with none inserted when done: '
  read -r _ || die "no input: run make ssh-keys in a terminal"
  if ! devices=$(ykman list 2>/dev/null) || ! serial=$(ykman list --serials 2>/dev/null); then
    warn "ykman could not list YubiKeys: check the USB connection"; continue
  fi
  [[ $devices != *'<access denied>'* ]] || die "no permission to open the YubiKey: run make ssh-keys at the local console"
  case $(printf '%s' "$devices" | grep -c . || true) in
    0) [[ -n $done_serials ]] && break; warn "no YubiKey found: insert one (Ctrl+C to quit)"; continue ;;
    1) [[ -n $serial ]] || die "this YubiKey reports no serial (Security Key series?): use YubiKey 5 series keys" ;;
    *) warn "more than one YubiKey inserted: leave only one"; continue ;;
  esac
  case " $done_serials " in *" $serial "*) warn "YubiKey $serial is done: swap in the next one"; continue ;; esac

  existing=$(grep -l " yubikey-$serial\$" "$SSH_DIR"/id_ed25519_sk.pub "$SSH_DIR"/id_ed25519_sk_backup*.pub 2>/dev/null | head -n1 || true)
  if [[ -n $existing ]]; then
    ok "YubiKey $serial already has ${existing%.pub}"
  else
    info=$(ykman fido info 2>/dev/null) || die "ykman fido info failed on YubiKey $serial (FIDO2 disabled? ykman config usb)"
    pin=$(printf '%s\n' "$info" | sed -n 's/^PIN: *//p')
    case $pin in
      'Not set')
        printf 'YubiKey %s has no FIDO2 PIN: choose one of 6 or more characters.\n' "$serial"
        ykman fido access set-min-length 6 || warn "could not set the 6-character floor: still pick 6 or more"
        ykman fido access change-pin || die "setting the PIN failed: re-run make ssh-keys" ;;
      [0-9]*' attempt'*)
        [[ $info != *'must be changed'* ]] || die "YubiKey $serial must change its FIDO2 PIN first: ykman fido access change-pin"
        [[ $info != *'temporarily blocked'* ]] || die "YubiKey $serial PIN is temporarily blocked: re-insert it and re-run make ssh-keys"
        ok "YubiKey $serial already has a FIDO2 PIN (kept)"
        [[ $(printf '%s\n' "$info" | sed -n 's/^Minimum PIN length: *//p') -ge 6 ]] \
          || warn "its PIN may be shorter than 6: ykman fido access set-min-length 6 (forces a PIN change only if it is shorter)" ;;
      *) die "YubiKey $serial FIDO2 PIN state is '$pin' (blocked: ykman fido reset wipes its FIDO keys; disabled: ykman config usb)" ;;
    esac
    key=$SSH_DIR/id_ed25519_sk
    if [[ -e $key || -e $key.pub ]]; then
      key=$SSH_DIR/id_ed25519_sk_backup n=2
      while [[ -e $key || -e $key.pub ]]; do key=$SSH_DIR/id_ed25519_sk_backup$n n=$((n + 1)); done
    fi
    printf 'Making %s: enter the PIN, then touch the YubiKey when it blinks.\n' "$key"
    # Own user id (the short host name, max 31 bytes), so a resident ssh: credential already on the YubiKey (another
    # machine's, default empty user id) is kept, not overwritten.
    host=${HOSTNAME%%.*}
    ssh-keygen -q -t ed25519-sk -O resident -O verify-required -O "user=${host:0:31}" -N '' -C "yubikey-$serial" -f "$key" \
      || die "ssh-keygen failed on YubiKey $serial: re-run make ssh-keys (finished keys are kept)"
    ok "made $key"
    added+=("$key.pub")
  fi
  done_serials="$done_serials $serial"
done

compgen -G "$SSH_DIR/id_ed25519_sk_backup*.pub" >/dev/null \
  || warn "no backup YubiKey SSH key: losing this YubiKey locks SSH out. Re-run make ssh-keys with the backup."
if [[ ${#added[@]} -gt 0 ]]; then
  printf 'Add the new public keys to GitHub now (make github-ssh-keys)? [y/N] '
  read -r answer || answer=
  # Without this, SSH to GitHub fails later (smoke check github-ssh-auth) with no hint of why.
  if [[ $answer != [yY] ]] || ! "$(dirname "$0")/github-ssh-keys.sh"; then
    warn "the new keys may not be on GitHub yet: make github-ssh-keys"
  fi
fi
# vault-init fills only the empty stub lines: keys added after that go in by hand.
if grep -qx "vault_ssh_auth_key_pub: ''" "$(dirname "$0")/../group_vars/all/vault.yml" 2>/dev/null; then
  ok "Next: insert one YubiKey, then make vault-init"
elif [[ ${#added[@]} -gt 0 ]]; then
  warn "the vault already has SSH keys: add each new .pub (${added[*]}) as a line of vault_ssh_auth_key_pub (SECURITY.md § Replacing SSH keys)"
fi
