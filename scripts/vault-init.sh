#!/usr/bin/env bash
# vault-init.sh — First-time vault setup after `make setup-yubikeys`:
#   1. git update-index --skip-worktree group_vars/all/vault.yml (the committed file stays the stub)
#   2. ansible-vault encrypt group_vars/all/vault.yml (password from the YubiKey via vault-pass.sh)
#   3. ansible-vault edit to add the keys (same as `make vault-edit`)
# Idempotent: steps already done are skipped. Usage: make vault-init
set -euo pipefail
# SECURITY: ansible-vault runs vault-pass.sh, which prints the vault password; never trace it.
{ set +x; } 2>/dev/null

cd "$(dirname "$0")/.."
VAULT=group_vars/all/vault.yml
PASS_SH=scripts/vault-pass.sh

die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }

[[ $EUID -ne 0 ]] || die "run as your user, not root"
[[ -f $VAULT ]] || die "$VAULT not found"
[[ -x $PASS_SH ]] || die "$PASS_SH missing: run make setup-yubikeys first"
if grep -qE "^pass=['\"]CHANGE_ME['\"]|ci-dummy-vault-password" "$PASS_SH"; then
  die "$PASS_SH is still the stub: run make setup-yubikeys first (encrypting with the stub password protects nothing)"
fi

# Skip-worktree first, so the encrypted vault can never be staged over the committed stub.
if git ls-files -v -- "$VAULT" | grep -q '^S '; then
  ok "$VAULT already marked skip-worktree"
else
  git update-index --skip-worktree -- "$VAULT"
  ok "$VAULT marked skip-worktree (git ignores local changes to it)"
fi

if head -c 14 "$VAULT" | grep -qF "\$ANSIBLE_VAULT"; then
  ok "$VAULT already encrypted"
else
  printf 'Encrypting %s (insert the YubiKey and touch it when it blinks)...\n' "$VAULT"
  scripts/verify-ansible-args.sh ansible-vault encrypt "$VAULT"
  ok "$VAULT encrypted"
fi

printf 'Opening the vault to add your keys (format: see the file header; touch the YubiKey)...\n'
EDITOR='' VISUAL='' ANSIBLE_EDITOR='' scripts/verify-ansible-args.sh ansible-vault edit "$VAULT"
ok "vault saved. Next: make chunk-ssh_client"
