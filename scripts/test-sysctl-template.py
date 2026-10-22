#!/usr/bin/env python3
"""Check complete sysctl assignments rendered by the real Ansible engine."""
import json
import importlib.util
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

INJECTED_CORE_PATTERN = '|/usr/lib/systemd/systemd-coredump %P;`touch x`$(id)&<>'


class SysctlTemplateTests(unittest.TestCase):
    def test_firewall_readiness_waits_and_times_out(self):
        spec = importlib.util.spec_from_file_location('wait_firewalld', ROOT / 'roles/system/files/wait-firewalld.py')
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'firewall-cmd'
            counter = root / 'attempts'
            binary.write_text('#!/bin/sh\nn=0\n[ ! -f "' + str(counter) + '" ] || n=$(cat "' + str(counter) + '")\n'
                              'n=$((n + 1))\necho "$n" > "' + str(counter) + '"\n[ "$n" -ge 3 ]\n')
            binary.chmod(0o755)
            self.assertTrue(helper.wait_ready(binary, timeout=2, interval=0.01))
            self.assertEqual(counter.read_text().strip(), '3')
            binary.write_text('#!/bin/sh\nexit 1\n')
            self.assertFalse(helper.wait_ready(binary, timeout=0.05, interval=0.01))
            self.assertTrue(helper.wait_ready(root / 'absent', timeout=0.05, interval=0.01))

    def test_module_lock_service_has_valid_boot_ordering(self):
        content = (ROOT / 'roles/system/templates/kernel-modules-lockdown.service.j2').read_text()
        self.assertIn('After=systemd-modules-load.service network-online.target firewalld.service NetworkManager.service', content)
        self.assertIn('ExecStart=/usr/sbin/sysctl -w kernel.modules_disabled=1', content)
        self.assertNotIn('After=multi-user.target', content)
        binary = shutil.which('systemd-analyze')
        if binary:
            with tempfile.TemporaryDirectory() as directory:
                unit = Path(directory) / 'kernel-modules-lockdown.service'
                unit.write_text(content)
                result = subprocess.run([binary, 'verify', str(unit)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_each_assignment_retains_its_line_and_value(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            play = [{'hosts': 'localhost', 'gather_facts': False,
                     'vars_files': [str(ROOT / path) for path in [
                         'default.config.yml', 'group_vars/all/vars.yml', 'roles/system/defaults/main.yml']],
                     'vars': {'profile': 'work', 'ansible_managed': 'Ansible managed'},
                     'tasks': [{'name': 'Inject shell metacharacters into the core pattern',
                                'ansible.builtin.set_fact': {'system_core_pattern': INJECTED_CORE_PATTERN}},
                               {'name': 'Resolve the merged production policy',
                                'ansible.builtin.set_fact': {'system_sysctl_hardening':
                                    '{{ system_sysctl_hardening | combine(_system_sysctl_work_defaults) | combine(_system_sysctl_performance_defaults) | combine({"kernel.core_pattern": system_core_pattern, "kernel.modules_disabled": system_modules_disabled | int}) }}'}},
                               {'name': 'Render complete production sysctl template',
                                'ansible.builtin.template': {
                                    'src': str(ROOT / 'roles/system/templates/sysctl-hardening.conf.j2'),
                                    'dest': str(output / 'sysctl.conf'), 'mode': '0600'}},
                               {'name': 'Capture resolved expected policy values',
                                'ansible.builtin.copy': {'content': '{{ system_sysctl_hardening | to_json }}',
                                                        'dest': str(output / 'expected.json'), 'mode': '0600'}}]}]
            playbook = output / 'test.yml'
            playbook.write_text(yaml.safe_dump(play))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(playbook)],
                cwd=ROOT, env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = json.loads((output / 'expected.json').read_text())
            # The one-way module lock belongs to the ordered service, not early sysctl.
            expected.pop('kernel.modules_disabled', None)
            assignments = {}
            for line in (output / 'sysctl.conf').read_text().splitlines():
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                key, separator, value = line.partition(' = ')
                self.assertEqual(separator, ' = ', line)
                self.assertNotIn(key, assignments, line)
                assignments[key] = value.split('  #', 1)[0]
            self.assertEqual(set(assignments), set(expected))
            for key, value in expected.items():
                if key == 'kernel.core_pattern':
                    value = ''.join(c for c in str(value) if c not in '`$();&<>')
                self.assertEqual(assignments[key], str(value), key)
            self.assertEqual(assignments['kernel.core_pattern'], '|/usr/lib/systemd/systemd-coredump %Ptouch xid')


if __name__ == '__main__':
    unittest.main()
