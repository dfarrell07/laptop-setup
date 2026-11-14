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
        self.assertIn('ExecStartPre=-/usr/bin/nm-online -q -t 120', service)

    def test_user_update_units_deploy_and_validate(self):
        tasks = yaml.safe_load((ROOT / 'roles/packages/tasks/user_updates.yml').read_text())
        for task in tasks:
            module = task.get('ansible.builtin.copy') or task.get('ansible.builtin.template')
            if module:
                module['src'] = str(ROOT / 'roles/packages' / ('files' if 'ansible.builtin.copy' in task else 'templates')
                                    / module['src'])
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            variables = {'ansible_facts': {'user_dir': name, 'user_uid': '4294967294'}, 'ansible_managed': 'test',
                         'packages_user_updates_calendar': 'daily', 'packages_user_updates_container': 'fedora-dev',
                         'packages_user_updates_pipx_skip': ['yamllint', 'ansible-lint'],
                         'packages_user_updates_npm': ['@openai/codex']}
            run_play(home, tasks, variables)
            script = home / '.local/bin/laptop-user-update'
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)
            service = (home / '.config/systemd/user/laptop-user-update.service').read_text()
            self.assertIn('ExecStart=%h/.local/bin/laptop-user-update fedora-dev', service)
            self.assertIn('Environment="LAPTOP_UPDATE_PIPX_SKIP=yamllint ansible-lint"', service)
            self.assertIn('Environment="LAPTOP_UPDATE_NPM=@openai/codex"', service)
            self.assertIn('ExecStartPre=-/usr/bin/nm-online -q -t 120', service)
            self.assertIn('OnCalendar=daily', (home / '.config/systemd/user/laptop-user-update.timer').read_text())
            with self.assertRaisesRegex(AssertionError, 'container name'):
                run_play(home, tasks, {**variables, 'packages_user_updates_container': 'x;rm'})

    def run_updater(self, home, npm_rc=0, container_exists=True, outdated_fails=False):
        bin_dir = home / '.local/bin'
        bin_dir.mkdir(parents=True)
        (home / '.npm-global/lib/node_modules').mkdir(parents=True)
        log = home / 'calls'
        for command, rc in [('pipx', 0), ('distrobox', 0)]:
            stub(bin_dir, command, f'echo "{command} $*" >> {log}; exit {rc}')
        stub(bin_dir, 'npm', f'echo "npm $* user=$NPM_CONFIG_USERCONFIG global=$NPM_CONFIG_GLOBALCONFIG" >> {log}\n'
             + ('if [ "$1" = outdated ]; then echo "registry unreachable" >&2; exit 1; fi\n' if outdated_fails else '')
             + 'if [ "$1" = outdated ]; then\n'
             f'  echo "{home}/.npm-global/lib/node_modules/@openai/codex:@openai/codex@0.170.0:@openai/codex@0.169.0:'
             '@openai/codex@0.170.0:global"\n'
             f'  echo "{home}/.npm-global/lib/node_modules/semver:semver@7.8.6:semver@7.8.6:semver@7.8.6:global"\n'
             f'  echo "{home}/.npm-global/lib/node_modules/inherits:inherits@2.0.4:inherits@2.0.3:inherits@2.0.4:global"\n'
             f'  echo "{home}/.npm-global/lib/node_modules/ms:ms@2.1.3:ms@3.0.0-beta.2:ms@2.1.3:global"\n'
             f'  echo "{home}/.npm-global/lib/node_modules/rc:rc@1.4.0:rc@1.4.0-alpha.2:rc@1.4.0:global"\n'
             '  exit 1\nfi\n'
             f'exit {npm_rc}')
        stub(bin_dir, 'podman', 'exit ' + ('0' if container_exists else '1'))
        result = subprocess.run(['bash', str(ROOT / 'roles/packages/files/laptop-user-update.sh'), 'fedora-dev'],
                                env={'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LAPTOP_UPDATE_PATH': f'{bin_dir}:/usr/bin:/bin',
                                     'LAPTOP_UPDATE_PIPX_SKIP': 'yamllint ansible-lint',
                                     'LAPTOP_UPDATE_NPM': '@openai/codex ms rc'},
                                capture_output=True, text=True,
                                timeout=30)
        return result, (log.read_text() if log.exists() else '')

    def test_user_updater_runs_each_step_and_fails_visibly(self):
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.run_updater(Path(name))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('npm install --global --prefix=' + name + '/.npm-global --registry=https://registry.npmjs.org '
                          '--ignore-scripts --no-audit --no-fund @openai/codex@0.170.0 rc@1.4.0 user=/dev/null global=/', calls)
            global_config = calls.split('global=')[1].split()[0]
            self.assertNotEqual(global_config, '/dev/null')
            self.assertFalse(Path(global_config).exists())  # temp config removed
            installed = calls.split('npm install')[1]
            self.assertNotIn('semver', installed)  # current
            self.assertNotIn('inherits', installed)  # not allowlisted (may come from git or a private registry)
            self.assertNotIn('ms@', installed)  # a newer prerelease is never moved back to latest
            self.assertIn('rc@1.4.0', installed)  # a prerelease moves to its own final release
            self.assertIn('pipx upgrade-all --skip yamllint ansible-lint', calls)
            self.assertIn('distrobox upgrade fedora-dev', calls)
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.run_updater(Path(name), npm_rc=3, container_exists=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn('== npm: FAILED (exit 3)', result.stdout)
            self.assertIn('pipx upgrade-all', calls)  # later steps still run
            self.assertNotIn('distrobox', calls)
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.run_updater(Path(name), outdated_fails=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('== npm: FAILED to check for updates (exit 1): registry unreachable', result.stdout)
            self.assertNotIn('npm install', calls)

    def test_smoke_user_updates_check(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('# Daily user-space updater')
        end = source.index('\nfi\n', source.index('record "user-updates" "WARN"')) + 4
        # systemctl is a shell function, not a PATH stub: a temp dir may be mounted noexec.
        for state, body, expected in [('failed', 'return 0', 'user-updates FAIL'),
                                      ('enabled', '[ "$2" = is-enabled ]', 'user-updates PASS'),
                                      ('off', 'return 1', 'user-updates WARN')]:
            with self.subTest(state=state):
                run = subprocess.run(['bash', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; }; IS_LINUX=true; CONTAINER=; '
                                      '_cfg=/dev/null; systemctl() { ' + body + '; };\n' + source[start:end]],
                                     capture_output=True, text=True)
                self.assertEqual(run.stdout.strip(), expected, run.stderr)

    def test_bar_shows_upd_only_when_an_update_unit_failed(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        for failed in [None, 'user', 'system']:
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                script = render(directory, 'roles/desktop/templates/i3status-bar.sh.j2',
                                {**defaults, 'ansible_managed': 'test'}, 'bar-status')
                stubs = directory / 'stubs'
                stubs.mkdir()
                stub(stubs, 'i3status', 'printf \'{"version":1}\\n[\\n[{"name":"wireless","full_text":"W"}]\\n\'')
                stub(stubs, 'systemctl', {
                    None: 'exit 1',
                    'user': 'if [ "$1 $2 $3" = "--user is-failed --quiet" ] && [ "$4" = laptop-user-update.service ]; '
                            'then exit 0; fi; exit 1',
                    'system': 'if [ "$1 $2" = "is-failed --quiet" ] && echo "$*" | grep -q " dnf5-automatic.service "; '
                              'then exit 0; fi; exit 1'}[failed])
                for quiet in ['nmcli', 'tailscale', 'rpm', 'podman']:
                    stub(stubs, quiet, 'exit 1')
                (directory / 'bar-status').write_text(script)
                result = subprocess.run(['bash', str(directory / 'bar-status')], capture_output=True, text=True,
                                        timeout=30, env=dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin', HOME=name))
                self.assertEqual('"full_text":"UPD"' in result.stdout.splitlines()[-1], failed is not None)

    def test_aide_rebaselines_only_after_an_rpm_transaction(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/dnf_automatic.yml').read_text())
        hook = next(t for t in tasks if t.get('name') == 'Deploy AIDE update hook for dnf-automatic')
        lines = hook['ansible.builtin.copy']['content'].splitlines()
        post = next(l for l in lines if l.startswith('ExecStopPost=-'))[len('ExecStopPost=-'):]
        self.assertIn('ExecStartPre=-/usr/bin/touch /run/dnf-automatic-aide.stamp', lines)
        self.assertIn('TimeoutStopSec=30min', lines)  # stop-phase commands are killed after 45s by default
        self.assertNotRegex(post.replace('$$', ''), r'\$')  # systemd would expand a single $
        for touched, aide_rc, expect in [(None, 0, False), ('rpmdb.sqlite-shm', 0, False), ('rpmdb.sqlite', 0, True),
                                         ('rpmdb.sqlite-wal', 1, True), ('rpmdb.sqlite', 9, False)]:
            with self.subTest(touched=touched, aide_rc=aide_rc), tempfile.TemporaryDirectory() as name:
                d = Path(name)
                (d / 'rpm').mkdir()
                for f in ['rpmdb.sqlite', 'rpmdb.sqlite-shm', 'rpmdb.sqlite-wal']:
                    (d / 'rpm' / f).touch()
                    os.utime(d / 'rpm' / f, (500, 500))
                stamp = d / 'stamp'
                stamp.touch()
                os.utime(stamp, (1000, 1000))
                if touched:
                    os.utime(d / 'rpm' / touched, None)
                bin_dir = d / 'bin'
                bin_dir.mkdir()
                stub(bin_dir, 'aide', f'touch {d}/aide.db.new.gz; exit {aide_rc}')
                cmd = (post.replace('$$', '$').replace('/usr/lib/sysimage/rpm /var/lib/rpm', str(d / 'rpm'))
                       .replace('/run/dnf-automatic-aide.stamp', str(stamp))
                       .replace('/var/lib/aide', str(d)).replace('/usr/bin/bash', 'bash'))
                subprocess.run(cmd, shell=True, check=False, env={'PATH': f'{bin_dir}:/usr/bin:/bin'})
                self.assertEqual((d / 'aide.db.gz').exists(), expect)  # installed only after a clean update
                self.assertFalse((d / 'aide.db.new.gz').exists())  # an errored update's output is discarded

if __name__ == '__main__':
    unittest.main()
