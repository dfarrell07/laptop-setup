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
    def test_leap_directives_and_native_parser(self):
        binary = shutil.which('chronyd') or shutil.which('chronyd', path='/usr/sbin:/sbin')
        for leap in ['/usr/share/zoneinfo/leap-seconds.list', '']:
            with self.subTest(leap=leap), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'chrony.conf'
                play = Path(directory) / 'render.yml'
                play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                    'vars_files': [str(ROOT / 'roles/system/defaults/main.yml')],
                    'vars': {'ansible_managed': 'Ansible managed', 'is_dnf': True, 'is_apt': False},
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
                                 [f'leapseclist {leap}'] if leap else [])
                if binary:
                    parsed = VALIDATOR.validate(config, binary)
                    self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)
                    valid = config.read_text()
                    for invalid in ['invalid-chrony-directive', '-v']:
                        config.write_text(valid + '\n' + invalid + '\n')
                        self.assertNotEqual(VALIDATOR.validate(config, binary).returncode, 0,
                                            'Native parser accepted an invalid directive or option')


if __name__ == '__main__':
    unittest.main()
