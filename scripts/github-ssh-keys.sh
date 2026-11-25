#!/usr/bin/env bash
# github-ssh-keys.sh (make github-ssh-keys) — add every security-key SSH public key (~/.ssh/id_ed25519_sk.pub and each
# id_ed25519_sk_backup*.pub, made by make ssh-keys) to GitHub as an authentication key, and the commit-signing key
# (~/.ssh/id_ed25519_sk_signing.pub, from make vault-signing-key or make dotfiles) as a signing key. GitHub lists a
# user's keys publicly, so keys it already has are skipped without any token scope and a re-run is safe. Only a key
# that needs adding asks gh for its scope (admin:public_key, admin:ssh_signing_key), in a terminal. A signing-key scope
# added here is dropped again on exit: a token that can add signing keys can make any commit show as Verified.
set -euo pipefail

die()  { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }
note() { printf '\033[0;33mNOTE:  %s\033[0m\n' "$*"; }

host=github.com
command -v gh >/dev/null || die "gh not found: run make packages"
gh auth status --hostname "$host" >/dev/null 2>&1 || die "gh is not logged in: gh auth login"
pubs=()
for pub in "$HOME/.ssh/id_ed25519_sk.pub" "$HOME"/.ssh/id_ed25519_sk_backup*.pub; do [[ -f $pub ]] && pubs+=("$pub"); done
[[ ${#pubs[@]} -gt 0 ]] || die "no ~/.ssh/id_ed25519_sk*.pub: run make ssh-keys first"

login=$(gh api --hostname "$host" user --jq .login) || die "cannot read the GitHub user from gh"
list_keys() { gh api --hostname "$host" --paginate "users/$login/$1" --jq '.[].key'; }
# known BLOB LIST: the key blob is in the list GitHub returned
known() { printf '%s\n' "$2" | awk -v b="$1" '$2 == b { found = 1 } END { exit !found }'; }
blob() { local _ b; read -r _ b _ < "$1" || true; printf '%s' "$b"; }

auth_missing=()
registered=$(list_keys keys) || die "cannot list $login's GitHub SSH keys"
for pub in "${pubs[@]}"; do
  if known "$(blob "$pub")" "$registered"; then ok "$pub is already on GitHub (authentication)"; else auth_missing+=("$pub"); fi
done
signing="$HOME/.ssh/id_ed25519_sk_signing.pub"
signing_missing=""
if [[ ! -f $signing ]]; then
  note "no $signing, signing key skipped (make dotfiles or make vault-signing-key)"
else
  signing_registered=$(list_keys ssh_signing_keys) || die "cannot list $login's GitHub SSH signing keys"
  if known "$(blob "$signing")" "$signing_registered"; then ok "$signing is already on GitHub (signing)"; else signing_missing=$signing; fi
fi

# The token's scopes (write: or admin: both allow adding keys).
scopes=$(gh api --hostname "$host" -i user 2>/dev/null | tr -d '\r' |
  awk 'tolower($0) ~ /^x-oauth-scopes:/ { sub(/^[^:]*: */, ""); print }') || die "cannot read the gh token's scopes"
has_scope() { [[ ", $scopes, " == *", admin:$1, "* || ", $scopes, " == *", write:$1, "* ]]; }
# A token that can add signing keys can make any commit show as Verified: one this run did not grant is pointed out on
# every exit path (dropping it needs the browser), with the scope names it actually holds.
held_signing=()
for s in admin write; do [[ ", $scopes, " == *", $s:ssh_signing_key, "* ]] && held_signing+=("$s:ssh_signing_key"); done
if [[ ${#held_signing[@]} -gt 0 ]]; then
  note "the gh token can add signing keys; drop that: gh auth refresh --hostname $host --remove-scopes $(IFS=,; printf '%s' "${held_signing[*]}")"
fi
[[ ${#auth_missing[@]} -gt 0 || -n $signing_missing ]] || exit 0

# Ask only for the scopes the missing keys need and the token lacks.
need=()
[[ ${#auth_missing[@]} -gt 0 ]] && ! has_scope public_key && need+=(admin:public_key)
[[ -n $signing_missing ]] && ! has_scope ssh_signing_key && need+=(admin:ssh_signing_key)
# Drop a signing-key scope this run added on every exit, a failed key add included; failing to drop it fails the run.
drop_signing_scope() {
  local rc=$?
  # Ctrl-C during the drop's own browser step stops gh (a handler, unlike an ignored INT, is reset in children) and
  # still reaches the ERROR line below
  trap 'rc=130' INT
  printf 'Dropping admin:ssh_signing_key from the gh token again: follow the prompts.\n'
  gh auth refresh --hostname "$host" --remove-scopes admin:ssh_signing_key && exit "$rc"
  printf '\033[0;31mERROR: %s\033[0m\n' "could not drop admin:ssh_signing_key: gh auth refresh --hostname $host --remove-scopes admin:ssh_signing_key" >&2
  exit 1
}
unscoped=""
if [[ ${#need[@]} -gt 0 && ! -t 0 ]]; then  # no terminal for the browser step: add what the token allows, then fail
  unscoped="the gh token lacks ${need[*]} to add the remaining keys: run make github-ssh-keys in a terminal"
  [[ " ${need[*]} " != *" admin:public_key "* ]] || auth_missing=()
  [[ " ${need[*]} " != *" admin:ssh_signing_key "* ]] || signing_missing=""
elif [[ ${#need[@]} -gt 0 ]]; then
  printf 'gh needs %s to add the missing keys: follow the prompts.\n' "${need[*]}"
  gh auth refresh --hostname "$host" --scopes "$(IFS=,; printf '%s' "${need[*]}")"
  if [[ " ${need[*]} " == *" admin:ssh_signing_key "* ]]; then trap drop_signing_scope EXIT; fi
fi

add_key() {  # add_key PUB TYPE
  local comment
  read -r _ _ comment _ < "$1" || true
  gh ssh-key add "$1" --title "${comment:-$(basename "$1" .pub)}" --type "$2" >/dev/null || die "gh ssh-key add $1 ($2) failed"
  ok "added $1 to GitHub ($2)"
}
for pub in ${auth_missing[@]+"${auth_missing[@]}"}; do add_key "$pub" authentication; done
[[ -z $signing_missing ]] || add_key "$signing_missing" signing
[[ -z $unscoped ]] || die "$unscoped"
