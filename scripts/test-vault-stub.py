#!/usr/bin/env python3
"""Verify the actual vault template and reject plaintext secrets or YAML tricks."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / 'scripts/verify-vault-stub.py'
# The committed stub from the index: after make vault-init the working-tree vault is ciphertext (skip-worktree).
HAVE_YAML = subprocess.run(['python3', '-I', '-c', 'import yaml'], capture_output=True).returncode == 0
STUB = subprocess.run(['git', '-C', str(ROOT), 'show', ':group_vars/all/vault.yml'],
                      capture_output=True, text=True, check=True).stdout


class VaultStubTests(unittest.TestCase):
    def check(self, content, accepted):
        result = subprocess.run(['python3', str(VERIFY)], input=content, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_committed_template_and_empty_types(self):
        self.check(STUB, True)
        for value in ('[]', "''", '""'):
            self.check('---\nvault_placeholder: "encrypt-me"\nvault_key: ' + value + ' # empty\n', True)

    def test_nonempty_and_disguised_values(self):
        marker = 'vault_placeholder: "encrypt-me"\n'
        for extra in ('vault_key: secret\n', 'vault_key: [] + secret\n',
                      'vault_key: []\n  - secret\n', 'vault_key: |\n  secret\n',
                      'other_key: secret\n', ' vault_key: secret\n',
                      'vault_key: !!str []\n', 'vault_key: &value secret\n', 'vault_key: ""#secret\n',
                      'vault_key: []\nvault_key: ""\n', '---\nvault_key: []\n'):
            with self.subTest(extra=extra):
                self.check(marker + extra, False)
        self.check('# vault_placeholder\nvault_key: []\n', False)
        for comment in ('# -----BEGIN OPENSSH PRIVATE KEY-----', '#   b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW', '# PRIVATE KEY'):
            with self.subTest(comment=comment):
                self.check(marker + comment + '\nvault_key: []\n', False)
        for tail in ('# -----BEGIN OPENSSH PRIVATE KEY-----', '# b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAA'):
            self.check(marker + 'vault_key: [] ' + tail + '\n', False)
            self.check('vault_placeholder: "encrypt-me" ' + tail + '\nvault_key: []\n', False)
        self.check(marker + '# Populate these, then run ansible-vault encrypt\nvault_key: []\n', True)
        self.check('vault_placeholder: secret\nvault_key: []\n', False)
        self.check('', False)
        self.check(marker + '# PRIVATE\0 KEY\nvault_key: []\n', False)
        self.check(marker + ''.join('vault_k%d: []\n' % i for i in range(40000)), False)
        self.check(marker + '  \u00a0# hunter2 correct horse battery staple\n', False)
        self.check(marker + 'vault_' + 'a' * 60 + ': []\n', False)
        self.check(marker + 'vault_' + 'a' * 56 + ': []\n', True)
        self.check(marker + '# private_key backup\nvault_key: []\n', False)
        self.check(marker + 'vault_x: x' + ' ' * 5000 + 'y\n', False)


FAKE_DISPATCH = """#!/bin/bash
# Fake verify-ansible-args.sh: log the call; for encrypt --output FILE -, keep stdin as FILE.plain and write a header.
echo "$*" >> "$(dirname "$0")/calls.log"
[ "$2 $3 $5" = "encrypt --output -" ] && cat > "$4.plain" && printf '$ANSIBLE_VAULT;1.1;AES256\\n3031\\n' > "$4"
"""


class VaultInitTests(unittest.TestCase):
    """make vault-init: refuses the stub or missing keys, sets skip-worktree, encrypts the filled stub once."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / 'scripts').mkdir()
        (self.repo / 'group_vars/all').mkdir(parents=True)
        shutil.copy(ROOT / 'scripts/vault-init.sh', self.repo / 'scripts/vault-init.sh')
        (self.repo / 'group_vars/all/vault.yml').write_text(STUB)
        dispatch = self.repo / 'scripts/verify-ansible-args.sh'
        dispatch.write_text(FAKE_DISPATCH)
        dispatch.chmod(0o700)
        git = ['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false']
        subprocess.run(git[:3] + ['init', '-q'], check=True)
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-qm', 'init', '--no-verify'], check=True)
        self.home = self.repo / 'home'
        (self.home / '.ssh').mkdir(parents=True)

    def write_pass(self, body):
        path = self.repo / 'scripts/vault-pass.sh'
        path.write_text('#!/bin/bash\n' + body + '\nprintf "%s" "$pass"\n')
        path.chmod(0o700)

    def write_keys(self):
        ssh = self.home / '.ssh'
        (ssh / 'id_ed25519_sk').write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n')
        for name, serial in (('id_ed25519_sk', 1), ('id_ed25519_sk_backup', 2), ('id_ed25519_sk_signing', 3)):
            (ssh / (name + '.pub')).write_text('sk-ssh-ed25519@openssh.com AAAA%d yubikey-%d' % (serial, serial))

    def run_init(self):
        r = subprocess.run(['bash', str(self.repo / 'scripts/vault-init.sh')], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30, env={**os.environ, 'HOME': str(self.home)})
        return r.returncode, r.stdout + r.stderr

    def plain(self):
        return self.repo / 'group_vars/all/vault.yml.plain'

    def calls(self):
        log = self.repo / 'scripts/calls.log'
        return log.read_text().splitlines() if log.exists() else []

    def skip_worktree(self):
        out = subprocess.run(['git', '-C', str(self.repo), 'ls-files', '-v', 'group_vars/all/vault.yml'],
                             capture_output=True, text=True, check=True).stdout
        return out.startswith('S ')

    def test_refuses_stub_pass_or_missing_keys_and_changes_nothing(self):
        self.write_pass("pass='CHANGE_ME'")
        self.write_keys()
        rc, out = self.run_init()
        self.assertNotEqual(rc, 0)
        self.assertIn('still the stub', out)
        shutil.copy(ROOT / 'scripts/vault-pass-ci.sh', self.repo / 'scripts/vault-pass.sh')
        _, out = self.run_init()
        self.assertIn('still the stub', out)
        self.write_pass("pass='real-yubikey-derived'")
        (self.home / '.ssh/id_ed25519_sk').write_text('')
        rc, out = self.run_init()
        self.assertNotEqual(rc, 0)
        self.assertIn('is empty', out)
        (self.home / '.ssh/id_ed25519_sk').unlink()
        rc, out = self.run_init()
        self.assertNotEqual(rc, 0)
        self.assertIn('make ssh-keys', out)
        self.assertFalse(self.skip_worktree())
        self.assertEqual(self.calls(), [])

    def test_encrypts_filled_stub_once(self):
        self.write_pass("pass='real-yubikey-derived'")
        self.write_keys()
        rc, out = self.run_init()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.skip_worktree())
        vault = self.repo / 'group_vars/all/vault.yml'
        self.assertTrue(vault.read_text().startswith('$ANSIBLE_VAULT'))
        data = yaml.safe_load(self.plain().read_text())
        self.assertEqual(data['vault_ssh_auth_key'], (self.home / '.ssh/id_ed25519_sk').read_text())
        self.assertEqual(data['vault_ssh_auth_key_pub'].splitlines(),
                         ['sk-ssh-ed25519@openssh.com AAAA1 yubikey-1', 'sk-ssh-ed25519@openssh.com AAAA2 yubikey-2'])
        rc, out = self.run_init()
        self.assertEqual(rc, 0, out)
        self.assertIn('already encrypted', out)
        self.assertEqual(len(self.calls()), 1)

    @unittest.skipUnless(HAVE_YAML, 'vault-init checks YAML only when python3 has PyYAML')
    def test_hand_filled_vault_kept_and_bad_yaml_refused(self):
        self.write_pass("pass='real-yubikey-derived'")
        vault = self.repo / 'group_vars/all/vault.yml'
        stub = vault.read_text()
        filled = stub.replace("vault_ssh_auth_key: ''", "vault_ssh_auth_key: a").replace("_pub: ''", "_pub: b", 1)
        vault.write_text(filled.replace("vault_ssh_auth_key: a", "vault_ssh_auth_key: |\n  -----BEGIN\n x: SECRET"))
        rc, out = self.run_init()
        self.assertNotEqual(rc, 0)
        self.assertIn('not valid YAML', out)
        self.assertNotIn('SECRET', out)
        self.assertEqual(self.calls(), [])
        vault.write_text(filled)
        rc, out = self.run_init()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.plain().read_text(), filled)


if __name__ == '__main__':
    unittest.main()
