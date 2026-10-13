#!/usr/bin/env python3
"""Check timeout directive boundaries with the real Ansible template engine."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))


class SSHDaemonTemplateTests(unittest.TestCase):
    def test_timeout_lines_and_disabled_values(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            play = [{'hosts': 'localhost', 'gather_facts': False,
                     'vars_files': [str(ROOT / path) for path in [
                         'default.config.yml', 'group_vars/all/vars.yml', 'roles/system/defaults/main.yml']],
                     'vars': {'real_user_id': 'public-test-user'},
                     'tasks': [{'name': 'Render production SSH daemon template',
                                'ansible.builtin.template': {
                                    'src': str(ROOT / 'roles/system/templates/sshd-hardening.conf.j2'),
                                    'dest': str(output) + '/{{ index }}.conf', 'mode': '0600'},
                                'vars': {'system_ssh_unused_connection_timeout': '{{ item }}'},
                                'loop': [3600, 0, ''], 'loop_control': {'index_var': 'index'}}]}]
            playbook = output / 'test.yml'
            playbook.write_text(yaml.safe_dump(play))
            result = subprocess.run(
                [str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                cwd=ROOT, env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for index in range(3):
                lines = (output / f'{index}.conf').read_text().splitlines()
                directives = [line for line in lines if line.lstrip().startswith('UnusedConnectionTimeout ')]
                self.assertEqual(directives, ['UnusedConnectionTimeout 3600'] if index == 0 else [])
                self.assertTrue(any(line.startswith('TCPKeepAlive no') for line in lines))


if __name__ == '__main__':
    unittest.main()
