#!/usr/bin/env python3
"""Verify the actual vault template and reject plaintext secrets or YAML tricks."""
from pathlib import Path
import os
import pty
import select
import time
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
VERIFY = ROOT / 'scripts/verify-vault-stub.py'


class VaultStubTests(unittest.TestCase):
    def check(self, content, accepted):
        result = subprocess.run(['python3', str(VERIFY)], input=content, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_committed_template_and_empty_types(self):
        committed = (ROOT / 'group_vars/all/vault.yml').read_text()
        # After the documented ansible-vault encrypt the file is ciphertext, not the stub
        if not committed.startswith('$ANSIBLE_VAULT'):
            self.check(committed, True)
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
# Fake verify-ansible-args.sh: log the call, then act like ansible-vault encrypt/edit.
echo "$*" >> "$(dirname "$0")/calls.log"
case "$2" in
  encrypt) printf '$ANSIBLE_VAULT;1.1;AES256\\n3031\\n' > "$3" ;;
  edit) [ -z "${FAKE_EDIT:-}" ] || echo 3032 >> "$3"; exit "${FAKE_EDIT_RC:-0}" ;;
  view) printf '%s\\n' "${FAKE_VIEW:-vault_ssh_auth_key: x}" ;;
esac
"""


class VaultInitTests(unittest.TestCase):
    """make vault-init: refuses the stub, sets skip-worktree, encrypts once, warns on an unchanged edit."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        (self.repo / 'scripts').mkdir()
        (self.repo / 'group_vars/all').mkdir(parents=True)
        for name in ('vault-init.sh', 'vault-pass-ci.sh'):
            shutil.copy(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        shutil.copy(ROOT / 'group_vars/all/vault.yml', self.repo / 'group_vars/all/vault.yml')
        dispatch = self.repo / 'scripts/verify-ansible-args.sh'
        dispatch.write_text(FAKE_DISPATCH)
        dispatch.chmod(0o700)
        git = ['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false']
        subprocess.run(git[:3] + ['init', '-q'], check=True)
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-qm', 'init', '--no-verify'], check=True)

    def write_pass(self, body):
        path = self.repo / 'scripts/vault-pass.sh'
        path.write_text('#!/bin/bash\n' + body + '\nprintf "%s" "$pass"\n')
        path.chmod(0o700)

    def run_init(self, **env):
        """Run with a pty on stdin/stdout (the script requires a terminal); return (rc, output)."""
        main, sub = pty.openpty()
        proc = subprocess.Popen(['bash', str(self.repo / 'scripts/vault-init.sh')], stdin=sub, stdout=sub,
                                stderr=sub, env={**os.environ, **env})
        os.close(sub)
        out, deadline = b'', time.monotonic() + 30
        while True:
            ready, _, _ = select.select([main], [], [], max(0, deadline - time.monotonic()))
            if not ready:  # waiting for input or hung: fail instead of blocking CI
                proc.kill()
                proc.wait()
                os.close(main)
                self.fail('vault-init.sh did not finish within 30 s; output: ' + out.decode(errors='replace'))
            try:
                chunk = os.read(main, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.close(main)
        return proc.wait(timeout=30), out.decode(errors='replace')

    def calls(self):
        log = self.repo / 'scripts/calls.log'
        return log.read_text().splitlines() if log.exists() else []

    def skip_worktree(self):
        out = subprocess.run(['git', '-C', str(self.repo), 'ls-files', '-v', 'group_vars/all/vault.yml'],
                             capture_output=True, text=True, check=True).stdout
        return out.startswith('S ')

    def test_refuses_stub_and_changes_nothing(self):
        for body in ("pass='CHANGE_ME'", '  export pass=CHANGE_ME'):
            self.write_pass(body)
            rc, out = self.run_init()
            self.assertNotEqual(rc, 0)
            self.assertIn('still the stub', out)
        shutil.copy(ROOT / 'scripts/vault-pass-ci.sh', self.repo / 'scripts/vault-pass.sh')
        rc, out = self.run_init()
        self.assertIn('still the stub', out)
        self.assertFalse(self.skip_worktree())
        self.assertEqual(self.calls(), [])

    def test_encrypts_once_and_warns_on_unchanged_edit(self):
        self.write_pass("pass='real-yubikey-derived'")
        rc, out = self.run_init(FAKE_EDIT='1')
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.skip_worktree())
        self.assertTrue((self.repo / 'group_vars/all/vault.yml').read_text().startswith('$ANSIBLE_VAULT'))
        self.assertIn('vault saved', out)
        rc, out = self.run_init()
        self.assertEqual(rc, 0, out)
        self.assertIn('already encrypted', out)
        self.assertIn('vault unchanged (nothing saved)', out)  # keys may be present: no 'still empty' claim
        self.assertNotIn('still empty', out)
        self.assertEqual([c.split()[1] for c in self.calls()], ['encrypt', 'edit', 'view', 'edit'])

    def test_invalid_yaml_after_edit_fails(self):
        self.write_pass("pass='real-yubikey-derived'")
        rc, out = self.run_init(FAKE_EDIT='1', FAKE_VIEW="vault_ssh_auth_key: ''\n  -----BEGIN")
        self.assertNotEqual(rc, 0)
        self.assertIn('not valid YAML', out)
        self.assertNotIn('vault saved', out)

    def test_unchanged_first_edit_warns_keys_empty(self):
        self.write_pass("pass='real-yubikey-derived'")
        rc, out = self.run_init()
        self.assertEqual(rc, 0, out)
        self.assertIn('still empty', out)
        self.assertNotIn('vault saved', out)

    def test_edit_failure_explains_recovery(self):
        self.write_pass("pass='real-yubikey-derived'")
        rc, out = self.run_init(FAKE_EDIT_RC='1')
        self.assertNotEqual(rc, 0)
        self.assertIn('YubiKey', out)
        self.assertIn('vault.yml.bak', out)  # backup before any checkout of the stub


if __name__ == '__main__':
    unittest.main()
