#!/usr/bin/env python3
"""Check real Git reads, seeded files and personal-profile JSON without provisioning."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil

ROOT = Path(__file__).resolve().parent.parent


class SmokeTests(unittest.TestCase):
    def test_home_tmp_probe_uses_a_binary_available_on_macos_and_linux(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('if [[ ! -d "$HOME/tmp" ]]')
        end = source.index('\nfi', start) + len('\nfi')
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'tmp').mkdir()
            tools = home / 'tools'
            tools.mkdir()
            copier = tools / 'cp'
            copier.write_text('#!/bin/sh\n[ "$1" = /bin/true ] && exit 1\nexec /usr/bin/cp "$@"\n')
            copier.chmod(0o755)
            result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                     'record() { printf "%s %s\\n" "$1" "$2"; };\n' + source[start:end]],
                                    env=dict(os.environ, HOME=str(home), PATH=str(tools) + ':' + os.environ['PATH']),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'home-tmp-dir PASS')
            self.assertEqual(list((home / 'tmp').iterdir()), [])

    def test_intel_thermald_states_do_not_abort_diagnostics(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('    _thermald_state=')
        end = source.index('    unset _thermald_state', start) + len('    unset _thermald_state')
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'systemctl'
            executable.write_text('#!/bin/sh\nprintf "%s\\n" "$THERMAL_STATE"\nexit "$THERMAL_STATUS"\n')
            executable.chmod(0o755)
            for state, status, expected in [('not-found', '4', 'WARN'), ('masked', '1', 'WARN'),
                                            ('disabled', '1', 'PASS'), ('enabled', '0', 'PASS')]:
                with self.subTest(state=state):
                    result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                             'record() { printf "%s %s\\n" "$1" "$2"; };\n' + source[start:end]],
                                            env=dict(os.environ, PATH=directory + ':' + os.environ['PATH'],
                                                     THERMAL_STATE=state, THERMAL_STATUS=status),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'thermald-unmasked-on-intel ' + expected)

    def test_chrony_query_failures_are_recorded_and_authenticated_sources_pass(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  if ! $CSB_HOST && command -v chronyc')
        end = source.index('  _chrony_svc=', start)
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'chronyc'
            executable.write_text('#!/bin/sh\n[ "$CHRONY_QUERY_FAIL" = 1 ] && exit 4\n'
                                  'case "$*" in\n  "-c sources") echo "^,*,time.example";;\n'
                                  '  "-c authdata") echo "time.example,NTS,1,2,3";;\nesac\n')
            executable.chmod(0o755)
            for fails, expected in [('0', 'PASS'), ('1', 'FAIL')]:
                result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                         'record() { printf "%s %s\\n" "$1" "$2"; }; CSB_HOST=false;\n' + source[start:end]],
                                        env=dict(os.environ, PATH=directory + ':' + os.environ['PATH'], CHRONY_QUERY_FAIL=fails),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'chrony-nts-active ' + expected)

    def test_pam_record_parsing_preserves_posix_classes_and_keys(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = {'flk': 'deny=5\nunlock_time=900\nlocal_users_only\n',
                      'pwq': 'minlen=14\ndcredit=-1\nucredit=-1\nlcredit=-1\nocredit=-1\ndifok=4\nmaxrepeat=3\nenforce_for_root\n',
                      'pwh': 'remember=24\nenforce_for_root\n'}
            files = {'flk': 'faillock.conf', 'pwq': 'pwquality.conf', 'pwh': 'pwhistory.conf'}
            for suffix, content in values.items():
                fixture = root / files[suffix]
                fixture.write_text(content)
                start = source.index('  for _' + suffix + ' in \\\n')
                end = source.index('\n  done', start) + len('\n  done')
                loop = source[start:end].replace('/etc/security/' + files[suffix], str(fixture))
                for valid in [True, False]:
                    with self.subTest(suffix=suffix, valid=valid):
                        fixture.write_text(content if valid else '')
                        result = subprocess.run(['bash', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; };\n'
                            '_skip_faillock=false; _skip_pwquality=false; _skip_pwhistory=false;\n' + loop],
                            capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        rows = [line.split() for line in result.stdout.splitlines()]
                        self.assertTrue(rows)
                        self.assertTrue(all(key.startswith({'flk': 'faillock-', 'pwq': 'pwquality-', 'pwh': 'pwhistory-'}[suffix])
                                            and status == ('PASS' if valid else 'FAIL') for key, status in rows), rows)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux system checks')
    def test_system_only_keeps_json_clean_and_excludes_user_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            executable = home / 'authselect'
            executable.write_text('#!/bin/sh\necho "Current configuration is valid."\nexit 0\n')
            executable.chmod(0o755)
            chronyc = home / 'chronyc'
            chronyc.write_text('#!/bin/sh\nexit 4\n')
            chronyc.chmod(0o755)
            result = subprocess.run(
                ['bash', '-E', '-c',
                 'trap \'printf "smoke command failed at line %s\\n" "$LINENO" >&2\' ERR; source "$1" "${@:2}"',
                 'smoke-fixture', str(ROOT / 'scripts/smoke-test.sh'), '--json', '--system-only', '--profile', 'personal'],
                env=dict(os.environ, HOME=str(home), PATH=str(home) + ':' + os.environ['PATH']),
                capture_output=True, text=True, timeout=60)
            self.assertIn(result.returncode, [0, 1], result.stderr)
            data = json.loads(result.stdout)
            names = {item['name'] for item in data['results']}
            self.assertIn('sysctl-ptrace-scope', names)
            self.assertNotIn('ssh-dir-perms', names)
            self.assertNotIn('home-dir-perms', names)
            self.assertEqual(next(item['status'] for item in data['results'] if item['name'] == 'chrony-nts-active'), 'FAIL')
            self.assertEqual(data['failures'], sum(item['status'] == 'FAIL' for item in data['results']))
            invalid = subprocess.run(
                ['bash', str(ROOT / 'scripts/smoke-test.sh'), '--system-only', '--user-only'],
                capture_output=True, text=True)
            self.assertEqual(invalid.returncode, 2)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux environment.d checks')
    def test_environment_export_opt_in_and_exact_managed_values(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            scripts = home / 'scripts'
            scripts.mkdir()
            shutil.copyfile(ROOT / 'scripts/smoke-test.sh', scripts / 'smoke-test.sh')
            binaries = home / 'bin'
            binaries.mkdir()
            for name in ['ssh', 'systemd-detect-virt']:
                executable = binaries / name
                executable.write_text('#!/bin/sh\nexit 1\n')
                executable.chmod(0o755)
            export = home / '.config/environment.d/containers.conf'
            export.parent.mkdir(parents=True)
            for enabled, content, expected in [
                (False, None, 'SKIP'), (True, None, 'FAIL'),
                (True, 'DOCKER_HOST=unix://${XDG_RUNTIME_DIR}/podman/podman.sock\nKIND_EXPERIMENTAL_PROVIDER=podman\n', 'PASS'),
                (True, 'NOT_DOCKER_HOST=remote\nNOT_KIND_EXPERIMENTAL_PROVIDER=podman\n', 'FAIL'),
            ]:
                with self.subTest(enabled=enabled, content=content):
                    (home / 'config.yml').write_text('containers_environment_d_enabled: ' + str(enabled).lower() + '\n')
                    if content is None:
                        export.unlink(missing_ok=True)
                    else:
                        export.write_text(content)
                    result = subprocess.run(
                        ['bash', str(scripts / 'smoke-test.sh'), '--json', '--user-only', '--profile', 'personal'],
                        env=dict(os.environ, HOME=str(home), PATH=str(binaries) + ':' + os.environ['PATH'],
                                 MOLECULE_PROJECT_DIRECTORY=str(ROOT)),
                        capture_output=True, text=True, timeout=60)
                    data = json.loads(result.stdout)
                    checks = {item['name']: item['status'] for item in data['results']}
                    self.assertEqual(checks['env-d-containers'], expected, data)

    def test_git_reads_seed_markers_profile_and_container_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            binaries = home / 'bin'
            binaries.mkdir()
            for name, body in {
                'ssh': 'exit 255',
                'gcloud': 'echo "Google Cloud SDK 1.0.0"',
                'distrobox': 'shift 3\nexec "$@"',
            }.items():
                executable = binaries / name
                executable.write_text('#!/bin/sh\n' + body + '\n')
                executable.chmod(0o755)
            config = home / '.config/git/config'
            config.parent.mkdir(parents=True)
            config.write_text('# Ansible managed\n[core]\n fsmonitor = false\n hooksPath = ~/.config/git/template/hooks\n[safe]\n bareRepository = explicit\n')
            (home / '.vimrc').write_text('" Seeded by Ansible — safe to customize\n')
            ripgrep = home / '.config/ripgrep/config'
            ripgrep.parent.mkdir(parents=True)
            ripgrep.write_text('# Seeded by Ansible — safe to customize\n')
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                       PATH=str(binaries) + ':' + os.environ['PATH'],
                       GIT_CONFIG_NOSYSTEM='1', MOLECULE_PROJECT_DIRECTORY=str(ROOT))
            for name in list(env):
                if name.startswith('GIT_CONFIG_') and name != 'GIT_CONFIG_NOSYSTEM':
                    del env[name]
            for profile, mode in [('personal', 0o700), ('work', 0o550), ('personal', 0o770), ('personal', 0o755)]:
                with self.subTest(profile=profile, mode=oct(mode)):
                    home.chmod(mode)
                    result = subprocess.run(
                        ['bash', str(ROOT / 'scripts/smoke-test.sh'), '--json', '--user-only',
                         '--profile', profile, '--container', 'isolated-test'],
                        cwd=home, env=env, capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)  # incomplete fixture must fail
                    data = json.loads(result.stdout)
                    checks = {item['name']: item['status'] for item in data['results']}
                    for name in ['git-core.fsmonitor', 'git-safe.bareRepository', 'dotfile-.vimrc', 'dotfile-ripgreprc', 'git-hooks-path']:
                        self.assertEqual(checks[name], 'PASS', data)
                    for name in ['env-d-containers', 'env-d-ssh-agent']:
                        self.assertEqual(checks[name], 'SKIP', data)
                    if profile == 'personal':
                        self.assertEqual(checks['gitconfig-includeif'], 'SKIP', data)
                    self.assertEqual(checks['home-dir-perms'], 'PASS' if mode in [0o700, 0o550] else 'FAIL', data)
                    home.chmod(0o700)
                    self.assertEqual(data['failures'], sum(status == 'FAIL' for status in checks.values()))


if __name__ == '__main__':
    unittest.main()
