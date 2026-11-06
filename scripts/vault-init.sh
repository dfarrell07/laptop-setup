#!/usr/bin/env bash
# vault-init.sh — First-time vault setup after `make setup-yubikeys`:
#   1. git update-index --skip-worktree group_vars/all/vault.yml (the committed file stays the stub)
#   2. ansible-vault encrypt group_vars/all/vault.yml (password from the YubiKey via vault-pass.sh)
#   3. ansible-vault edit to add the keys (same as `make vault-edit`)
# Steps 1 and 2 are skipped when already done; step 3 runs every time. Usage: make vault-init
# Generate the SSH keys first (setup-yubikeys "Next steps" 2): the editor needs their contents.
set -euo pipefail
# SECURITY: ansible-vault runs vault-pass.sh, which prints the vault password; never trace it.
{ set +x; } 2>/dev/null

cd "$(dirname "$0")/.."
VAULT=group_vars/all/vault.yml
PASS_SH=scripts/vault-pass.sh
DISPATCH=scripts/verify-ansible-args.sh

die()  { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33mWARN:  %s\033[0m\n' "$*" >&2; }

[[ $EUID -ne 0 ]] || die "run as your user, not root"
[[ -f $VAULT ]] || die "$VAULT not found"
[[ -f $PASS_SH ]] || die "$PASS_SH missing: run make setup-yubikeys first"
[[ -x $PASS_SH ]] || die "$PASS_SH is not executable: chmod 700 $PASS_SH"
if grep -qE "^[[:space:]]*(export[[:space:]]+)?pass=['\"]?CHANGE_ME|ci-dummy-vault-password" "$PASS_SH" \
   || cmp -s "$PASS_SH" scripts/vault-pass-ci.sh; then
  die "$PASS_SH is still the stub: run make setup-yubikeys first (encrypting with the stub password protects nothing)"
fi
[[ -t 0 && -t 1 ]] || die "needs a terminal (it opens an editor and waits for a YubiKey touch)"

# Skip-worktree first, so the encrypted vault can never be staged over the committed stub.
# Side effect: a pull that changes the committed stub aborts ("would be overwritten"); SECURITY.md
# § "Setting Up vault-pass.sh" has the steps.
if git ls-files -v -- "$VAULT" | grep -q '^[Ss] '; then
  ok "$VAULT already marked skip-worktree"
else
  git update-index --skip-worktree -- "$VAULT" || die "git update-index failed: is $VAULT tracked in this checkout?"
  ok "$VAULT marked skip-worktree (git ignores local changes to it)"
fi

if head -c 14 "$VAULT" | grep -qF "\$ANSIBLE_VAULT"; then
  ok "$VAULT already encrypted"
else
  printf 'Encrypting %s (insert the YubiKey and touch it when it blinks)...\n' "$VAULT"
  "$DISPATCH" ansible-vault encrypt "$VAULT" \
    || die "encrypt failed: insert the YubiKey and re-run make vault-init (safe to repeat)"
  ok "$VAULT encrypted"
fi

before=$(cksum < "$VAULT")
printf 'Opening the vault in vi (touch the YubiKey). Paste key files indented 2 spaces under "key: |";\n'
printf 'run :set paste before pasting, then :wq to save. Format: see the comments at the top.\n'
if ! "$DISPATCH" ansible-vault edit "$VAULT"; then
  die "ansible-vault edit failed. If the vault was encrypted earlier with a different password (e.g. the
       old stub), restore and re-run: git update-index --no-skip-worktree $VAULT;
       git checkout -- $VAULT; make vault-init"
fi
if [[ $(cksum < "$VAULT") == "$before" ]]; then
  warn "vault unchanged: vault_ssh_auth_key(_pub) are still empty. Generate the keys (setup-yubikeys step 2), then make vault-edit"
  exit 0
fi
ok "vault saved. Next: make chunk-ssh_client (later changes: make vault-edit)"
