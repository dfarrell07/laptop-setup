#!/usr/bin/env python3
"""Exercise real AeroSpace seed/migration tasks without touching the real home."""
import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))


class AeroSpaceTests(unittest.TestCase):
    def test_fresh_repeat_legacy_and_custom_bindings(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        tasks = copy.deepcopy(next(task['block'] for task in production if task['name'] == 'Deploy AeroSpace configuration (macOS)'))
        for task in tasks:
            if 'ansible.builtin.template' in task:
                task['ansible.builtin.template']['src'] = str(ROOT / 'roles/desktop/templates/aerospace.toml.j2')
        for terminal, app in [('ghostty', 'Ghostty'), ('terminal', 'Terminal')]:
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                config = home / '.config/aerospace/aerospace.toml'
                play = home / 'test.yml'
                play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                    'vars_files': [str(ROOT / 'roles/desktop/defaults/main.yml')],
                    'vars': {'ansible_facts': {'user_dir': str(home), 'system': 'Darwin'}},
                    'tasks': [{'ansible.builtin.set_fact': {'desktop_terminal': terminal}}] + tasks}]))
                def run(expected_rc=0):
                    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                        env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
                    return result.stdout
                run()
                expected = f'exec-and-forget open -na {app}'
                self.assertEqual(tomllib.loads(config.read_text())['mode']['main']['binding']['alt-enter'], expected)
                self.assertEqual(re.findall(r'changed=(\d+)', run())[-1], '0')
                config.write_text(config.read_text().replace(expected, 'exec-and-forget open -na Alacritty') + '\n# keep custom settings\n')
                run()
                self.assertIn('# keep custom settings', config.read_text())
                self.assertEqual(tomllib.loads(config.read_text())['mode']['main']['binding']['alt-enter'], expected)
                custom = 'exec-and-forget my-custom-terminal'
                config.write_text(config.read_text().replace(expected, custom))
                self.assertEqual(re.findall(r'changed=(\d+)', run())[-1], '0')
                self.assertEqual(tomllib.loads(config.read_text())['mode']['main']['binding']['alt-enter'], custom)
                before = config.read_text()
                for key, value in [('desktop_macos_terminal_app', 'Ghostty; touch /tmp/unsafe-terminal'),
                                   ('desktop_macos_terminal_path', '/tmp/untrusted.app')]:
                    fixture = yaml.safe_load(play.read_text())
                    fixture[0]['tasks'][0]['ansible.builtin.set_fact'] = {'desktop_terminal': terminal, key: value}
                    play.write_text(yaml.safe_dump(fixture))
                    run(2)
                    self.assertEqual(config.read_text(), before)

    def test_firewall_enabled_and_disabled_output_formats(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        task = next(task for task in production if task['name'] == 'Enable macOS firewall settings')
        condition = task['when'][1]
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        setting = next(item for item in defaults['desktop_macos_firewall_settings'] if item['name'] == 'stealth')
        cases = [('Stealth Mode = 1', False), ('Stealth mode enabled', False),
                 ('Firewall stealth mode is on', False), ('Stealth mode disabled', True),
                 ('Stealth mode not enabled', True), ('Stealth Mode = 0', True)]
        tasks = [{'ansible.builtin.assert': {'that': f'({condition}) == {str(enable).lower()}'},
                  'vars': {'item': setting, 'ansible_loop': {'index0': 0},
                           '_fw_check_results': {'results': [{'stdout': output}]}}} for output, enable in cases]
        with tempfile.TemporaryDirectory() as directory:
            play = Path(directory) / 'test.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks}]))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                                    env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
