#!/usr/bin/env python3
"""Copy an SSH signing key into the vault (make vault-signing-key [KEY=~/.ssh/id_ed25519_sk_signing]).

Reads KEY (the private key file) and KEY.pub, checks they are a private key block and one public key line, decrypts
the vault in memory with scripts/vault-pass.sh (a YubiKey touch), replaces vault_ssh_signing_key and
vault_ssh_signing_key_pub, and re-encrypts with the same password. Nothing secret is printed or written in plaintext;
the vault file is replaced atomically, mode 0600.

Usage: vault-set-signing-key.py KEY VAULT_FILE
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ansible.parsing.vault import VaultLib, VaultSecret

PUB_RE = re.compile(r'^(ssh-|ecdsa-|sk-)[a-zA-Z0-9@._+-]+ [A-Za-z0-9+/]+={0,2}( [^\x00-\x1f\x7f]*)?$')
KEYS = ('vault_ssh_signing_key', 'vault_ssh_signing_key_pub')


def die(message):
    print(f'ERROR: {message}', file=sys.stderr)
    sys.exit(1)


def read_key(path):
    private = Path(os.path.expanduser(path))
    public = private.with_name(private.name + '.pub')
    for f in (private, public):
        if not f.is_file() or f.is_symlink():
            die(f'{f} is not a regular file (make the key first: ssh-keygen -t ed25519-sk -O resident '
                f'-O verify-required -C signing -f {private})')
    key = private.read_text()
    if not re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', key) or '-----END' not in key:
        die(f'{private} is not a private key file')
    pub = public.read_text().strip()
    if '\n' in pub or not PUB_RE.match(pub):
        die(f'{public} is not a single public key line')
    return key, pub


def replace_entries(text, entries):
    """Replace (or append) top-level keys in the vault's YAML text, keeping every other line as it is."""
    lines = text.splitlines(keepends=True)
    out, skipping = [], False
    seen = set()
    for line in lines:
        top = re.match(r'^([A-Za-z_][A-Za-z0-9_]*):', line)
        if top:
            skipping = top.group(1) in entries
            if skipping:
                seen.add(top.group(1))
                out.append(entries[top.group(1)])
                continue
        elif skipping and (line.startswith((' ', '\t')) or not line.strip()):
            continue  # the replaced value's continuation lines (a | block) and blank lines inside it
        else:
            skipping = False
        out.append(line)
    if out and not out[-1].endswith('\n'):
        out[-1] += '\n'
    out.extend(entries[k] for k in entries if k not in seen)
    return ''.join(out)


def main():
    if len(sys.argv) != 3:
        die('usage: vault-set-signing-key.py KEY VAULT_FILE')
    key, pub = read_key(sys.argv[1])
    vault = Path(sys.argv[2])
    raw = vault.read_bytes()
    if not raw.startswith(b'$ANSIBLE_VAULT'):
        die(f'{vault} is not encrypted: make vault-init first')
    print('Opening the vault (touch the YubiKey when it blinks)...', file=sys.stderr)
    password = subprocess.run(['scripts/vault-pass.sh'], capture_output=True, check=False)
    if password.returncode or not password.stdout.strip():
        die('scripts/vault-pass.sh gave no password (YubiKey plugged in and touched?)')
    vault_lib = VaultLib([('default', VaultSecret(password.stdout.strip()))])
    text = vault_lib.decrypt(raw).decode()
    entries = {
        'vault_ssh_signing_key': 'vault_ssh_signing_key: |\n' + ''.join(f'  {l}\n' for l in key.rstrip('\n').split('\n')),
        'vault_ssh_signing_key_pub': "vault_ssh_signing_key_pub: '" + pub.replace("'", "''") + "'\n",
    }
    updated = replace_entries(text, entries)
    encrypted = vault_lib.encrypt(updated.encode())
    fd, tmp = tempfile.mkstemp(dir=vault.parent, prefix='.vault-signing.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(encrypted)
        os.replace(tmp, vault)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f'OK: vault_ssh_signing_key and vault_ssh_signing_key_pub set from {sys.argv[1]} (values not shown)',
          file=sys.stderr)


if __name__ == '__main__':
    main()
