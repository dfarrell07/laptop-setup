#!/usr/bin/env bash
# vault-import.sh (make vault-import FROM=...) — set up a new machine with the encrypted vault another machine already
# uses (docs/migration-checklist.md "Prepare the destination"), instead of make setup-yubikeys, which would generate a
# new secret and orphan that vault.
#
#   FROM=PATH        an encrypted vault.yml, or a directory holding one (a make backup directory, a checkout)
#   FROM=HOST:PATH   the same over SSH (scp), e.g. FROM=laptop:src/laptop-setup/group_vars/all/vault.yml
#
# Steps: write the standard scripts/vault-pass.sh (no secret in it), fetch the vault to a temp file, check it is an
# Ansible vault, prove a YubiKey opens it (touch the key; the plaintext goes to /dev/null), install it 0600 and mark it
# skip-worktree (make vault-init). Nothing replaces the local vault until the key has opened the new one.
set -euo pipefail
# SECURITY: ansible-vault runs vault-pass.sh, which prints the vault password; never trace it.
{ set +x; } 2>/dev/null
cd "$(dirname "$0")/.."
VAULT=group_vars/all/vault.yml
PASS_SH=scripts/vault-pass.sh
DISPATCH=scripts/verify-ansible-args.sh
die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }
is_vault() { head -c 14 "$1" 2>/dev/null | grep -qF "\$ANSIBLE_VAULT"; }

FROM=${1:-}
[[ -n $FROM ]] || die "usage: make vault-import FROM=PATH|HOST:PATH (an encrypted vault.yml, or a directory with one)"
[[ $FROM != -* ]] || die "FROM must not start with '-': $FROM"
if is_vault "$VAULT"; then
  die "$VAULT is already encrypted here: nothing to import (view it with make vault-view)"
fi

# Until the import is committed, any exit (an error, Ctrl-C at the touch prompt) puts the previous helper and its
# baseline back, or removes the new ones when there were none: with the YubiKey helper in place and no vault it opens,
# every Ansible run would ask for a key.
tmp=$(mktemp "${VAULT}.import.XXXXXX") prev=$(mktemp) prev_sha=$(mktemp)
wrote=false committed=false had_prev=false had_sha=false stub=true
restore() {
  if $had_prev; then cp -p "$prev" "$PASS_SH"; else rm -f "$PASS_SH"; fi
  if $had_sha; then cp -p "$prev_sha" scripts/vault-pass.sh.sha256; else rm -f scripts/vault-pass.sh.sha256; fi
}
cleanup() {
  local rc=$?
  if $wrote && ! $committed; then restore; fi
  rm -f "$tmp" "$prev" "$prev_sha"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
chmod 600 "$tmp"
if [[ -f $PASS_SH ]]; then
  cp -p "$PASS_SH" "$prev" && had_prev=true
  grep -qE "^pass=['\"]CHANGE_ME['\"]|ci-dummy-vault-password" "$PASS_SH" || stub=false
fi
if [[ -f scripts/vault-pass.sh.sha256 ]]; then cp -p scripts/vault-pass.sh.sha256 "$prev_sha" && had_sha=true; fi

# Fetch. A local path wins over HOST:PATH, so a file name with a colon still works.
FROM=${FROM%/}
if [[ -e $FROM ]]; then
  src=$FROM
  if [[ -d $FROM ]]; then
    # a checkout or a copied folder has vault.yml at the top; a make backup directory holds
    # <repo path>/group_vars/all/vault.yml
    found=()
    [[ ! -f $FROM/vault.yml ]] || found+=("$FROM/vault.yml")
    while IFS= read -r f; do found+=("$f"); done < <(
      find "$FROM" -maxdepth 6 -type f -path '*/group_vars/all/vault.yml' 2>/dev/null)
    [[ ${#found[@]} -eq 1 ]] || die "expected exactly one vault.yml under $FROM, found ${#found[@]}: give the file itself"
    src=${found[0]}
  fi
  cat -- "$src" >"$tmp" || die "cannot read $src"
elif [[ $FROM == *:* ]]; then
  printf 'Copying %s over SSH...\n' "$FROM"
  scp -q -- "$FROM" "$tmp" || die "scp $FROM failed (host reachable? path right? a directory needs the file path)"
  src=$FROM
else
  die "$FROM: no such file or directory (for another machine use HOST:PATH)"
fi
is_vault "$tmp" || die "$src is not an encrypted Ansible vault (first line must start with \$ANSIBLE_VAULT)"
ok "fetched an encrypted vault from $src"

# The helper is identical on every machine; a different non-stub one is someone's edit, so leave it to them.
wrote=true
scripts/write-vault-pass.sh
if ! $stub && ! cmp -s "$PASS_SH" "$prev"; then
  die "$PASS_SH differs from the standard YubiKey helper: review it, or move it aside and re-run (nothing was changed)"
fi
ok "$PASS_SH is the standard YubiKey helper (no secret in it)"

printf 'Opening the imported vault to prove a key works (insert one YubiKey and touch it when it blinks)...\n'
"$DISPATCH" ansible-vault decrypt --output - "$tmp" >/dev/null ||
  die "the YubiKey did not open the vault: insert a key programmed with the same secret as the source machine's, then re-run (nothing was changed)"
ok "a YubiKey opened the imported vault"

# skip-worktree first, so the encrypted vault is never a change git would stage (SECURITY.md)
git update-index --skip-worktree -- "$VAULT" || die "git update-index failed: is $VAULT tracked in this checkout?"
mv -f -- "$tmp" "$VAULT"
chmod 600 "$VAULT"
committed=true
ok "$VAULT installed (0600, skip-worktree)"
scripts/vault-init.sh  # already encrypted: confirms skip-worktree
printf 'Next: make apply CHUNK=ssh_client and make apply CHUNK=dotfiles (SSH and signing keys from the vault)\n'
