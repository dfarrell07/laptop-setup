#!/usr/bin/env python3
"""Automatic updates: dnf-automatic config and CSB guard, Flatpak timer, the user updater and the bar's UPD field."""
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


def run_play(directory, tasks, variables):
    play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables, 'tasks': tasks}]
    (directory / 'play.yml').write_text(yaml.safe_dump(play))
    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(directory / 'play.yml')], cwd=ROOT,
                            env=dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / 'roles'), ANSIBLE_NOCOLOR='1',
                                     ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                            capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout


def render(directory, src, variables, name):
    run_play(directory, [{'name': 'render', 'ansible.builtin.template': {
        'src': str(ROOT / src), 'dest': str(directory / name)}}], variables)
    return (directory / name).read_text()


def stub(directory, command, body):
    path = directory / command
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)


class AutoUpdateTests(unittest.TestCase):
    def test_dnf_automatic_applies_all_updates_and_runs_on_csb_fedora_only_not_rhel_csb(self):
        defaults = yaml.safe_load((ROOT / 'roles/system/defaults/main.yml').read_text())
        self.assertEqual(defaults['system_dnf_automatic_upgrade_type'], 'default')
        with tempfile.TemporaryDirectory() as name:
            conf = render(Path(name), 'roles/system/templates/dnf-automatic.conf.j2',
                          {**defaults, 'proxy_url': '', 'ansible_managed': 'test'}, 'automatic.conf')
        self.assertIn('upgrade_type = default', conf)
        self.assertIn('apply_updates = yes', conf)
        self.assertIn('reboot = never', conf)
        main = yaml.safe_load((ROOT / 'roles/system/tasks/main.yml').read_text())
        task = next(t for t in main if t.get('name') == 'Dnf-automatic')
        self.assertEqual(task['when'], 'not csb_rhel')

    def test_flatpak_update_timer_is_daily_and_noninteractive(self):
        service = (ROOT / 'roles/system/files/flatpak-update.service').read_text()
        timer = (ROOT / 'roles/system/files/flatpak-update.timer').read_text()
        self.assertIn('ExecStart=/usr/bin/flatpak update --system --noninteractive --assumeyes', service)
        self.assertIn('OnCalendar=daily', timer)
        self.assertIn('Persistent=true', timer)

    def test_user_update_units_deploy_and_validate(self):
        tasks = yaml.safe_load((ROOT / 'roles/packages/tasks/user_updates.yml').read_text())
        for task in tasks:
            module = task.get('ansible.builtin.copy') or task.get('ansible.builtin.template')
            if module:
                module['src'] = str(ROOT / 'roles/packages' / ('files' if 'ansible.builtin.copy' in task else 'templates')
                                    / module['src'].replace('{{ item }}', '{{ item }}'))
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            variables = {'ansible_facts': {'user_dir': name, 'user_uid': '4294967294'}, 'ansible_managed': 'test',
                         'packages_user_updates_calendar': 'daily', 'packages_user_updates_container': 'fedora-dev'}
            run_play(home, tasks, variables)
            script = home / '.local/bin/laptop-user-update'
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)
            service = (home / '.config/systemd/user/laptop-user-update.service').read_text()
            self.assertIn('ExecStart=%h/.local/bin/laptop-user-update fedora-dev', service)
            self.assertIn('OnCalendar=daily', (home / '.config/systemd/user/laptop-user-update.timer').read_text())
            with self.assertRaisesRegex(AssertionError, 'container name'):
                run_play(home, tasks, {**variables, 'packages_user_updates_container': 'x;rm'})

    def run_updater(self, home, npm_rc=0, container_exists=True):
        bin_dir = home / '.local/bin'
        bin_dir.mkdir(parents=True)
        (home / '.npm-global/lib/node_modules').mkdir(parents=True)
        log = home / 'calls'
        for command, rc in [('npm', npm_rc), ('pipx', 0), ('distrobox', 0)]:
            stub(bin_dir, command, f'echo "{command} $*" >> {log}; exit {rc}')
        stub(bin_dir, 'podman', 'exit ' + ('0' if container_exists else '1'))
        result = subprocess.run(['bash', str(ROOT / 'roles/packages/files/laptop-user-update.sh'), 'fedora-dev'],
                                env={'HOME': str(home), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True,
                                timeout=30)
        return result, (log.read_text() if log.exists() else '')

    def test_user_updater_runs_each_step_and_fails_visibly(self):
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.run_updater(Path(name))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('npm update --global --prefix=' + name + '/.npm-global --ignore-scripts', calls)
            self.assertIn('pipx upgrade-all', calls)
            self.assertIn('distrobox upgrade fedora-dev', calls)
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.run_updater(Path(name), npm_rc=3, container_exists=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn('== npm: FAILED (exit 3)', result.stdout)
            self.assertIn('pipx upgrade-all', calls)  # later steps still run
            self.assertNotIn('distrobox', calls)

    def test_bar_shows_upd_only_when_an_update_unit_failed(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        for failed in [False, True]:
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                script = render(directory, 'roles/desktop/templates/i3status-bar.sh.j2',
                                {**defaults, 'ansible_managed': 'test'}, 'bar-status')
                stubs = directory / 'stubs'
                stubs.mkdir()
                stub(stubs, 'i3status', 'printf \'{"version":1}\\n[\\n[{"name":"wireless","full_text":"W"}]\\n\'')
                stub(stubs, 'systemctl', 'case "$*" in *laptop-user-update*) exit ' + ('0' if failed else '1')
                     + ';; *) exit 1;; esac')
                for quiet in ['nmcli', 'tailscale', 'rpm', 'podman']:
                    stub(stubs, quiet, 'exit 1')
                (directory / 'bar-status').write_text(script)
                result = subprocess.run(['bash', str(directory / 'bar-status')], capture_output=True, text=True,
                                        timeout=30, env=dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin', HOME=name))
                self.assertEqual('"full_text":"UPD"' in result.stdout.splitlines()[-1], failed)


if __name__ == '__main__':
    unittest.main()
