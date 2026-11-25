#!/bin/bash
# make pull: fast-forward this checkout to its upstream, keeping a local (encrypted) group_vars/all/vault.yml.
# vault-init and vault-import mark the vault skip-worktree, so a plain `git pull` refuses whenever upstream changes the
# committed stub ("Your local changes ... would be overwritten"). This sets the vault aside, pulls, puts it back
# byte for byte and marks it skip-worktree again; any failure restores it before exiting.
set -euo pipefail
set +x  # never trace: the vault copy is ciphertext, but keep vault handling out of traces anyway

die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)" || die "not in a git checkout"
VAULT=group_vars/all/vault.yml
[[ -n $(git status --porcelain --untracked-files=no -- . ":(exclude)$VAULT") ]] &&
  die "local changes besides $VAULT: commit or stash them first (git status)"
if [[ $(git ls-files -v -- "$VAULT" | cut -c1) != S ]]; then  # no local vault set aside: an ordinary pull
  git pull --ff-only
  exit 0
fi

backup=$(git rev-parse --git-dir)/vault.yml.pull-backup
# a copy left by an interrupted run may be the only one: never overwrite it
[[ ! -e $backup ]] || die "$backup exists from an earlier make pull: compare it with $VAULT, cp -p the right one into place, delete $backup"
umask 077
cp -p -- "$VAULT" "$backup"
restore() {
  if cp -p -- "$backup" "$VAULT" && git update-index --skip-worktree -- "$VAULT"; then
    rm -f -- "$backup"
  else
    die "could not restore $VAULT: your copy is in $backup (cp -p it back, then git update-index --skip-worktree $VAULT)"
  fi
}
trap restore EXIT
old=$(git rev-parse HEAD)
git update-index --no-skip-worktree -- "$VAULT"
git checkout -q -- "$VAULT"
git pull --ff-only
if ! git diff --quiet "$old" HEAD -- "$VAULT"; then
  printf 'NOTE: the vault stub changed upstream (new keys?): git diff %s HEAD -- %s, then make vault-edit\n' "${old:0:8}" "$VAULT"
fi
