#!/usr/bin/env python3
"""The CSB report stays unchanged when a run hits the same failures again (re-plan to changed=0)."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TEST_TIMEOUT = max(120, int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0')))


class CsbReportTests(unittest.TestCase):
    def run_play(self, directory, failures):
        """Record each failure through the real handler, then write the real report; return the play's recap."""
        tasks = [{'name': f'Record {task}', 'ansible.builtin.include_tasks': str(ROOT / 'common/tasks/csb-failure-handler.yml'),
                  'vars': {'_csb_task_name': task, '_csb_role': 'redhat', '_csb_action': 'Ask IT',
                           '_csb_ticket_summary': f'{task} blocked', '_csb_ticket_body': 'Body', '_csb_error': 'No package'}}
                 for task in failures]
        tasks.append({'name': 'Write the report', 'ansible.builtin.include_tasks': str(ROOT / 'common/tasks/csb-report.yml')})
        play = Path(directory) / 'play.yml'
        play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                                         'vars': {'ansible_user_dir': directory}, 'tasks': tasks}]))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                                env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default',
                                         ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                capture_output=True, text=True, timeout=TEST_TIMEOUT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return re.search(r'localhost\s*:\s*ok=\d+\s+changed=(\d+)', result.stdout)

    def changed(self, directory, failures):
        recap = self.run_play(directory, failures)
        self.assertIsNotNone(recap)
        return int(recap.group(1))

    def test_same_failures_leave_the_report_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.changed(directory, ['Install Red Hat cert packages']), 1)
            first = (Path(directory) / 'csb-report.md').read_text()
            self.assertIn('Install Red Hat cert packages', first)
            self.assertEqual(self.changed(directory, ['Install Red Hat cert packages']), 0, 'a re-run must be changed=0')
            self.assertEqual((Path(directory) / 'csb-report.md').read_text(), first)

    def test_different_failures_rewrite_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.changed(directory, ['Install Red Hat cert packages']), 1)
            self.assertEqual(self.changed(directory, ['Install Red Hat cert packages', 'Join the VPN']), 1)
            self.assertIn('Join the VPN', (Path(directory) / 'csb-report.md').read_text())


if __name__ == '__main__':
    unittest.main()
