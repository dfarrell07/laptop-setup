#!/usr/bin/env python3
"""Tests for scripts/pull.sh (make pull): fast-forward that keeps a skip-worktree local vault."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PULL = ROOT / 'scripts/pull.sh'
VAULT = 'group_vars/all/vault.yml'
ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='t',
           GIT_AUTHOR_EMAIL='t@example.com', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.com')


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


class PullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.upstream, self.clone = base / 'upstream', base / 'clone'
        git(base, 'init', '-q', '-b', 'main', str(self.upstream))
        (self.upstream / 'group_vars/all').mkdir(parents=True)
        (self.upstream / VAULT).write_text('vault_a: ""\n')
        (self.upstream / 'README').write_text('one\n')
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-qm', 'one')
        git(base, 'clone', '-q', str(self.upstream), str(self.clone))
        (self.clone / VAULT).write_text('$ANSIBLE_VAULT;1.1;AES256\nCIPHER\n')  # the owner's encrypted vault
        git(self.clone, 'update-index', '--skip-worktree', '--', VAULT)

    def tearDown(self):
        self.tmp.cleanup()

    def upstream_commit(self, path, text):
        (self.upstream / path).write_text(text)
        git(self.upstream, 'commit', '-qam', path)

    def pull(self):
        return subprocess.run(['bash', str(PULL)], cwd=self.clone, env=ENV, capture_output=True, text=True)

    def test_keeps_the_local_vault_across_a_stub_change(self):
        self.upstream_commit(VAULT, 'vault_a: ""\nvault_b: ""\n')
        self.upstream_commit('README', 'two\n')
        self.assertNotEqual(subprocess.run(['git', 'pull', '--ff-only'], cwd=self.clone, env=ENV,
                                           capture_output=True).returncode, 0)  # what make pull exists for
        result = self.pull()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.clone / 'README').read_text(), 'two\n')
        self.assertEqual((self.clone / VAULT).read_text(), '$ANSIBLE_VAULT;1.1;AES256\nCIPHER\n')
        self.assertEqual(git(self.clone, 'ls-files', '-v', '--', VAULT)[0], 'S')
        self.assertIn('the vault stub changed upstream', result.stdout)
        self.assertFalse((self.clone / '.git/vault.yml.pull-backup').exists())

    def test_a_failed_pull_puts_the_vault_back(self):
        self.upstream_commit('README', 'upstream\n')
        (self.clone / 'README').write_text('local\n')
        git(self.clone, 'commit', '-qam', 'local')  # diverged: --ff-only fails
        result = self.pull()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.clone / VAULT).read_text(), '$ANSIBLE_VAULT;1.1;AES256\nCIPHER\n')
        self.assertEqual(git(self.clone, 'ls-files', '-v', '--', VAULT)[0], 'S')
        self.assertFalse((self.clone / '.git/vault.yml.pull-backup').exists())

    def test_refuses_other_local_changes_and_a_leftover_backup(self):
        (self.clone / 'README').write_text('edited\n')
        result = self.pull()
        self.assertIn('local changes besides', result.stderr)
        git(self.clone, 'checkout', '-q', '--', 'README')
        (self.clone / '.git/vault.yml.pull-backup').write_text('the only copy\n')
        result = self.pull()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exists from an earlier make pull', result.stderr)
        self.assertEqual((self.clone / '.git/vault.yml.pull-backup').read_text(), 'the only copy\n')

    def test_a_checkout_without_a_local_vault_pulls_plainly(self):
        git(self.clone, 'update-index', '--no-skip-worktree', '--', VAULT)
        git(self.clone, 'checkout', '-q', '--', VAULT)
        self.upstream_commit(VAULT, 'vault_a: ""\nvault_b: ""\n')
        result = self.pull()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('vault_b', (self.clone / VAULT).read_text())


if __name__ == '__main__':
    unittest.main()
