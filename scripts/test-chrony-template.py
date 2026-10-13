#!/usr/bin/env python3
"""Render the actual chrony template and validate its optional directive boundary."""
import os
import importlib.util
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
SPEC = importlib.util.spec_from_file_location('validate_chrony', ROOT / 'roles/system/files/validate-chrony.py')
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


class ChronyTemplateTests(unittest.TestCase):
    def test_optional_leap_directives_and_native_parser(self):
        binary = shutil.which('chronyd')
        supported = True
        if binary:
            probe = subprocess.run([binary, '-p', '-f', '/dev/null', 'leapseclist /usr/share/zoneinfo/leap-seconds.list'],
                                   capture_output=True, text=True)
            if probe.returncode:
                self.assertIn('Invalid directive', probe.stderr)
                supported = False
        for leap, capability in [('/usr/share/zoneinfo/leap-seconds.list', supported),
                                 ('/usr/share/zoneinfo/leap-seconds.list', False),
                                 ('/usr/share/zoneinfo/leap-seconds.list', None), ('', supported)]:
            with self.subTest(leap=leap, capability=capability), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'chrony.conf'
                play = Path(directory) / 'render.yml'
                play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                    'vars_files': [str(ROOT / 'roles/system/defaults/main.yml')],
                    'vars': {'ansible_managed': 'Ansible managed', 'is_dnf': True, 'is_apt': False,
                             **({'_chrony_leapseclist_probe': {'rc': 0 if capability else 1}}
                                if capability is not None else {})},
                    'tasks': [{'ansible.builtin.set_fact': {'system_chrony_leapseclist': leap}},
                              {'ansible.builtin.template': {'src': str(ROOT / 'roles/system/templates/chrony-nts.conf.j2'),
                                                           'dest': str(config), 'mode': '0600'}}]}]))
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                    env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                lines = config.read_text().splitlines()
                self.assertIn('leapsecmode slew', lines)
                self.assertEqual([line for line in lines if line.startswith('leapseclist ')],
                                 [f'leapseclist {leap}'] if leap and capability else [])
                if binary:
                    parsed = VALIDATOR.validate(config, binary)
                    self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)
                    valid = config.read_text()
                    for invalid in ['invalid-chrony-directive', '-v']:
                        config.write_text(valid + '\n' + invalid + '\n')
                        self.assertNotEqual(VALIDATOR.validate(config, binary).returncode, 0,
                                            'Native parser accepted an invalid directive or option')

    def test_capability_probe_accepts_only_the_expected_unsupported_error(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/chrony.yml').read_text())
        probe = next(task for task in tasks if task['name'] == 'Probe optional chrony leap-second file support')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'chronyd'
            probe['ansible.builtin.command']['argv'][0] = str(executable)
            play = root / 'probe.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                'vars': {'csb_rhel': False, 'system_chrony_service_enabled': True,
                         'is_dnf': True, 'is_apt': False, 'system_is_container': False}, 'tasks': [probe]}]))
            for error, status, accepted in [('', 0, True), ('Invalid directive at line 1', 1, True),
                                            ('Could not open /dev/null: Permission denied', 1, False)]:
                with self.subTest(error=error):
                    executable.write_text('#!/bin/sh\nprintf "%s\\n" "' + error + '" >&2\nexit ' + str(status) + '\n')
                    executable.chmod(0o755)
                    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                        env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0 if accepted else 2, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
