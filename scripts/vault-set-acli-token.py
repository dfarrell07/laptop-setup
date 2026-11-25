#!/usr/bin/env python3
"""Store an Atlassian API token in the vault (make acli-token).

Asks for the token with echo off on the terminal (or reads one line from stdin when it is not a terminal), checks it
looks like a token, then sets vault_acli_api_token the way make vault-signing-key sets the signing key: the vault is
decrypted in memory with scripts/vault-pass.sh (a YubiKey touch), re-encrypted with the same password and replaced
atomically. The token is never printed, logged or written in plaintext. Exit 3: the vault already holds it.

Usage: vault-set-acli-token.py VAULT_FILE
"""
import getpass
import importlib.util
import re
import sys
from pathlib import Path

# Atlassian API tokens are long base64url-like strings. The value is later read through a Jinja template
# (acli_api_token in group_vars/all/vars.yml), so anything outside this set, braces above all, is refused.
TOKEN_RE = re.compile(r'^[A-Za-z0-9_=+/.-]{16,1024}$')


def load_vault_helpers():
    path = Path(__file__).resolve().with_name('vault-set-signing-key.py')
    spec = importlib.util.spec_from_file_location('vault_set_signing_key', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_token():
    if sys.stdin.isatty():
        print('Create a token at https://id.atlassian.com/manage-profile/security/api-tokens', file=sys.stderr)
        token = getpass.getpass('Atlassian API token (input hidden): ')
    else:
        token = sys.stdin.readline()
    return token.strip()


def main():
    helpers = load_vault_helpers()
    if len(sys.argv) != 2:
        helpers.die('usage: vault-set-acli-token.py VAULT_FILE')
    vault = Path(sys.argv[1])
    if not vault.is_file() or not vault.read_bytes().startswith(b'$ANSIBLE_VAULT'):
        helpers.die(f'{vault} is not an encrypted vault: make vault-init (or vault-import) first; nothing changed')
    try:
        token = read_token()
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        helpers.die('cancelled; nothing changed')
    if not token:
        helpers.die('no token given; nothing changed')
    if not TOKEN_RE.match(token):
        helpers.die('that does not look like an Atlassian API token (16+ characters of letters, digits and _=+/.-); '
                    'nothing changed')
    entries = {'vault_acli_api_token': "vault_acli_api_token: '" + token + "'\n"}
    if not helpers.update_vault(sys.argv[1], {'vault_acli_api_token': token}, entries, 'vault_acli_api_token'):
        print('OK: the vault already holds this token; nothing changed', file=sys.stderr)
        sys.exit(3)
    print('OK: vault_acli_api_token set (value not shown)', file=sys.stderr)


if __name__ == '__main__':
    main()
