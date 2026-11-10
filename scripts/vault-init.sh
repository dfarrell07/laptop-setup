#!/usr/bin/env bash
# vault-init.sh (make vault-init) — first-time vault setup after make setup-yubikeys and make ssh-keys: mark the vault
# skip-worktree (the committed file stays the stub), then encrypt it with the SSH keys filled in: vault_ssh_auth_key
# from ~/.ssh/id_ed25519_sk, vault_ssh_auth_key_pub from its .pub and every id_ed25519_sk_backup*.pub. The keys reach
# ansible-vault on stdin, never the plaintext file. Later edits: make vault-edit.
set -euo pipefail
# SECURITY: ansible-vault runs vault-pass.sh, which prints the vault password; never trace it.
{ set +x; } 2>/dev/null

cd "$(dirname "$0")/.."
VAULT=group_vars/all/vault.yml
PASS_SH=scripts/vault-pass.sh
DISPATCH=scripts/verify-ansible-args.sh
KEY=$HOME/.ssh/id_ed25519_sk

die() { printf '\033[0;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[0;32mOK:    %s\033[0m\n' "$*"; }

[[ -f $PASS_SH && -x $PASS_SH ]] || die "$PASS_SH missing or not executable: run make setup-yubikeys first"
if grep -qE "^pass=['\"]CHANGE_ME['\"]|ci-dummy-vault-password" "$PASS_SH"; then
  die "$PASS_SH is still the stub: run make setup-yubikeys first (encrypting with the stub password protects nothing)"
fi

encrypted=false
head -c 14 "$VAULT" | grep -qF "\$ANSIBLE_VAULT" && encrypted=true
# Fill each SSH key line still '' in the stub from the key files; lines filled by hand stay. Nothing is written here.
read -r -d '' FILL <<'EOF' || true
import glob, os, sys
vault, key = sys.argv[1], sys.argv[2]
lines = open(vault).read().splitlines(keepends=True)
empty = {name: i for i, line in enumerate(lines) for name in ('vault_ssh_auth_key', 'vault_ssh_auth_key_pub')
         if line == name + ": ''\n"}
if empty and not (os.path.isfile(key) and os.path.isfile(key + '.pub')):
    sys.exit(f'no {key}: run make ssh-keys first')
for name, i in empty.items():
    files = [key] if name == 'vault_ssh_auth_key' else [key + '.pub'] + sorted(glob.glob(key + '_backup*.pub'))
    if not open(files[0]).read().strip():
        sys.exit(f'{files[0]} is empty: run make ssh-keys')
    text = '\n'.join(open(f).read().strip() for f in files)  # one key per line, even without a final newline
    lines[i] = name + ': |\n' + ''.join('  ' + line + '\n' for line in text.splitlines())
out = ''.join(lines)
try:
    import yaml
    data = yaml.safe_load(out)
except ImportError:
    print('WARN: no PyYAML for python3: the vault YAML and empty-key checks are skipped', file=sys.stderr)
    data = None
except yaml.YAMLError as e:  # its message quotes the bad line, which may be a secret: give only the position
    m = getattr(e, 'problem_mark', None)
    sys.exit(f'{vault} is not valid YAML' + (f' (line {m.line + 1}, column {m.column + 1})' if m else ''))
# An empty key means no authorized_keys after hardening: a lockout, so never encrypt one.
if data is not None and not all(str(data.get(n) or '').strip() for n in ('vault_ssh_auth_key', 'vault_ssh_auth_key_pub')):
    sys.exit(f'{vault}: vault_ssh_auth_key or vault_ssh_auth_key_pub would be empty (empty key file?): run make ssh-keys')
sys.stdout.write(out)
EOF
$encrypted || filled=$(python3 -I -c "$FILL" "$VAULT" "$KEY") || die "could not fill the vault (message above)"

if git ls-files -v -- "$VAULT" | grep -q '^[Ss] '; then
  ok "$VAULT already marked skip-worktree"
else
  git update-index --skip-worktree -- "$VAULT" || die "git update-index failed: is $VAULT tracked in this checkout?"
  ok "$VAULT marked skip-worktree (git ignores local changes to it)"
fi
if $encrypted; then
  ok "$VAULT already encrypted: change it with make vault-edit"
  exit 0
fi

printf 'Encrypting %s (insert one YubiKey and touch it when it blinks)...\n' "$VAULT"
printf '%s\n' "$filled" | "$DISPATCH" ansible-vault encrypt --output "$VAULT" - \
  || die "encrypt failed: insert the YubiKey and re-run make vault-init (safe to repeat)"
ok "vault encrypted. Next: CLAUDE.md steps 3a-3c (usb-add, preflight, backup, make check), then chunks in make status order"
