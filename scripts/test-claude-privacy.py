#!/usr/bin/env python3
"""Exercise the production tmpfiles template without changing the real home."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent


class ClaudePrivacyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('systemd-tmpfiles'), 'Requires Linux systemd-tmpfiles')
    def test_recursive_privacy_preserves_file_modes_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            default = home / '.claude'
            personal = home / '.claude-personal'
            nested = default / 'nested'
            nested.mkdir(parents=True)
            personal.mkdir()
            for path in [default, nested, personal]:
                path.chmod(0o755)
            modes = {default / 'CLAUDE.md': 0o600, default / 'settings.json': 0o644,
                     nested / 'session.json': 0o644, nested / 'run.sh': 0o755,
                     personal / 'settings.json': 0o644}
            for path, mode in modes.items():
                path.write_text('public test fixture\n')
                path.chmod(mode)
            outside = home / 'outside.txt'
            outside.write_text('public outside fixture\n')
            outside.chmod(0o644)
            (nested / 'outside-link').symlink_to(outside)
            template = Environment(undefined=StrictUndefined).from_string(
                (ROOT / 'roles/claude/templates/claude-privacy.conf.j2').read_text())
            config = home / 'privacy.conf'
            config.write_text(template.render(_claude_instances=[str(default), str(personal)],
                                              claude_default_config_dir=str(default)).replace('%h', str(home)))
            for _ in range(2):
                result = subprocess.run(['systemd-tmpfiles', '--user', '--create', str(config)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                for path in [default, nested]:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                for path in modes:
                    expected = 0o700 if path.name == 'run.sh' else 0o600
                    self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))
                self.assertEqual(outside.stat().st_mode & 0o777, 0o644)

    def test_policy_targets_user_settings_not_local_settings(self):
        tasks = (ROOT / 'roles/claude/tasks/main.yml').read_text()
        defaults = (ROOT / 'roles/claude/defaults/main.yml').read_text()
        template = (ROOT / 'roles/claude/templates/settings-policy.json.j2').read_text()
        self.assertIn('merge-claude-settings.py', tasks)
        self.assertIn("item ~ '/settings.json'", tasks)
        self.assertNotRegex(tasks, r'dest:\s*"?\{\{ item \}\}/settings\.local\.json')
        self.assertFalse((ROOT / 'roles/claude/templates/settings.local.json.j2').exists())
        for key in ['"sandbox"', '"enableAllProjectMcpServers"', '"deny"']:
            self.assertIn(key, template)
        self.assertIn('"**/.githooks/**"', defaults)
        self.assertIn('"**/.git/hooks/**"', defaults)
        for rule in ['"**/.claude/settings.json"', '"**/.claude/settings.local.json"', '"**/.mcp.json"']:
            self.assertIn(rule, defaults)
        self.assertNotIn('Write(', template)

    def test_weaker_nested_sandbox_only_in_containers(self):
        defaults = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
        self.assertNotIn('enableWeakerNestedSandbox', defaults['claude_sandbox_config'])
        ansible = ROOT / '.venv/bin/ansible'
        if not ansible.exists():
            self.skipTest('no .venv ansible')
        for in_container, tier, expected in [(False, 'host-only', False), (False, 'hybrid', False),
                                             (True, 'host-only', True), (False, 'container', True)]:
            with tempfile.TemporaryDirectory() as tmp:
                vars_file = Path(tmp) / 'v.json'
                vars_file.write_text(json.dumps({
                    **defaults, 'system_is_container': in_container, 'needs_container_tier': tier,
                    'ansible_facts': {'user_dir': tmp}, '_claude_instances': [], 'item': tmp}))
                out = Path(tmp) / 'o.json'
                result = subprocess.run(
                    [str(ansible), 'localhost', '-m', 'template', '-a',
                     f'src={ROOT}/roles/claude/templates/settings-policy.json.j2 dest={out}',
                     '-e', f'@{vars_file}', '-i', 'localhost,', '-c', 'local'],
                    capture_output=True, text=True, timeout=120, cwd=tmp,
                    env={**os.environ, 'ANSIBLE_LOCAL_TEMP': tmp, 'ANSIBLE_REMOTE_TEMP': tmp, 'HOME': tmp})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sandbox = json.loads(out.read_text())['sandbox']
                self.assertIs(sandbox['enableWeakerNestedSandbox'], expected, (in_container, tier))


if __name__ == '__main__':
    unittest.main()
