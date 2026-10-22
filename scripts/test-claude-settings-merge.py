#!/usr/bin/env python3
"""Unit tests for roles/claude/files/merge-claude-settings.py (temp dirs only)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / 'roles/claude/files/merge-claude-settings.py'
POLICY = {
    'env': {'DISABLE_TELEMETRY': '1'},
    'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False},
    'cleanupPeriodDays': 30,
    'enableAllProjectMcpServers': False,
    'permissions': {'deny': ['Read(~/.ssh/**)', 'Edit(**/.githooks/**)']},
}


def run(path, policy=POLICY, *extra, raw=None):
    return subprocess.run([sys.executable, '-I', str(HELPER), str(path), *extra],
                          input=raw if raw is not None else json.dumps(policy),
                          capture_output=True, text=True, timeout=30)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / 'settings.json'
        self.addCleanup(self._tmp.cleanup)

    def load(self):
        return json.loads(self.path.read_text())

    def test_creates_missing_file_0600(self):
        result = run(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'changed')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.load()['permissions']['deny'], POLICY['permissions']['deny'])

    def test_preserves_user_keys_and_applies_rules(self):
        self.path.write_text(json.dumps({
            'theme': 'dark', 'model': 'x', 'enabledPlugins': {'a@b': True},
            'env': {'FOO': '1', 'DISABLE_TELEMETRY': '0'},
            'sandbox': {'enabled': False, 'excludedCommands': ['docker'],
                        'allowUnsandboxedCommands': True, 'failIfUnavailable': False},
            'enableAllProjectMcpServers': True, 'cleanupPeriodDays': 999,
            'permissions': {'allow': ['Bash(ls)'], 'ask': ['Bash(rm:*)'],
                            'deny': ['Read(~/x)', 'Read(~/.ssh/**)'], 'defaultMode': 'plan'}}))
        self.path.chmod(0o600)
        self.assertEqual(run(self.path).returncode, 0)
        data = self.load()
        self.assertEqual((data['theme'], data['model']), ('dark', 'x'))
        self.assertEqual(data['enabledPlugins'], {'a@b': True})
        self.assertEqual(data['env'], {'FOO': '1', 'DISABLE_TELEMETRY': '1'})
        self.assertTrue(data['sandbox']['enabled'])
        self.assertFalse(data['sandbox']['allowUnsandboxedCommands'])
        self.assertTrue(data['sandbox']['failIfUnavailable'])
        self.assertEqual(data['sandbox']['excludedCommands'], ['docker'])
        self.assertFalse(data['enableAllProjectMcpServers'])
        self.assertEqual(data['cleanupPeriodDays'], 30)
        self.assertEqual(data['permissions']['deny'],
                         ['Read(~/.ssh/**)', 'Edit(**/.githooks/**)', 'Read(~/x)'])
        self.assertEqual(data['permissions']['allow'], ['Bash(ls)'])
        self.assertEqual(data['permissions']['ask'], ['Bash(rm:*)'])
        self.assertEqual(data['permissions']['defaultMode'], 'plan')
        self.assertEqual(len(list(self.dir.glob('settings.json.bak-*'))), 1)

    def test_idempotent(self):
        run(self.path)
        before = self.path.read_text()
        result = run(self.path)
        self.assertEqual(result.stdout.strip(), 'unchanged')
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(list(self.dir.glob('settings.json.bak-*')), [])

    def test_invalid_existing_json_fails_without_clobbering(self):
        for content in ['{not json', '[1, 2]', ' \n\t\n']:
            self.path.write_text(content)
            result = run(self.path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('refusing to overwrite', result.stderr)
            self.assertEqual(self.path.read_text(), content)

    def test_empty_existing_file_is_replaced(self):
        self.path.write_text('')
        result = run(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'changed')
        self.assertEqual(self.load()['cleanupPeriodDays'], 30)

    def test_back_to_back_changes_keep_every_backup(self):
        self.path.write_text(json.dumps({'theme': 'dark'}))
        for value in (1, 2):
            self.assertEqual(run(self.path).returncode, 0)
            self.path.write_text(json.dumps({'theme': 'dark', 'cleanupPeriodDays': value}))
        self.assertEqual(run(self.path).returncode, 0)
        self.assertEqual(len(list(self.dir.glob('settings.json.bak-*'))), 3)

    def test_symlinked_settings_are_written_through(self):
        target = self.dir / 'real.json'
        target.write_text(json.dumps({'theme': 'dark'}))
        self.path.symlink_to(target)
        self.assertEqual(run(self.path).returncode, 0)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(target.read_text())['theme'], 'dark')
        self.assertEqual(json.loads(target.read_text())['cleanupPeriodDays'], 30)

    def test_invalid_policy_fails(self):
        result = run(self.path, raw='nope')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.path.exists())

    def test_check_mode_writes_nothing(self):
        result = run(self.path, POLICY, '--check')
        self.assertEqual(result.stdout.strip(), 'changed')
        self.assertFalse(self.path.exists())

    def test_legacy_file_removed_only_if_it_has_policy_shape(self):
        legacy = self.dir / 'settings.local.json'
        legacy.write_text(json.dumps(POLICY))
        run(self.path, POLICY, '--remove-legacy', str(legacy))
        self.assertFalse(legacy.exists())
        legacy.write_text(json.dumps({**POLICY, 'theme': 'dark'}))
        self.assertEqual(run(self.path, POLICY, '--remove-legacy', str(legacy)).returncode, 0)
        self.assertTrue(legacy.exists())
        legacy.write_text('{broken')
        self.assertEqual(run(self.path, POLICY, '--remove-legacy', str(legacy)).returncode, 0)
        self.assertTrue(legacy.exists())


if __name__ == '__main__':
    unittest.main()
