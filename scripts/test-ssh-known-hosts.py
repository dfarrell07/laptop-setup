#!/usr/bin/env python3
"""Exercise real SSH host-key tasks against an isolated home directory."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCE = yaml.safe_load((ROOT / 'roles/ssh/tasks/main.yml').read_text())
NAMES = [
    'Ensure SSH directory exists',
    'Ensure known_hosts has private permissions before seeding',
    'Check known_hosts exists before the read-only probe (including check mode)',
    'Read current GitHub known_hosts entries',
    'Remove stale github.com known_hosts entries before re-seeding',
    'Pre-seed GitHub known_hosts',
]
TASKS = [next(task for task in SOURCE if task.get('name') == name) for name in NAMES]
KEY = yaml.safe_load((ROOT / 'roles/ssh/defaults/main.yml').read_text())['ssh_github_known_hosts_fallback'][0]
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = shutil.which('ansible-playbook')
STALE_KEY = next(task['ansible.builtin.set_fact']['ssh_signing_key_pub']
                 for task in yaml.safe_load((ROOT / 'molecule/shared/system-container-overrides.yml').read_text())
                 if 'ssh_signing_key_pub' in task.get('ansible.builtin.set_fact', {}))


class KnownHostsTests(unittest.TestCase):
    def converge(self, home):
        playbook = home / 'test.yml'
        playbook.write_text(yaml.safe_dump([{
            'name': 'Isolated production known_hosts tasks', 'hosts': 'localhost',
            'gather_facts': False,
            'vars': {'ansible_facts': {'user_dir': str(home)}, 'ssh_github_ok': False,
                     'ssh_github_ed25519_keys': [], 'ssh_github_known_hosts_fallback': [KEY]},
            'tasks': TASKS,
        }]))
        result = subprocess.run(
            [str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
            env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return int(re.findall(r'changed=([0-9]+)', result.stdout)[-1])

    def test_fresh_seed_is_private_and_repeat_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertGreater(self.converge(home), 0)
            hosts = home / '.ssh/known_hosts'
            self.assertEqual(hosts.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.converge(home), 0)

    def test_stale_keys_are_replaced_and_other_hosts_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.ssh').mkdir()
            hosts = home / '.ssh/known_hosts'
            hosts.write_text(f'github.com {STALE_KEY}\nother.example.com {STALE_KEY}\n')
            hosts.chmod(0o644)
            self.assertGreater(self.converge(home), 0)
            self.assertEqual(hosts.stat().st_mode & 0o777, 0o600)
            github = subprocess.check_output(['ssh-keygen', '-F', 'github.com', '-f', str(hosts)], text=True)
            self.assertIn(KEY, github)
            self.assertNotIn(STALE_KEY, github)
            self.assertIn('other.example.com', hosts.read_text())
            self.assertEqual(self.converge(home), 0)


if __name__ == '__main__':
    unittest.main()
