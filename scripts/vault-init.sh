#!/usr/bin/env bash
# vault-init.sh (make vault-init) — first-time vault setup after make setup-yubikeys: mark the vault skip-worktree
# (the committed file stays the stub), encrypt it, open the editor, then check the result parses as YAML.
# Done steps are skipped; the editor opens every time. Generate the SSH keys first (setup-yubikeys step 2).
set -euo pipefail
# SECURITY: ansible-vault runs vault-pass.sh, which prints the vault password; never trace it.
{ set +x; } 2>/dev/null

cd "$(dirname "$0")/.."
VAULT=group_vars/all/vault.yml
PASS_SH=scripts/vault-pass.sh
DISPATCH=scripts/verify-ansible-args.sh

die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }

[[ -f $PASS_SH && -x $PASS_SH ]] || die "$PASS_SH missing or not executable: run make setup-yubikeys first"
if grep -qE "^[[:space:]]*(export[[:space:]]+)?pass=['\"]?CHANGE_ME|ci-dummy-vault-password" "$PASS_SH"; then
  die "$PASS_SH is still the stub: run make setup-yubikeys first (encrypting with the stub password protects nothing)"
fi

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
printf 'Opening the vault in vi (touch the YubiKey; format: comments at the top). For the private key, change %s to |\n' "''"
printf 'on its line, then with the cursor there run :r !sed "s/^/  /" ~/.ssh/id_ed25519_sk  and :wq to save.\n'
"$DISPATCH" ansible-vault edit "$VAULT" || die "ansible-vault edit failed (YubiKey not inserted or not touched in time?): re-run make vault-init"
if [[ $(cksum < "$VAULT") == "$before" ]]; then
  ok "vault unchanged (nothing saved). Add the keys later with make vault-edit"
  exit 0
fi
printf 'Checking the saved vault parses as YAML (touch the YubiKey again)...\n'
if ! "$DISPATCH" ansible-vault view "$VAULT" | python3 -I -c '
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)  # no PyYAML for this python3: skip the check
yaml.safe_load(sys.stdin)' >/dev/null 2>&1; then
  die "the saved vault is not valid YAML (or could not be decrypted): fix it with make vault-edit (format: comments at the top)"
fi
ok "vault saved. Next: CLAUDE.md steps 3a-3d (usb-add, preflight, backup), then make chunk-ssh_client"
