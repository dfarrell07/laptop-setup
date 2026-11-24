#!/usr/bin/env bash
# github-ssh-keys.sh (make github-ssh-keys) — add every security-key SSH public key (~/.ssh/id_ed25519_sk.pub and each
# id_ed25519_sk_backup*.pub, made by make ssh-keys) to GitHub as an authentication key, and the commit-signing key
# (~/.ssh/id_ed25519_sk_signing.pub, from make vault-signing-key or make dotfiles) as a signing key. Keys GitHub already
# has are skipped, so a re-run is safe. When the gh token cannot list them, asks gh for the admin:public_key and
# admin:ssh_signing_key scopes first.
set -euo pipefail

die()  { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }

host=github.com
command -v gh >/dev/null || die "gh not found: run make packages"
gh auth status --hostname "$host" >/dev/null 2>&1 || die "gh is not logged in: gh auth login"
pubs=()
for pub in "$HOME/.ssh/id_ed25519_sk.pub" "$HOME"/.ssh/id_ed25519_sk_backup*.pub; do [[ -f $pub ]] && pubs+=("$pub"); done
[[ ${#pubs[@]} -gt 0 ]] || die "no ~/.ssh/id_ed25519_sk*.pub: run make ssh-keys first"

signing="$HOME/.ssh/id_ed25519_sk_signing.pub"
[[ -f $signing ]] || signing=""

list_keys() { gh api --hostname "$host" --paginate "user/$1" --jq '.[].key' 2>/dev/null; }
need_scopes=false
registered=$(list_keys keys) || need_scopes=true
signing_registered=""
if [[ -n $signing ]]; then signing_registered=$(list_keys ssh_signing_keys) || need_scopes=true; fi
if $need_scopes; then
  [[ -t 0 ]] || die "the gh token lacks the admin:public_key or admin:ssh_signing_key scope: run make github-ssh-keys in a terminal"
  printf 'gh needs the admin:public_key and admin:ssh_signing_key scopes to manage SSH keys: follow the prompts.\n'
  gh auth refresh --hostname "$host" --scopes admin:public_key,admin:ssh_signing_key
  registered=$(list_keys keys) || die "still cannot list GitHub SSH keys after gh auth refresh"
  if [[ -n $signing ]]; then
    signing_registered=$(list_keys ssh_signing_keys) || die "still cannot list GitHub SSH signing keys after gh auth refresh"
  fi
fi

# add_key PUB TYPE REGISTERED: add PUB as a TYPE (authentication or signing) key unless its blob is in REGISTERED.
add_key() {
  local pub=$1 type=$2 known=$3 blob comment
  read -r _ blob comment _ < "$pub" || true
  if printf '%s\n' "$known" | awk -v b="$blob" '$2 == b { found = 1 } END { exit !found }'; then
    ok "$pub is already on GitHub ($type)"
  else
    gh ssh-key add "$pub" --title "${comment:-$(basename "$pub" .pub)}" --type "$type" >/dev/null \
      || die "gh ssh-key add $pub ($type) failed"
    ok "added $pub to GitHub ($type)"
  fi
}
for pub in "${pubs[@]}"; do add_key "$pub" authentication "$registered"; done
if [[ -n $signing ]]; then add_key "$signing" signing "$signing_registered"; fi
