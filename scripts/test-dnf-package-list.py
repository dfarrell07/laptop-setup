#!/usr/bin/env python3
"""The dnf package list per distribution (RHEL 9 leaves out EPEL-only packages) and the CSB per-package retry's
report of what still failed, run through the real tasks of roles/packages/tasks/install_dnf_packages.yml."""
import json
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
TEST_TIMEOUT = max(120, int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0')))
TASKS = yaml.safe_load((ROOT / 'roles/packages/tasks/install_dnf_packages.yml').read_text())
BLOCK = next(task for task in TASKS if task.get('name') == 'Install dnf packages')
DEFAULTS = yaml.safe_load((ROOT / 'roles/packages/defaults/main.yml').read_text())


def task(section, name):
    return next(item for item in BLOCK[section] if item.get('name') == name)


class DnfPackageListTests(unittest.TestCase):
    def run_tasks(self, tasks, variables):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out.json'
            tasks = tasks + [{'name': 'Save', 'ansible.builtin.copy': {
                'content': '{{ {"all": _packages_dnf_all | default(none), '
                           '"unavailable": _packages_dnf_unavailable | default(none)} | to_json }}',
                'dest': str(out), 'mode': '0600'}}]
            play = Path(directory) / 'play.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'vars': variables,
                                             'tasks': tasks}]))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                                    env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                    capture_output=True, text=True, timeout=TEST_TIMEOUT)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(out.read_text())

    def package_list(self, distribution, major):
        empty = ['packages_languages', 'packages_languages_work', 'packages_security', 'packages_security_yubikey',
                 'packages_security_rhel', 'packages_containers', 'packages_containers_work', 'packages_containers_kubectl',
                 'packages_networking', 'packages_networking_work', 'packages_support_work', 'packages_aws_cli_fedora_work',
                 'packages_extra_dnf', 'packages_fedora']
        variables = {name: [] for name in empty}
        variables.update(packages_cli=['git', 'zsh', 'pipx', 'jq'], repo_gh_cli=True, repo_tailscale=True,
                         profile='personal', packages_yubikey_enabled=False, packages_install_binaries=False,
                         packages_bpfman_enabled=False, _packages_linux_x86_work=False, ansible_distribution=distribution,
                         ansible_facts={'distribution_major_version': str(major), 'architecture': 'x86_64'},
                         packages_dnf_epel_only_el9=DEFAULTS['packages_dnf_epel_only_el9'])
        tasks = [task('block', 'Build combined dnf package list'),
                 task('block', 'Leave out packages RHEL 9 ships only in EPEL')]
        return self.run_tasks(tasks, variables)['all']

    def test_rhel9_leaves_out_epel_only_packages(self):
        self.assertEqual(self.package_list('Rocky', 9), ['git', 'zsh', 'jq'])
        self.assertEqual(self.package_list('RedHat', 9), ['git', 'zsh', 'jq'])

    def test_rhel10_and_fedora_keep_them(self):
        self.assertEqual(self.package_list('Rocky', 10), ['git', 'zsh', 'pipx', 'jq'])
        self.assertEqual(self.package_list('Fedora', 44), ['git', 'zsh', 'pipx', 'jq'])

    def test_per_package_retry_reports_only_the_packages_that_still_fail(self):
        results = {'results': [{'item': 'git', 'rc': 0}, {'item': 'nosuch', 'rc': 1, 'failures': ['No package nosuch']},
                               {'item': 'zsh', 'rc': 0}, {'item': 'other', 'rc': 1}]}
        skipped = {'results': [{'item': 'git', 'skipped': True}, {'item': 'nosuch', 'skipped': True}]}
        out = self.run_tasks([task('rescue', 'List the dnf packages that still failed (CSB)')],
                             {'csb_rhel': True, '_packages_dnf_each_host': results,
                              '_packages_dnf_each_container': skipped})
        self.assertEqual(out['unavailable'], ['nosuch', 'other'])

    def test_retry_runs_only_on_csb_and_only_when_the_repositories_answer(self):
        def when(name):
            return [str(item) for item in ([task('rescue', name)['when']] if isinstance(task('rescue', name)['when'], str)
                                           else task('rescue', name)['when'])]
        for name in ['Retry each dnf package on its own on the host (CSB)',
                     'Retry each dnf package on its own in the container (CSB)']:
            with self.subTest(name=name):
                self.assertRegex(' '.join(when(name)), r'^csb_rhel and .*_packages_dnf_repos\.rc \| default\(1\) == 0$')
                self.assertIs(task('rescue', name)['failed_when'], False)
        # Nothing is recorded after a retry that installed everything
        self.assertEqual(when('Record dnf package install failure (CSB)'),
                         ['csb_rhel', '_packages_dnf_repos.rc | default(1) != 0 or '
                                      '_packages_dnf_unavailable | default([]) | length > 0'])

if __name__ == '__main__':
    unittest.main()
