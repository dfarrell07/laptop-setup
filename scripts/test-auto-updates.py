#!/usr/bin/env python3
"""Automatic updates: dnf-automatic config and CSB guard, Flatpak timer, the user updater and the bar's UPD field."""
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


def run_play(directory, tasks, variables, *args):
    play = [{'hosts': 'localhost', 'gather_facts': False, 'vars': variables, 'tasks': tasks}]
    (directory / 'play.yml').write_text(yaml.safe_dump(play))
    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', *args, str(directory / 'play.yml')], cwd=ROOT,
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
                         'is_linux': True, 'is_macos': False,
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
            self.assertFalse((home / 'Library').exists())  # no launchd agent on Linux

    def test_user_update_launchd_agent_on_macos(self):
        import plistlib
        tasks = yaml.safe_load((ROOT / 'roles/packages/tasks/user_updates.yml').read_text())

        def fix_src(task):
            module = task.get('ansible.builtin.copy') or task.get('ansible.builtin.template')
            if module:
                module['src'] = str(ROOT / 'roles/packages' / ('files' if 'ansible.builtin.copy' in task else 'templates')
                                    / module['src'])
            for inner in task.get('block', []):
                fix_src(inner)
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            stubs = home / 'stubs'
            stubs.mkdir()

            def stub_launchctl(task):  # Apple's launchctl by full path; run the stub instead
                argv = task.get('ansible.builtin.command', {}).get('argv')
                if argv:
                    self.assertEqual(argv[0], '/bin/launchctl')
                    argv[0] = str(stubs / 'launchctl')
                for inner in task.get('block', []):
                    stub_launchctl(inner)
            for task in tasks:
                fix_src(task)
                stub_launchctl(task)
            log = home / 'launchctl.log'
            # gui domain present; a job is loaded once bootstrapped; the first two bootstraps fail as launchd's
            # teardown race does ("Bootstrap failed: 5")
            stub(stubs, 'launchctl', f'echo "$*" >> {log}\ncase "$1 $2" in\n'
                 '  "print gui/501") exit 0 ;;\n'
                 f'  "print gui/501/com.laptop-setup.user-update") [ -f {home}/loaded ] ;;\n'
                 f'  "bootstrap gui/501") echo x >> {home}/tries; [ "$(wc -l < {home}/tries)" -gt 2 ] || exit 5\n'
                 f'    touch {home}/loaded ;;\n'
                 f'  "bootout gui/501/com.laptop-setup.user-update") rm {home}/loaded ;;\n'
                 '  *) exit 1 ;;\nesac\n')
            # role_path for the shared launchd helper's include; the stub stands in for its /bin/launchctl too
            variables = {'ansible_facts': {'user_dir': name, 'user_uid': '501'}, 'ansible_managed': 'test',
                         'role_path': str(ROOT / 'roles/packages'), '_launchd_agent_launchctl': str(stubs / 'launchctl'),
                         'is_linux': False, 'is_macos': True, 'dotfiles_homebrew_prefix': '/opt/homebrew',
                         'packages_user_updates_calendar': 'daily', 'packages_user_updates_container': 'fedora-dev',
                         'packages_user_updates_pipx_skip': ['yamllint', 'ansible-lint'],
                         'packages_user_updates_npm': ['@openai/codex']}
            first = run_play(home, tasks, variables)
            self.assertNotIn('would be bootstrapped', first)  # the check-mode report stays out of a real run
            bootstrap = f'bootstrap gui/501 {name}/Library/LaunchAgents/com.laptop-setup.user-update.plist'
            self.assertEqual(log.read_text().splitlines()[-3:], [bootstrap] * 3)  # retried until it loaded
            log.unlink()
            again = run_play(home, tasks, variables)
            self.assertNotIn('bootstrap', log.read_text())  # loaded and unchanged: left alone
            self.assertRegex(again, r'changed=0 ')
            (home / 'loaded').unlink()  # booted out by hand: the plan reports the load, and only looks
            log.unlink()
            plan = run_play(home, tasks, variables, '--check')
            self.assertRegex(plan, r'changed=1 ')
            self.assertIn('would be bootstrapped', plan)
            self.assertEqual({l.split()[0] for l in log.read_text().splitlines()}, {'print'})
            (home / 'loaded').touch()
            run_play(home, tasks, {**variables, 'packages_user_updates_npm': ['@openai/codex', 'other']})
            self.assertEqual([l.split()[0] for l in log.read_text().splitlines()][-2:], ['bootout', 'bootstrap'])
            self.assertFalse((home / '.config/systemd').exists())  # no systemd units on macOS
            script = home / '.local/bin/laptop-user-update'
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)
            agent = plistlib.loads((home / 'Library/LaunchAgents/com.laptop-setup.user-update.plist').read_bytes())
            # the wrapper, then the log and the script with no container argument (no distrobox on macOS)
            wrapper, logfile = agent['ProgramArguments'][:3], name + '/.local/state/log/laptop-user-update.log'
            self.assertEqual(agent['ProgramArguments'][3:], ['laptop-user-update', logfile, str(script)])
            self.assertEqual(wrapper[:2], ['/bin/sh', '-c'])
            self.assertNotIn('StandardOutPath', agent)  # the wrapper's own redirection: the log is replaced when trimmed
            # the wrapper keeps the last 2000 lines, appends the run's output and returns its exit code
            fake = home / 'fake-update'
            fake.write_text('#!/bin/sh\necho run; echo oops >&2; exit 3\n')
            fake.chmod(0o755)
            trimmed = home / 'trim.log'
            for lines, kept in [(2500, [f'line {n}' for n in range(501, 2501)]), (3, ['line 1', 'line 2', 'line 3']),
                                (None, [])]:
                with self.subTest(lines=lines):
                    trimmed.unlink(missing_ok=True)
                    if lines:
                        trimmed.write_text(''.join(f'line {n}\n' for n in range(1, lines + 1)))
                    result = subprocess.run(wrapper + ['laptop-user-update', str(trimmed), str(fake)], capture_output=True,
                                            text=True, env={'PATH': '/usr/bin:/bin'})
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (3, '', ''))
                    self.assertEqual(trimmed.read_text().splitlines(), kept + ['run', 'oops'])
                    self.assertFalse(Path(str(trimmed) + '.tmp').exists())
            self.assertEqual(agent['StartCalendarInterval'], {'Hour': 0, 'Minute': 0})
            self.assertEqual(agent['EnvironmentVariables'], {
                'LAPTOP_UPDATE_PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'LAPTOP_UPDATE_NPM': '@openai/codex other',
                'LAPTOP_UPDATE_PIPX_SKIP': 'yamllint ansible-lint'})
            self.assertTrue((home / '.local/state/log').is_dir())

    def test_launchd_job_waits_for_a_graphical_login(self):
        tasks = yaml.safe_load((ROOT / 'roles/common/tasks/launchd-agent.yml').read_text())
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            log = home / 'launchctl.log'
            # gui/501 exists only when the file gui does; the job is loaded once bootstrapped
            stub(home, 'launchctl', f'echo "$*" >> {log}\ncase "$1 $2" in\n'
                 f'  "print gui/501") [ -f {home}/gui ] ;;\n'
                 f'  "print gui/501/job") [ -f {home}/loaded ] ;;\n'
                 f'  "bootstrap gui/501") touch {home}/loaded ;;\n'
                 '  *) exit 1 ;;\nesac\n')
            variables = {'ansible_facts': {'user_uid': '501'}, '_launchd_agent_launchctl': str(home / 'launchctl'),
                         '_launchd_agent_label': 'job', '_launchd_agent_plist': f'{name}/job.plist',
                         '_launchd_agent_changed': False}
            ssh = run_play(home, tasks, variables)  # over SSH alone: left for launchd to load at the next login
            self.assertIn('No graphical login: job loads at the next one', ssh)
            self.assertRegex(ssh, r'changed=0 ')
            self.assertEqual(log.read_text().splitlines(), ['print gui/501'])
            (home / 'gui').touch()  # with a graphical login it loads there
            run_play(home, tasks, variables)
            self.assertEqual(log.read_text().splitlines()[-1], f'bootstrap gui/501 {name}/job.plist')
            self.assertRegex(run_play(home, tasks, variables), r'changed=0 ')

    def run_updater(self, home, npm_rc=0, container_exists=True, outdated_fails=False, container='fedora-dev'):
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
             f'  echo "{home}/.npm-global/lib/node_modules/tag:tag@0.2.0-alpha.2:tag@0.2.0:tag@0.2.0-alpha.2:global"\n'
             '  exit 1\nfi\n'
             f'exit {npm_rc}')
        stub(bin_dir, 'podman', f'echo "podman $*" >> {log}; ' + (
            'case "$*" in *ExecIDs*) echo 0;; *State.Running*) echo false;; esac; exit 0'
            if container_exists else 'exit 1'))
        result = subprocess.run(['bash', str(ROOT / 'roles/packages/files/laptop-user-update.sh')] + ([container] if container else []),
                                env={'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LAPTOP_UPDATE_PATH': f'{bin_dir}:/usr/bin:/bin',
                                     'LAPTOP_UPDATE_PIPX_SKIP': 'yamllint ansible-lint',
                                     'LAPTOP_UPDATE_NPM': '@openai/codex ms rc tag'},
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
            self.assertNotIn('tag@', installed)  # a mis-tagged prerelease latest never replaces a release
            self.assertIn('podman stop --time 10 fedora-dev', calls)  # the stopped container is stopped again
            self.assertIn('pipx upgrade-all --skip yamllint ansible-lint', calls)
            self.assertIn('distrobox upgrade fedora-dev', calls)
        with tempfile.TemporaryDirectory() as name:
            result, calls = self.run_updater(Path(name), npm_rc=3, container_exists=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn('== npm: FAILED (exit 3)', result.stdout)
            self.assertIn('pipx upgrade-all', calls)  # later steps still run
            self.assertNotIn('distrobox upgrade', calls)
        with tempfile.TemporaryDirectory() as name:  # macOS: the launchd agent passes no container
            result, calls = self.run_updater(Path(name), container='')
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn('pipx upgrade-all --skip yamllint ansible-lint', calls)
            self.assertNotIn('podman', calls)
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
        disabled = tempfile.NamedTemporaryFile('w', suffix='.yml', delete=False)
        disabled.write('packages_user_updates_enabled: false\n')
        disabled.close()
        self.addCleanup(os.unlink, disabled.name)
        for state, body, cfg, expected in [('failed', 'return 0', '/dev/null', 'user-updates FAIL'),
                                           ('enabled', '[ "$2" = is-enabled ]', '/dev/null', 'user-updates PASS'),
                                           ('off', 'return 1', '/dev/null', 'user-updates WARN'),
                                           ('disabled-clean', 'return 1', disabled.name, 'user-updates PASS'),
                                           ('disabled-left', '[ "$2" = is-enabled ]', disabled.name, 'user-updates WARN')]:
            with self.subTest(state=state):
                run = subprocess.run(['bash', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; }; IS_LINUX=true; CONTAINER=; '
                                      f'_user_updates_off={"false" if cfg == "/dev/null" else "true"}; '
                                      f'systemctl() {{ ' + body + '; };\n' + source[start:end]],
                                     capture_output=True, text=True)
                self.assertEqual(run.stdout.strip(), expected, run.stderr)

    def test_smoke_user_updates_check_on_macos(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _uu_label=com.laptop-setup.user-update')
        end = source.index('  unset _uu_label _uu_job', start)
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            stubs = home / 'stubs'
            stubs.mkdir()
            # launchctl print: only the user domain (SSH session) holds the job, with $EXIT as its last exit code
            (stubs / 'id').symlink_to(shutil.which('id'))
            stub(stubs, 'launchctl', 'case "$2" in user/*) [ -n "$EXIT" ] || exit 113; '
                 'printf "\\tstate = not running\\n\\tlast exit code = %s\\n" "$EXIT" ;; *) exit 113 ;; esac')
            (home / 'Library/LaunchAgents').mkdir(parents=True)
            for off, exit_code, agent, expected in [
                    ('false', '(never exited)', False, 'user-updates PASS'), ('false', '0', False, 'user-updates PASS'),
                    ('false', '1', False, 'user-updates FAIL'), ('false', '', False, 'user-updates WARN'),
                    ('true', '', False, 'user-updates PASS'), ('true', '', True, 'user-updates WARN')]:
                with self.subTest(off=off, exit_code=exit_code, agent=agent):
                    plist = home / 'Library/LaunchAgents/com.laptop-setup.user-update.plist'
                    plist.write_text('') if agent else plist.unlink(missing_ok=True)
                    run = subprocess.run([shutil.which('bash'), '-eu', '-o', 'pipefail', '-c',
                                          'record() { printf "%s %s\\n" "$1" "$2"; };'
                                          f' _user_updates_off={off};\n' + source[start:end]],
                                         env=dict(os.environ, HOME=name, EXIT=exit_code, PATH=str(stubs)),
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

    def test_bar_shows_every_configured_vpn_with_its_own_state(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        extra = [{'name': 'Mullvad', 'command': ['mullvad', 'status'], 'connected': '^Connected'}]  # not installed here
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            script = render(directory, 'roles/desktop/templates/i3status-bar.sh.j2',
                            {**defaults, 'csb_detected': True, 'desktop_i3status_vpns_extra': extra,
                             'ansible_managed': 'test'}, 'bar-status')
            stubs = directory / 'stubs'
            stubs.mkdir()
            # two updates, so the second one comes from the 15 s cache: the VPN commands run once
            stub(stubs, 'i3status', 'printf \'{"version":1}\\n[\\n[{"name":"wireless","full_text":"W"}]\\n'
                 ',[{"name":"wireless","full_text":"W"}]\\n\'')
            stub(stubs, 'netbird', f'echo run >> {directory}/netbird.calls; '
                 'printf "OS: linux\\nManagement: Connected\\nSignal: Connected\\n"')
            stub(stubs, 'tailscale', 'printf \'{"BackendState": "Stopped"}\\n\'; exit 1')  # down exits non-zero
            stub(stubs, 'nmcli', 'echo "corp-wg:wireguard"')
            for quiet in ['systemctl', 'rpm', 'podman']:
                stub(stubs, quiet, 'exit 1')
            (directory / 'bar-status').write_text(script)
            result = subprocess.run(['bash', str(directory / 'bar-status')], capture_output=True, text=True,
                                    timeout=30, env=dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin', HOME=name))
            lines = result.stdout.splitlines()
            for line in lines[-2:]:
                blocks = json.loads(line.lstrip(','))
                vpn = [(b['full_text'], b.get('color')) for b in blocks if b['name'].startswith('vpn')]
                self.assertEqual(vpn, [('NetBird up', '#00FF00'), ('Tailscale down', None),
                                       ('Mullvad missing', '#FFFF00'), ('VPN corp-wg', '#00FF00')], result.stderr)
                self.assertEqual(blocks[-1]['name'], 'wireless')  # VPN blocks go before the Wi-Fi field
            self.assertEqual((directory / 'netbird.calls').read_text().count('run'), 1)

    def test_bar_vpn_assert_refuses_entries_that_would_break_the_bar(self):
        tasks = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        found = [t for block in tasks for t in block.get('block', [block]) if t.get('name') == 'Validate desktop_i3status_vpns']
        self.assertEqual(len(found), 1)
        check = {k: v for k, v in found[0].items() if k != 'when'}
        good = {'name': 'Mullvad', 'command': ['mullvad', 'status'], 'connected': '^Connected'}
        cases = [(good, True), ({**good, 'name': 'Mullvad\n'}, False), ({**good, 'connected': '^Connected\n'}, False),
                 ({**good, 'name': 'a"b'}, False), ({**good, 'command': 'mullvad status'}, False),
                 ({**good, 'command': []}, False), ({**good, 'connected': ''}, False)]
        for entry, ok in cases:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as name:
                if ok:
                    run_play(Path(name), [check], {'desktop_i3status_vpns': [entry]})
                else:
                    with self.assertRaises(AssertionError):
                        run_play(Path(name), [check], {'desktop_i3status_vpns': [entry]})

    def test_bar_vpn_list_has_netbird_only_on_csb_machines(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        for csb, names in [(False, 'Tailscale'), (True, 'NetBird Tailscale')]:
            with self.subTest(csb=csb), tempfile.TemporaryDirectory() as name:
                script = render(Path(name), 'roles/desktop/templates/i3status-bar.sh.j2',
                                {**defaults, 'csb_detected': csb, 'ansible_managed': 'test'}, 'bar-status')
                line = next(l for l in script.splitlines() if l.startswith('vpn_names='))
                self.assertEqual(line, f'vpn_names=({names} )')

    def test_aide_rebaselines_only_after_an_rpm_transaction(self):
        tasks = yaml.safe_load((ROOT / 'roles/system/tasks/dnf_automatic.yml').read_text())
        hook = next(t for t in tasks if t.get('name') == 'Deploy AIDE update hook for dnf-automatic')
        lines = hook['ansible.builtin.copy']['content'].splitlines()
        post = next(l for l in lines if l.startswith('ExecStopPost=-'))[len('ExecStopPost=-'):]
        self.assertIn('ExecStartPre=-/usr/bin/touch /run/dnf-automatic-aide.stamp', lines)
        self.assertIn('After=aide-rebaseline.service', lines)
        self.assertIn('Wants=aide-rebaseline.service', lines)
        self.assertFalse([l for l in lines if l.startswith('TimeoutStopSec')])  # the scan is not in the stop phase
        self.assertNotRegex(post.replace('$$', ''), r'\$')  # systemd would expand a single $
        for touched, expect in [(None, False), ('rpmdb.sqlite-shm', False), ('rpmdb.sqlite', True),
                                ('rpmdb.sqlite-wal', True)]:
            with self.subTest(touched=touched), tempfile.TemporaryDirectory() as name:
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
                cmd = (post.replace('$$', '$').replace('/usr/lib/sysimage/rpm /var/lib/rpm', str(d / 'rpm'))
                       .replace('/run/dnf-automatic-aide.stamp', str(stamp)).replace('/var/lib/aide', str(d))
                       .replace('systemctl start --no-block aide-rebaseline.service', f'touch {d}/started')
                       .replace('/usr/bin/bash', 'bash'))
                subprocess.run(cmd, shell=True, check=False)
                self.assertEqual((d / 'rebaseline-pending').exists(), expect)
                self.assertEqual((d / 'started').exists(), expect)

    def test_aide_rebaseline_service_installs_only_a_clean_update(self):
        unit = (ROOT / 'roles/system/files/aide-rebaseline.service').read_text().splitlines()
        self.assertIn('ConditionPathExists=|/var/lib/aide/rebaseline-pending', unit)
        self.assertIn('ConditionPathExists=|/var/lib/aide/rebaseline-pending.run', unit)  # an interrupted run resumes
        self.assertFalse([l for l in unit if l.startswith('WantedBy')])  # static: pulled in, never holds boot
        check = (ROOT / 'roles/system/files/aide-check.service').read_text().splitlines()
        self.assertIn('Wants=aide-rebaseline.service', check)  # a pending rebaseline runs before every check
        start = next(l for l in unit if l.startswith('ExecStart='))[len('ExecStart='):]
        self.assertIn('ConditionPathExists=/var/lib/aide/aide.db.gz', unit)  # no baseline yet: skip, don't fail
        self.assertIn('After=aide-rebaseline.service', check)
        self.assertIn('After=dnf5-automatic.service dnf-automatic.service', check)
        for aide_rc, during, marker, ok, pending in [
                (0, False, 'rebaseline-pending', True, False), (4, False, 'rebaseline-pending', True, False),
                (9, False, 'rebaseline-pending', False, True),
                (0, True, 'rebaseline-pending', True, True),  # a dnf run during the scan leaves a new marker
                (0, False, 'rebaseline-pending.run', True, False),  # a run that shutdown interrupted resumes
                (0, 'dnf', 'rebaseline-pending', False, True),  # skipped while a dnf transaction runs
                (0, 'dnf-ending', 'rebaseline-pending', True, False)]:  # runs when dnf's ExecStopPost starts it
            with self.subTest(aide_rc=aide_rc, during=during, marker=marker), tempfile.TemporaryDirectory() as name:
                d = Path(name)
                (d / marker).touch()
                again = f'touch {d}/rebaseline-pending; ' if during is True else ''
                state = {'dnf': 'activating', 'dnf-ending': 'deactivating'}.get(during, 'inactive')
                cmd = (start.replace('$$', '$').replace('/var/lib/aide', str(d)).replace('/usr/bin/bash', 'bash')
                       .replace('systemctl show -P ActiveState dnf5-automatic.service dnf-automatic.service',
                                f'printf "inactive\\n{state}\\n"')
                       .replace('aide --update', f'{again}touch {d}/aide.db.new.gz; (exit {aide_rc})'))
                rc = subprocess.run(cmd, shell=True, check=False).returncode
                self.assertEqual(rc == 0, ok or during == 'dnf')  # skipping is not a failure
                self.assertEqual((d / 'aide.db.gz').exists(), ok)
                self.assertFalse((d / 'aide.db.new.gz').exists())
                self.assertFalse((d / 'rebaseline-pending.run').exists())
                self.assertEqual((d / 'rebaseline-pending').exists(), pending)

if __name__ == '__main__':
    unittest.main()
