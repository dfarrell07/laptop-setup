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
die()  { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33mWARN:  %s\033[0m\n' "$*" >&2; }
is_vault() { head -c 14 "$1" 2>/dev/null | grep -qF "\$ANSIBLE_VAULT"; }
need() { command -v "$1" >/dev/null || die "$1 is not installed: make packages (macOS: brew install $2)"; }

github_user() {
  awk -F: '/^dotfiles_github_user:/ { v=$2; sub(/#.*/, "", v); gsub(/[ \047"]/, "", v); print v; exit }' \
    config.yml 2>/dev/null
}
user=$(github_user || true)
SECRETS_DIR=${SECRETS_DIR:-${user:+$HOME/src/$user/laptop-secrets}}
[[ -n $SECRETS_DIR ]] || die "set dotfiles_github_user in config.yml (or SECRETS_DIR)"
SECRETS_REMOTE=${SECRETS_REMOTE:-${user:+https://github.com/$user/laptop-secrets.git}}
[[ $SECRETS_DIR != *"'"* && $IDS != *"'"* ]] || die "SECRETS_DIR and the identities path must not contain a quote"
S=$SECRETS_DIR

# The recipients a file lists (comments and blank lines dropped), sorted, once each.
recipients_of() { [[ -f $1 ]] && grep -Ev '^[[:space:]]*(#|$)' "$1" | tr -d ' \t' | sort -u || true; }
shared_helper() { [[ -f $PASS_SH ]] && grep -qxF "$MARK" "$PASS_SH"; }
active() { shared_helper && [[ -f $S/vault-pass.age ]]; }

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
check_approved() {
  local now extra
  now=$(recipients_of "$S/recipients")
  [[ -n $now ]] || die "no recipients in $S/recipients: make vault-add-key"
  extra=$(comm -13 <(recipients_of "$APPROVED") <(printf '%s\n' "$now"))
  if [[ -n $extra && ${FORCE:-} != 1 ]]; then
    printf '%s\n' "$extra" | sed 's/^/  not approved here: /' >&2
    die "the recipients list has keys this machine never approved: check them (git -C $S log -p recipients), then FORCE=1"
  fi
  approve
}
approve() { mkdir -p "$CONF" && recipients_of "$S/recipients" >"$APPROVED" && chmod 600 "$APPROVED"; }

# Encrypt a password from stdin to the recipients, replacing vault-pass.age only once age has succeeded.
seal_password() {
  local tmp
  tmp=$(mktemp "$S/.vault-pass.age.XXXXXX")
  if ! age -R "$S/recipients" -o "$tmp"; then rm -f "$tmp"; die "age could not encrypt to $S/recipients"; fi
  mv -f "$tmp" "$S/vault-pass.age"
}
new_password() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }

verify_signed() {
  if git -C "$S" config --get gpg.ssh.allowedSignersFile >/dev/null || git config --get gpg.ssh.allowedSignersFile >/dev/null; then
    git -C "$S" verify-commit HEAD >/dev/null 2>&1 ||
      die "$S HEAD is not signed by an allowed key: inspect git -C $S log --show-signature before using it"
  else
    warn "no gpg.ssh.allowedSignersFile: the shared vault's commits are not signature-checked"
  fi
}

commit_push() {
  git -C "$S" add -A -- vault.yml vault-pass.age recipients .gitattributes README.md 2>/dev/null || git -C "$S" add -A
  if git -C "$S" diff --cached --quiet; then return 0; fi
  git -C "$S" commit -q -s -m "$1" || die "commit in $S failed"
  git -C "$S" push -q || die "push from $S failed: push it by hand (git -C $S push), then make vault-pull elsewhere"
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
  local first=false
  [[ -s $S/recipients && -n $(recipients_of "$S/recipients") ]] || first=true
  # The existing list must be approved here before the password is re-encrypted to it plus the new key.
  $first || check_approved
  printf '# %s\n%s\n' "$label" "$rec" >>"$S/recipients"
  approve
  if [[ -f $S/vault-pass.age ]]; then
    printf 'Re-encrypting the vault password to the new list (an enrolled key opens it: PIN, then touch)...\n'
    local pass
    pass=$(age -d -i "$IDS" "$S/vault-pass.age") || die "could not open vault-pass.age with an enrolled key"
    printf '%s' "$pass" | seal_password
    unset pass
    commit_push "Recipients: add a key"
  fi
  ok "recipient added: $rec"
}
approve_if_new_file() { [[ -f $APPROVED ]] || approve; }

cmd_remove_key() {
  local rec=${1:-${RECIPIENT:-}}
  [[ $rec =~ ^age1[a-z0-9]+$ ]] || die "usage: make vault-remove-key RECIPIENT=age1..."
  recipients_of "$S/recipients" | grep -qxF "$rec" || die "$rec is not a recipient"
  [[ $(recipients_of "$S/recipients" | wc -l) -gt 1 ]] || die "refusing to remove the last recipient"
  check_approved
  active || die "the shared vault is not active here: make vault-join"
  # Drop the recipient (and its comment line), then rotate: the old password stays readable in git history.
  awk -v r="$rec" 'BEGIN{c=""} /^[[:space:]]*#/ {c=$0; next} { gsub(/[ \t]/,""); if ($0 != r) { if (c!="") print c; print } c="" }' \
    "$S/recipients" >"$S/recipients.new" && mv -f "$S/recipients.new" "$S/recipients"
  approve
  rotate "Recipients: remove a key, new vault password"
}

# A new random password: vault-pass.age to the current recipients, vault.yml rekeyed to it.
rotate() {
  local newfile
  need age age
  newfile=$(mktemp "$PASS_SH.new.XXXXXX")
  cp -p "$S/vault-pass.age" "$S/.vault-pass.age.old"
  new_password | age -R "$S/recipients" -o "$S/.vault-pass.age.new" || die "age could not encrypt the new password"
  printf '#!/bin/sh\n{ set +x; } 2>/dev/null\nexec age -d -i %q %q\n' "$IDS" "$S/.vault-pass.age.new" >"$newfile"
  chmod 700 "$newfile"
  "$DISPATCH" ansible-vault rekey --new-vault-password-file "$newfile" "$S/vault.yml" ||
    { rm -f "$newfile" "$S/.vault-pass.age.new"; die "ansible-vault rekey failed (nothing was changed)"; }
  mv -f "$S/.vault-pass.age.new" "$S/vault-pass.age"
  rm -f "$newfile" "$S/.vault-pass.age.old"
  prove_opens "$S/vault.yml"
  install_local
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
  approve
  local newfile old_helper
  old_helper=$(mktemp "$PASS_SH.pre-shared.XXXXXX")
  cp -p "$PASS_SH" "$old_helper"
  new_password | seal_password
  cp -f -- "$VAULT" "$S/vault.yml" && chmod 600 "$S/vault.yml"
  newfile=$(mktemp "$PASS_SH.new.XXXXXX")
  printf '#!/bin/sh\n{ set +x; } 2>/dev/null\nexec age -d -i %q %q\n' "$IDS" "$S/vault-pass.age" >"$newfile"
  chmod 700 "$newfile"
  printf 'Re-encrypting the vault: touch the YubiKey for the current password, then PIN and touch for the new one...\n'
  if ! "$DISPATCH" ansible-vault rekey --new-vault-password-file "$newfile" "$S/vault.yml"; then
    rm -f "$newfile" "$S/vault.yml" "$S/vault-pass.age"
    die "ansible-vault rekey failed (nothing was changed)"
  fi
  rm -f "$newfile"
  write_helper
  if ! (prove_opens "$S/vault.yml"); then
    cp -p "$old_helper" "$PASS_SH" && (cd scripts && sha256sum vault-pass.sh >vault-pass.sh.sha256)
    die "the new vault did not open; the old helper is back and nothing was pushed"
  fi
  cp -p "$VAULT" "$VAULT.pre-shared"
  install_local
  write_repo_files
  commit_push "Shared vault: initial"
  ok "the shared vault is active. The old helper is $old_helper and the old vault $VAULT.pre-shared (keep them until"
  printf '       every machine has joined, then delete them). Other machines: make vault-join\n'
}

cmd_join() {
  need age age
  if [[ ! -d $S/.git ]]; then
    [[ -n $SECRETS_REMOTE ]] || die "set SECRETS_REMOTE"
    mkdir -p "$(dirname "$S")"
    git clone -q -- "$SECRETS_REMOTE" "$S" || die "git clone $SECRETS_REMOTE failed (gh auth login? the repo is private)"
  fi
  git -C "$S" pull -q --ff-only || die "git pull in $S failed"
  [[ -f $S/vault-pass.age && -f $S/vault.yml ]] || die "$S has no shared vault yet: make vault-share-init on the first machine"
  verify_signed
  if [[ ! -s $IDS ]]; then identity_stubs || die "no enrolled YubiKey plugged in (age-plugin-yubikey --identity listed none)"; fi
  printf 'Recipients in the shared vault:\n'; recipients_of "$S/recipients" | sed 's/^/  /'
  approve
  prove_opens_with_age
  if is_vault "$VAULT" && ! cmp -s "$S/vault.yml" "$VAULT"; then
    cp -p "$VAULT" "$VAULT.pre-shared"
    warn "this machine's own vault is kept as $VAULT.pre-shared: move any values only it has with make vault-edit"
  fi
  [[ -f $PASS_SH ]] && ! shared_helper && cp -p "$PASS_SH" "$PASS_SH.pre-shared"
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
  git -C "$S" pull -q --ff-only || die "git pull in $S failed"
  verify_signed
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
  install_local
  commit_push "Vault: edit"
}

cmd_view() {
  cmd_pull >/dev/null
  PAGER='' ANSIBLE_PAGER='' MANPAGER='' SYSTEMD_PAGER='' EDITOR='' VISUAL='' ANSIBLE_EDITOR='' "$DISPATCH" ansible-vault view "$VAULT"
}

cmd_status() {
  printf 'secrets repo:   %s %s\n' "$S" "$([[ -d $S/.git ]] && echo '(cloned)' || echo '(missing)')"
  printf 'shared helper:  %s\n' "$(shared_helper && echo yes || echo no)"
  printf 'recipients:     %s\n' "$(recipients_of "$S/recipients" | wc -l)"
  printf 'identity stubs: %s\n' "$( [[ -f $IDS ]] && grep -c '^AGE-PLUGIN-YUBIKEY-' "$IDS" || echo 0)"
  printf 'approved here:  %s\n' "$(recipients_of "$APPROVED" | wc -l)"
  if active && cmp -s "$S/vault.yml" "$VAULT"; then echo "working copy:   in step"; else echo "working copy:   not in step (make vault-pull)"; fi
}

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
