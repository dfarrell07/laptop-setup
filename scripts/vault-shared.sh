#!/usr/bin/env bash
# vault-shared.sh: one Ansible vault shared by every machine (docs/shared-vault.md), kept in the owner's private
# laptop-secrets repository:
#
#   vault.yml       the Ansible vault, encrypted with a random password
#   vault-pass.age  that password, encrypted with age to every enrolled YubiKey (age-plugin-yubikey, PIV)
#   recipients      the enrolled keys' public age recipients
#
# group_vars/all/vault.yml stays the working copy every play and check reads; this script keeps it in step with the
# shared one. scripts/vault-pass.sh becomes `age -d` of vault-pass.age (a YubiKey PIN and touch). Any enrolled key
# opens the vault on any machine; dropping a key is a recipients change plus a new password, nothing to reprogram.
#
#   init                 first machine: move this machine's vault into the shared repository (once)
#   join                 another machine: use the shared vault (its own vault is kept aside)
#   add-key [--no-pin]   enrol the plugged-in YubiKey (--no-pin: a spare opened by touch alone); RECIPIENT=age1...
#                        adds a recipient enrolled elsewhere instead
#   remove-key RECIPIENT drop a recipient and re-encrypt a NEW password (the old one stays in git history)
#   pull                 fetch the shared vault into group_vars/all/vault.yml
#   edit | view          edit (then commit and push) or view the shared vault
#   status               what is set up here (no secrets)
#
# Environment: SECRETS_DIR (default ~/src/<dotfiles_github_user>/laptop-secrets), SECRETS_REMOTE (default its GitHub
# HTTPS URL), VAULT_AGE_IDENTITIES (default ~/.config/laptop-setup/age-identities: YubiKey identity stubs, not
# secrets), FORCE=1 (approve a changed recipients list on this machine).
set -euo pipefail
# SECURITY: this script decrypts the vault password; never let xtrace record it.
{ set +x; } 2>/dev/null
cd "$(dirname "$0")/.."

VAULT=group_vars/all/vault.yml
PASS_SH=scripts/vault-pass.sh
DISPATCH=scripts/verify-ansible-args.sh
MARK='# laptop-setup shared vault helper (scripts/vault-shared.sh)'
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/laptop-setup
IDS=${VAULT_AGE_IDENTITIES:-$CONF/age-identities}
APPROVED=$CONF/age-recipients-approved
if [[ -n ${NO_COLOR:-} ]]; then R='' G='' Y='' N=''; else R='\033[0;31m' G='\033[0;32m' Y='\033[0;33m' N='\033[0m'; fi
die()  { printf "${R}ERROR: %s${N}\n" "$*" >&2; exit 1; }
ok()   { printf "${G}OK:    %s${N}\n" "$*"; }
warn() { printf "${Y}WARN:  %s${N}\n" "$*" >&2; }
is_vault() { head -c 14 "$1" 2>/dev/null | grep -qF "\$ANSIBLE_VAULT"; }
need() { command -v "$1" >/dev/null || die "$1 is not installed: sudo dnf install $1 (Fedora; age-plugin-yubikey: make packages) or brew install $2 (macOS)"; }

github_user() {
  awk -F: '/^dotfiles_github_user:/ { v=$2; sub(/#.*/, "", v); sub(/!unsafe/, "", v); gsub(/[ \047"]/, "", v); print v; exit }' \
    config.yml 2>/dev/null
}
user=$(github_user || true)
[[ $user =~ ^[A-Za-z0-9-]+$ && $user != CHANGE_ME ]] || user=''
SECRETS_DIR=${SECRETS_DIR:-${user:+$HOME/src/$user/laptop-secrets}}
SECRETS_REMOTE=${SECRETS_REMOTE:-${user:+https://github.com/$user/laptop-secrets.git}}
SIGNERS=${VAULT_ALLOWED_SIGNERS:-$CONF/vault-allowed-signers}
S=$SECRETS_DIR
need_dir() {
  [[ -n $S ]] || die "set dotfiles_github_user in config.yml (or SECRETS_DIR)"
  [[ $S != *"'"* && $IDS != *"'"* ]] || die "SECRETS_DIR and the identities path must not contain a quote"
}

# The recipients a file lists (comments and blank lines dropped), sorted, once each.
recipients_of() { [[ -f $1 ]] && grep -Ev '^[[:space:]]*(#|$)' "$1" | tr -d ' \t' | sort -u || true; }
shared_helper() { [[ -f $PASS_SH ]] && grep -qxF "$MARK" "$PASS_SH"; }
active() { [[ -n $S ]] && shared_helper && [[ -f $S/vault-pass.age ]]; }

write_helper() {
  local tmp
  tmp=$(mktemp "$PASS_SH.XXXXXX")
  cat >"$tmp" <<EOF
#!/bin/sh
$MARK
# Prints the vault password: age opens $S/vault-pass.age with a plugged-in enrolled YubiKey (PIN, touch).
{ set +x; } 2>/dev/null
exec age -d -i '$IDS' '$S/vault-pass.age'
EOF
  chmod 700 "$tmp"
  mv -f "$tmp" "$PASS_SH"
  (cd scripts && sha256sum vault-pass.sh >vault-pass.sh.sha256)
}

# Refuse to encrypt the password to a recipients list this machine has not approved: anyone who can push to the
# secrets repository could add their own key (Health's design). FORCE=1 approves the current list here.
check_approved() {  # $1: a recipient being removed, which need not be approved here
  local now extra
  now=$(recipients_of "$S/recipients" | grep -vxF "${1:-age1-none}" || true)
  [[ -n $now ]] || die "no recipients in $S/recipients: make vault-add-key"
  extra=$(comm -13 <(recipients_of "$APPROVED") <(printf '%s\n' "$now"))
  if [[ -n $extra && ${FORCE:-} != 1 ]]; then
    printf '%s\n' "$extra" | sed 's/^/  not approved here: /' >&2
    die "the recipients list has keys this machine never approved: check them (git -C $S log -p recipients), then FORCE=1"
  fi
  approve
}
approve() { mkdir -p "$CONF" && recipients_of "$S/recipients" >"$APPROVED" && chmod 600 "$APPROVED"; }

# Encrypt a password from stdin to a recipients file ($1, default the repository's), replacing vault-pass.age only
# once age has succeeded.
seal_password() {
  local tmp list=${1:-$S/recipients}
  tmp=$(mktemp "$S/.vault-pass.age.XXXXXX")
  if ! age -R "$list" -o "$tmp"; then rm -f "$tmp"; die "age could not encrypt to $list"; fi
  mv -f "$tmp" "$S/vault-pass.age"
}

# Bring the clone up to date before using or changing anything. The upstream commit is resolved once to a SHA; its
# whole history must be signed (verified SHAs are cached) and descend from the last commit this machine accepted (no
# rollback to an older signed state, which could bring back a removed key); its tree is checked; and then exactly that
# SHA is fast-forwarded to. An empty new repository has no upstream yet.
LAST_SEEN=$CONF/vault-last-seen
VERIFIED=$CONF/vault-verified-commits
sync_clone() {
  local target
  git -C "$S" fetch -q 2>/dev/null || { warn "could not fetch $S (offline?): using the local clone"; verify_all HEAD; return 0; }
  target=$(git -C "$S" rev-parse -q --verify '@{u}^{commit}' 2>/dev/null) || { verify_all HEAD; return 0; }
  no_rollback "$target"
  [[ -z $(git -C "$S" rev-list "$target..HEAD") ]] ||
    die "$S has local commits not on GitHub: push them (git -C $S push) or drop them (git -C $S reset --hard $target)"
  verify_all "$target"
  check_tree "$target"
  git -C "$S" merge -q --ff-only "$target" || die "git merge in $S failed: resolve it there (git -C $S status), then re-run"
  seen "$target"
}
seen() { mkdir -p "$CONF" && printf '%s\n' "$1" >"$LAST_SEEN" && chmod 600 "$LAST_SEEN"; }
no_rollback() {
  local last
  last=$(cat "$LAST_SEEN" 2>/dev/null || true)
  [[ -z $last ]] && return 0
  git -C "$S" merge-base --is-ancestor "$last" "$1" 2>/dev/null ||
    die "GitHub's laptop-secrets no longer contains $last, the last commit this machine accepted: its history was rewritten (a rollback could bring back a removed key). Find out why before using it; to accept it anyway: rm $LAST_SEEN"
}
new_password() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }

# Trust comes from commit signatures, not from age: age does not say who encrypted a file, so anyone who can push
# could seal a password of their own choosing to your keys. Every commit must carry a signature by one of the owner's
# SSH signing keys: those in ~/.config/git/allowed_signers, else the ones GitHub lists for the owner (fetched once).
ensure_signers() {
  [[ -s $SIGNERS ]] && return 0
  mkdir -p "$CONF" && chmod 700 "$CONF"
  local keys=''
  if [[ -s $HOME/.config/git/allowed_signers ]]; then
    keys=$(grep -Ev '^[[:space:]]*(#|$)' "$HOME/.config/git/allowed_signers" | grep -oE '(sk-)?ssh-[a-z0-9@.-]+ [A-Za-z0-9+/=]+' || true)
  fi
  if [[ -z $keys && -n $user ]] && command -v gh >/dev/null; then
    keys=$(gh api "users/$user/ssh_signing_keys" --jq '.[].key' 2>/dev/null | grep -oE '(sk-)?ssh-[a-z0-9@.-]+ [A-Za-z0-9+/=]+' || true)
  fi
  [[ -n $keys ]] || die "no SSH signing key to check the shared vault's commits: make dotfiles (allowed_signers), or add your signing key to GitHub (gh ssh-key add --type signing)"
  if [[ ! -s $HOME/.config/git/allowed_signers ]]; then
    # Keys GitHub lists are only as trustworthy as the GitHub account that can also push: confirm them by eye.
    printf 'Commit signing keys GitHub lists for %s:\n' "$user"
    printf '%s\n' "$keys" | while IFS= read -r k; do printf '%s\n' "$k" | ssh-keygen -lf - 2>/dev/null | sed 's/^/  /'; done
    [[ -t 0 ]] || die "confirm these in a terminal (or make dotfiles first, which writes ~/.config/git/allowed_signers)"
    local answer
    read -r -p "Are these your signing keys (compare with ssh-keygen -lf on your key's .pub)? [y/N] " answer
    [[ $answer == [yY] ]] || die "not confirmed: nothing was trusted"
  fi
  printf '%s\n' "$keys" | sed 's/^/* namespaces="git" /' >"$SIGNERS"
  chmod 600 "$SIGNERS"
  ok "commit signers for the shared vault: $SIGNERS ($(wc -l <"$SIGNERS") key(s); check them against yours)"
}
verify_all() {  # every commit reachable from $1 must be signed by an allowed signer
  local c
  git -C "$S" rev-parse -q --verify "$1^{commit}" >/dev/null 2>&1 || return 0  # nothing committed yet
  ensure_signers
  mkdir -p "$CONF" && chmod 700 "$CONF" && touch "$VERIFIED" && chmod 600 "$VERIFIED"
  for c in $(git -C "$S" rev-list "$1"); do
    grep -qxF "$c" "$VERIFIED" && continue
    git -C "$S" -c gpg.ssh.allowedSignersFile="$SIGNERS" verify-commit "$c" >/dev/null 2>&1 ||
      die "commit $c in $S is not signed by your signing key: nothing was merged or used. Inspect it (git -C $S show --show-signature $c)"
    printf '%s\n' "$c" >>"$VERIFIED"
  done
}
# No symlinks or odd files in the repository: the script writes next to them.
check_tree() {
  local bad
  bad=$(git -C "$S" ls-tree -r "$1" | awk '$1 != "100644" && $1 != "100755" {print $4}')
  [[ -z $bad ]] || die "$S holds non-regular files ($bad): refusing to use it"
  for f in vault.yml vault-pass.age recipients; do
    [[ ! -L $S/$f ]] || die "$S/$f is a symlink: refusing to use it"
  done
}

# A change to the shared repository is all or nothing until it is pushed: txn_begin saves vault.yml, vault-pass.age,
# recipients and HEAD; any failure or interrupt before txn_end restores them (and drops an unpushed commit).
txn_begin() {
  mkdir -p "$CONF" && chmod 700 "$CONF"
  TXN_DIR=$(mktemp -d "$CONF/txn.XXXXXX")
  TXN_HEAD=$(git -C "$S" rev-parse -q --verify HEAD 2>/dev/null || true)
  local f
  for f in vault.yml vault-pass.age recipients; do [[ ! -f $S/$f ]] || cp -p "$S/$f" "$TXN_DIR/$f"; done
  trap txn_rollback EXIT
  trap 'exit 130' INT TERM
}
txn_end() { trap - EXIT INT TERM; rm -rf "$TXN_DIR"; }
txn_rollback() {
  local rc=$? f
  for f in vault.yml vault-pass.age recipients; do
    if [[ -f $TXN_DIR/$f ]]; then cp -p "$TXN_DIR/$f" "$S/$f"; else rm -f "$S/$f"; fi
  done
  if [[ -n $TXN_HEAD ]]; then git -C "$S" reset -q "$TXN_HEAD" 2>/dev/null || true
  elif git -C "$S" rev-parse -q --verify HEAD >/dev/null 2>&1; then git -C "$S" update-ref -d HEAD 2>/dev/null || true; fi
  git -C "$S" reset -q 2>/dev/null || true
  rm -rf "$TXN_DIR"
  [[ -z ${TXN_UNDO:-} ]] || "$TXN_UNDO"
  warn "stopped before the change was pushed: the shared vault is as it was; run the command again"
  exit "$rc"
}

commit_push() {
  local f
  for f in vault.yml vault-pass.age recipients .gitattributes README.md; do
    [[ ! -e $S/$f ]] || git -C "$S" add -- "$f"
  done
  if git -C "$S" diff --cached --quiet; then return 0; fi
  # Signed, always: the other machines refuse an unsigned commit (a touch of the signing YubiKey).
  git -C "$S" commit -q -s -S -m "$1" ||
    die "signed commit in $S failed (commit signing needs your SSH signing key: make dotfiles)"
  git -C "$S" push -q || die "push from $S failed (offline, or another machine pushed first): run the command again"
  seen "$(git -C "$S" rev-parse HEAD)"
  ok "committed and pushed: $1"
}

install_local() {
  # skip-worktree first, so the encrypted vault is never a change git would stage (SECURITY.md)
  git update-index --skip-worktree -- "$VAULT" 2>/dev/null || true
  if ! cmp -s "$S/vault.yml" "$VAULT"; then
    cp -f -- "$S/vault.yml" "$VAULT.shared.tmp" && chmod 600 "$VAULT.shared.tmp" && mv -f -- "$VAULT.shared.tmp" "$VAULT"
    ok "$VAULT updated from the shared vault"
  fi
  chmod 600 "$VAULT"
}

prove_opens() {
  printf 'Opening the shared vault (plug in an enrolled YubiKey: PIN, then touch)...\n'
  "$DISPATCH" ansible-vault view "$1" >/dev/null ||
    die "the vault did not open: is an enrolled YubiKey plugged in (make vault-status)?"
}

identity_stubs() {
  need age-plugin-yubikey age-plugin-yubikey
  mkdir -p "$CONF" && chmod 700 "$CONF"
  local stubs line added=0
  stubs=$(age-plugin-yubikey --identity 2>/dev/null | grep '^AGE-PLUGIN-YUBIKEY-' || true)
  [[ -n $stubs ]] || return 1
  touch "$IDS" && chmod 600 "$IDS"
  while IFS= read -r line; do
    grep -qxF "$line" "$IDS" || { printf '%s\n' "$line" >>"$IDS"; added=$((added + 1)); }
  done <<<"$stubs"
  ok "identity stubs for the plugged-in YubiKey(s) in $IDS ($added new)"
}

cmd_add_key() {
  need age age
  [[ -d $S/.git ]] || die "$S is not a clone: gh repo create $user/laptop-secrets --private --clone (in ~/src/$user), or make vault-join"
  sync_clone
  local rec label nopin=false
  [[ ${1:-} == --no-pin ]] && nopin=true
  if [[ -n ${RECIPIENT:-} ]]; then
    [[ $RECIPIENT =~ ^age1[a-z0-9]+$ ]] || die "RECIPIENT must be an age recipient (age1...)"
    rec=$RECIPIENT label="added by hand"
  else
    need age-plugin-yubikey age-plugin-yubikey
    [[ -t 0 ]] || die "run it in your own terminal (the YubiKey asks for its PIN and a touch)"
    rec=$(age-plugin-yubikey --list 2>/dev/null | grep '^age1yubikey' | head -n 1 || true)
    if [[ -z $rec ]]; then
      # Firmware 5.7+ ships an AES management key the plugin cannot use: switch it to a PIN-protected TDES one first.
      if command -v ykman >/dev/null && ykman piv info 2>/dev/null | grep -q 'default Management key' &&
        ykman piv info 2>/dev/null | grep -Eq 'Management key algorithm:[[:space:]]*AES'; then
        printf 'This YubiKey has an AES factory management key, which age-plugin-yubikey cannot use; ykman makes a\n'
        printf 'PIN-protected TDES one (enter the PIV PIN; a new key has 123456).\n'
        ykman piv access change-management-key -a TDES --protect \
          -m 010203040506070801020304050607080102030405060708 || die "ykman could not change the management key"
      fi
      if $nopin; then
        age-plugin-yubikey --generate --pin-policy never --touch-policy always >/dev/null || die "age-plugin-yubikey --generate failed"
      else
        age-plugin-yubikey --generate >/dev/null || die "age-plugin-yubikey --generate failed (a new key's PIN is 123456)"
      fi
      rec=$(age-plugin-yubikey --list 2>/dev/null | grep '^age1yubikey' | head -n 1 || true)
      [[ -n $rec ]] || die "no age slot on the YubiKey after --generate"
    elif $nopin; then
      warn "this YubiKey already has an age slot; its own PIN setting stays"
    fi
    identity_stubs || die "age-plugin-yubikey listed no identity for the plugged-in key"
    label="YubiKey $(ykman list --serials 2>/dev/null | head -n 1 || echo '?'), added $(date +%F) on $(hostname -s)"
  fi
  if recipients_of "$S/recipients" | grep -qxF "$rec"; then
    ok "already a recipient: $rec"
    approve_if_new_file
    return 0
  fi
  # The existing list must be approved here before the password is re-encrypted to it plus the new key.
  [[ -z $(recipients_of "$S/recipients") ]] || check_approved
  txn_begin
  local next=$TXN_DIR/recipients.next
  { [[ ! -f $S/recipients ]] || cat "$S/recipients"; printf '# %s\n%s\n' "$label" "$rec"; } >"$next"
  if [[ -f $S/vault-pass.age ]]; then
    printf 'Re-encrypting the vault password to the new list (an enrolled key opens it: PIN, then touch)...\n'
    local pass
    pass=$(age -d -i "$IDS" "$S/vault-pass.age") || die "could not open vault-pass.age with an enrolled key"
    printf '%s' "$pass" | seal_password "$next"
    unset pass
  fi
  cp -f "$next" "$S/recipients"
  [[ ! -f $S/vault-pass.age ]] || commit_push "Recipients: add a key"
  txn_end
  approve
  ok "recipient added: $rec"
}
approve_if_new_file() { [[ -f $APPROVED ]] || approve; }

cmd_remove_key() {
  local rec=${1:-${RECIPIENT:-}}
  [[ $rec =~ ^age1[a-z0-9]+$ ]] || die "usage: make vault-remove-key RECIPIENT=age1..."
  recipients_of "$S/recipients" | grep -qxF "$rec" || die "$rec is not a recipient"
  [[ $(recipients_of "$S/recipients" | wc -l) -gt 1 ]] || die "refusing to remove the last recipient"
  active || die "the shared vault is not active here: make vault-join"
  sync_clone
  check_approved "$rec"
  # Drop the recipient (and its comment line) from a copy, then rotate to the copy: the old password stays readable in
  # git history, so the vault gets a new one.
  txn_begin
  local next=$TXN_DIR/recipients.next
  awk -v r="$rec" 'BEGIN{c=""} /^[[:space:]]*#/ {c=$0; next} { gsub(/[ \t]/,""); if ($0 != r) { if (c!="") print c; print } c="" }' \
    "$S/recipients" >"$next"
  rotate "Recipients: remove a key, new vault password" "$next"
  txn_end
  approve
  install_local
}

# Inside a transaction: a new random password sealed to the recipients file $2, vault.yml rekeyed to it, both proved
# to open with a plugged-in key before anything is replaced, then committed and pushed. Any failure or interrupt up to
# the push restores the previous files (txn_rollback).
rotate() {
  local list=$2 helper=$TXN_DIR/helper newpass=$TXN_DIR/vault-pass.age.next
  need age age
  new_password | age -R "$list" -o "$newpass" || die "age could not encrypt the new password"
  printf '#!/bin/sh\n{ set +x; } 2>/dev/null\nexec age -d -i %q %q\n' "$IDS" "$newpass" >"$helper"
  chmod 700 "$helper"
  "$DISPATCH" ansible-vault rekey --new-vault-password-file "$helper" "$S/vault.yml" ||
    die "ansible-vault rekey failed (the key you remove cannot be the only one plugged in)"
  printf 'Checking the new password opens with a key you keep (PIN, then touch)...\n'
  age -d -i "$IDS" "$newpass" >/dev/null || die "no plugged-in key opens the new password"
  cp -f "$newpass" "$S/vault-pass.age"
  cp -f "$list" "$S/recipients"
  commit_push "$1"
}

write_repo_files() {
  [[ -f $S/.gitattributes ]] || printf '*.age -text -diff\nvault.yml -diff\n' >"$S/.gitattributes"
  [[ -f $S/README.md ]] || cat >"$S/README.md" <<'EOF'
# laptop-secrets

The shared Ansible vault for laptop-setup (its docs/shared-vault.md). `vault.yml` is encrypted with a random password;
`vault-pass.age` is that password encrypted with age to every YubiKey in `recipients`. Change it only through
laptop-setup's make targets (`make vault-edit`, `make vault-add-key`, `make vault-remove-key`).
EOF
}

cmd_init() {
  need age age
  [[ -d $S/.git ]] || die "$S is not a clone: create the private repository first (gh repo create $user/laptop-secrets --private --clone, run in ~/src/$user)"
  [[ ! -f $S/vault-pass.age ]] || die "$S already holds a shared vault: use make vault-join"
  is_vault "$VAULT" || die "$VAULT is not encrypted here: make vault-init first (or vault-join on a machine with none)"
  ! shared_helper || die "$PASS_SH is already the shared helper"
  [[ -n $(recipients_of "$S/recipients") ]] || cmd_add_key
  [[ -n $(recipients_of "$S/recipients") ]] || die "no recipients: make vault-add-key"
  printf 'Recipients:\n'; recipients_of "$S/recipients" | sed 's/^/  /'
  check_approved
  local old_helper=$PASS_SH.pre-shared
  [[ ! -e $old_helper ]] || old_helper=$PASS_SH.pre-shared.$(date +%Y%m%d%H%M%S)
  cp -p "$PASS_SH" "$old_helper"
  cp -p scripts/vault-pass.sh.sha256 "$old_helper.sha256" 2>/dev/null || true
  # Until the push, any exit (an error, Ctrl-C at a PIN or touch prompt) restores the shared clone and HEAD and puts
  # the old helper back, so init can simply be run again.
  INIT_OLD=$old_helper TXN_UNDO=init_undo
  txn_begin
  new_password | seal_password
  cp -f -- "$VAULT" "$S/vault.yml" && chmod 600 "$S/vault.yml"
  local helper=$TXN_DIR/helper
  printf '#!/bin/sh\n{ set +x; } 2>/dev/null\nexec age -d -i %q %q\n' "$IDS" "$S/vault-pass.age" >"$helper"
  chmod 700 "$helper"
  printf 'Re-encrypting the vault: touch the YubiKey for the current password, then PIN and touch for the new one...\n'
  "$DISPATCH" ansible-vault rekey --new-vault-password-file "$helper" "$S/vault.yml" || die "ansible-vault rekey failed"
  write_helper
  prove_opens "$S/vault.yml"
  [[ -e $VAULT.pre-shared ]] || { cp -p "$VAULT" "$VAULT.pre-shared" && chmod 600 "$VAULT.pre-shared"; }
  write_repo_files
  commit_push "Shared vault: initial"
  txn_end
  install_local
  ok "the shared vault is active. The old helper is $old_helper and the old vault $VAULT.pre-shared (keep them until"
  printf '       every machine has joined, then delete them). Other machines: make vault-join\n'
}

init_undo() {
  cp -p "$INIT_OLD" "$PASS_SH" && { [[ ! -f $INIT_OLD.sha256 ]] || cp -p "$INIT_OLD.sha256" scripts/vault-pass.sh.sha256; }
  rm -f "$INIT_OLD" "$INIT_OLD.sha256"
}

cmd_join() {
  need age age
  if [[ ! -d $S/.git ]]; then
    [[ -n $SECRETS_REMOTE ]] || die "set SECRETS_REMOTE"
    mkdir -p "$(dirname "$S")"
    git clone -q --no-checkout -- "$SECRETS_REMOTE" "$S" || die "git clone $SECRETS_REMOTE failed (gh auth login? the repo is private)"
    if git -C "$S" rev-parse -q --verify HEAD >/dev/null 2>&1; then
      verify_all HEAD
      check_tree HEAD
      git -C "$S" checkout -q || die "checkout in $S failed"
    fi
  fi
  sync_clone
  verify_all HEAD  # a clone made by hand: its own commits too
  check_tree HEAD
  [[ -f $S/vault-pass.age && -f $S/vault.yml ]] || die "$S has no shared vault yet: make vault-share-init on the first machine"
  if [[ ! -s $IDS ]]; then identity_stubs || die "no enrolled YubiKey plugged in (age-plugin-yubikey --identity listed none)"; fi
  printf 'Recipients in the shared vault:\n'; recipients_of "$S/recipients" | sed 's/^/  /'
  # The first join trusts the list it shows (check it against your keys); after that, a change needs FORCE=1.
  if [[ -f $APPROVED ]]; then check_approved; else approve; fi
  prove_opens_with_age
  if ! shared_helper; then
    # This machine's own vault and helper are kept once, never overwritten by a later join.
    if is_vault "$VAULT" && ! cmp -s "$S/vault.yml" "$VAULT" && [[ ! -e $VAULT.pre-shared ]]; then
      cp -p "$VAULT" "$VAULT.pre-shared" && chmod 600 "$VAULT.pre-shared"
      warn "this machine's own vault is kept as $VAULT.pre-shared: move any values only it has with make vault-edit"
    fi
    [[ ! -f $PASS_SH || -e $PASS_SH.pre-shared ]] || cp -p "$PASS_SH" "$PASS_SH.pre-shared"
  fi
  write_helper
  install_local
  ok "the shared vault is active here"
}
prove_opens_with_age() {
  printf 'Opening the vault password (an enrolled YubiKey: PIN, then touch)...\n'
  age -d -i "$IDS" "$S/vault-pass.age" >/dev/null || die "no enrolled YubiKey opened vault-pass.age: plug one in, or enrol this one on a joined machine (make vault-add-key)"
}

cmd_pull() {
  active || die "the shared vault is not active here: make vault-join (or vault-share-init on the first machine)"
  sync_clone
  # A recipients change made elsewhere only matters when this machine re-encrypts; say so now.
  [[ -z $(comm -13 <(recipients_of "$APPROVED") <(recipients_of "$S/recipients")) ]] ||
    warn "the recipients list changed since this machine approved it: review git -C $S log -p recipients"
  install_local
}

cmd_edit() {
  cmd_pull
  local before
  before=$(sha256sum "$S/vault.yml")
  EDITOR='' VISUAL='' ANSIBLE_EDITOR='' "$DISPATCH" ansible-vault edit "$S/vault.yml"
  [[ $(sha256sum "$S/vault.yml") != "$before" ]] || { ok "no change"; return 0; }
  # Keep the edited (still encrypted) vault aside: if the commit or push fails, the clone goes back to GitHub's state
  # and this copy holds the edit.
  EDIT_KEEP=$CONF/vault.yml.unpushed
  cp -p "$S/vault.yml" "$EDIT_KEEP" && chmod 600 "$EDIT_KEEP"
  local edited=$EDIT_KEEP
  cp -p "$S/vault.yml" "$edited.tmp" && git -C "$S" checkout -q -- vault.yml
  TXN_UNDO=edit_undo
  txn_begin
  mv -f "$edited.tmp" "$S/vault.yml"
  commit_push "Vault: edit"
  txn_end
  rm -f "$EDIT_KEEP"
  install_local
}
edit_undo() {
  warn "your edit is kept, encrypted, in $EDIT_KEEP: view it with .venv/bin/ansible-vault view --vault-password-file scripts/vault-pass.sh $EDIT_KEEP and redo it with make vault-edit"
}

cmd_view() {
  (cmd_pull >/dev/null) || warn "showing this machine's working copy (the shared vault could not be updated)"
  PAGER='' ANSIBLE_PAGER='' MANPAGER='' SYSTEMD_PAGER='' EDITOR='' VISUAL='' ANSIBLE_EDITOR='' "$DISPATCH" ansible-vault view "$VAULT"
}

cmd_status() {
  printf 'secrets repo:   %s %s\n' "$S" "$([[ -d $S/.git ]] && echo '(cloned)' || echo '(missing)')"
  printf 'shared helper:  %s\n' "$(shared_helper && echo yes || echo no)"
  printf 'recipients:     %s\n' "$(recipients_of "$S/recipients" | wc -l)"
  printf 'identity stubs: %s\n' "$(grep -c '^AGE-PLUGIN-YUBIKEY-' "$IDS" 2>/dev/null || true)"
  if [[ -d $S/.git ]] && git -C "$S" rev-parse -q --verify '@{u}' >/dev/null 2>&1; then
    printf 'clone:          %s ahead, %s behind GitHub (as of the last fetch)\n' \
      "$(git -C "$S" rev-list --count '@{u}..HEAD')" "$(git -C "$S" rev-list --count 'HEAD..@{u}')"
  fi
  printf 'approved here:  %s\n' "$(recipients_of "$APPROVED" | wc -l)"
  if active && cmp -s "$S/vault.yml" "$VAULT"; then echo "working copy:   in step"; else echo "working copy:   not in step (make vault-pull)"; fi
}

case ${1:-} in active) ;; *) need_dir ;; esac
case ${1:-} in
  init) cmd_init ;;
  join) cmd_join ;;
  add-key) shift; cmd_add_key "$@" ;;
  remove-key) shift; cmd_remove_key "$@" ;;
  pull) cmd_pull ;;
  edit) cmd_edit ;;
  view) cmd_view ;;
  status) cmd_status ;;
  active) active ;;
  *) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
