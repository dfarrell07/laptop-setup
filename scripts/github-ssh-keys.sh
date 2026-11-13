#!/usr/bin/env bash
# github-ssh-keys.sh (make github-ssh-keys) — add every security-key SSH public key (~/.ssh/id_ed25519_sk.pub and each
# id_ed25519_sk_backup*.pub, made by make ssh-keys) to GitHub as an authentication key. Keys GitHub already has are
# skipped, so a re-run is safe. When the gh token cannot list SSH keys, asks gh for the admin:public_key scope first.
set -euo pipefail

die()  { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }

host=github.com
command -v gh >/dev/null || die "gh not found: run make packages"
gh auth status --hostname "$host" >/dev/null 2>&1 || die "gh is not logged in: gh auth login"
pubs=()
for pub in "$HOME/.ssh/id_ed25519_sk.pub" "$HOME"/.ssh/id_ed25519_sk_backup*.pub; do [[ -f $pub ]] && pubs+=("$pub"); done
[[ ${#pubs[@]} -gt 0 ]] || die "no ~/.ssh/id_ed25519_sk*.pub: run make ssh-keys first"

list_keys() { gh api --hostname "$host" --paginate user/keys --jq '.[].key' 2>/dev/null; }
if ! registered=$(list_keys); then
  [[ -t 0 ]] || die "the gh token lacks the admin:public_key scope: run make github-ssh-keys in a terminal"
  printf 'gh needs the admin:public_key scope to manage SSH keys: follow the prompts.\n'
  gh auth refresh --hostname "$host" --scopes admin:public_key
  registered=$(list_keys) || die "still cannot list GitHub SSH keys after gh auth refresh"
fi

for pub in "${pubs[@]}"; do
  read -r _ blob comment _ < "$pub" || true
  if printf '%s\n' "$registered" | awk -v b="$blob" '$2 == b { found = 1 } END { exit !found }'; then
    ok "$pub is already on GitHub"
  else
    gh ssh-key add "$pub" --title "${comment:-$(basename "$pub" .pub)}" >/dev/null || die "gh ssh-key add $pub failed"
    ok "added $pub to GitHub"
  fi
done
