#!/usr/bin/env python3
"""Check real Git reads, seeded files and personal-profile JSON without provisioning."""
import json
import os
import platform
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import shutil

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which('bash')


def only_tools(bin_dir, *names):
    """Link just these host tools into bin_dir: with PATH=bin_dir a slice sees no other host command (a real docker,
    brew or launchctl in /usr/bin would change its answer)."""
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        (bin_dir / name).symlink_to(shutil.which(name))
    return bin_dir


class SmokeTests(unittest.TestCase):
    def test_native_power_baseline_does_not_require_tlp(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  # TLP is an explicit opt-in;')
        end = source.index('\n  # dconf system policies', start)
        with tempfile.TemporaryDirectory() as directory:
            # A native stack is active and TLP is absent. No hardware/service probes
            # should run at all when the managed TLP choice is disabled.
            executable = Path(directory) / 'systemctl'
            executable.write_text('#!/bin/sh\necho unexpected-power-probe >&2\nexit 1\n')
            executable.chmod(0o755)
            result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                'record() { printf "%s %s\\n" "$1" "$2"; }; _tlp_enabled=false;\n' + source[start:end]],
                env=dict(os.environ, PATH=directory + ':' + os.environ['PATH']),
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'tlp-service SKIP')
            self.assertEqual(result.stderr, '')

    def test_vault_pass_stub_is_inspected_never_executed(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _vp="$SCRIPT_DIR/vault-pass.sh"')
        end = source.index('\n  unset _vp', start)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            script = Path(directory) / 'vault-pass.sh'
            for body, expect in [("pass='CHANGE_ME'\n", 'WARN'), ('echo real\n', 'PASS')]:
                script.write_text('#!/bin/sh\ntouch %s\n%s' % (marker, body))
                script.chmod(0o700)
                result = subprocess.run(['bash', '-eu', '-c',
                    'record() { printf "%s %s\\n" "$1" "$2"; }; SCRIPT_DIR=' + directory + ';\n' + source[start:end]],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith('vault-pass-stub ' + expect), result.stdout)
                self.assertFalse(marker.exists(), 'vault-pass.sh must not be executed')

    def test_aide_monitor_match_needs_an_exact_entry(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        line = next(l for l in source.splitlines() if 'grep -qE "^${_path//./' in l)
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / 'aide.conf'
            for text, expect in [('/etc/sysctl.d NORMAL\n', 0), ('/etc/sysctl.d$ NORMAL\n', 0),
                                 ('# /etc/sysctl.d NORMAL\n', 1), ('/etc/sysctl.dx NORMAL\n', 1),
                                 ('/etc/sysctlxd NORMAL\n', 1), ('/etc/sysctl.d.bak NORMAL\n', 1)]:
                conf.write_text(text)
                pattern = line.strip().split('if ', 1)[1].split(' /etc/aide.conf', 1)[0]
                result = subprocess.run(['bash', '-c', '_path=/etc/sysctl.d; ' + pattern + ' ' + str(conf)])
                self.assertEqual(result.returncode, expect, text)

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
            copier.write_text('#!/bin/sh\n[ "$1" = /bin/true ] && exit 1\nexec /bin/cp "$@"\n')  # /bin/cp: macOS has no /usr/bin/cp
            copier.chmod(0o755)
            result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                     'record() { printf "%s %s\\n" "$1" "$2"; };\n' + source[start:end]],
                                    env=dict(os.environ, HOME=str(home), PATH=str(tools) + ':' + os.environ['PATH']),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'home-tmp-dir PASS')
            self.assertEqual(list((home / 'tmp').iterdir()), [])

    def test_scripts_with_a_missing_interpreter_are_named(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('_dead_scripts=""')
        end = source.index('unset _dead_scripts', start)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            scripts = home / '.local/bin'
            scripts.mkdir(parents=True)
            for name, body in [('ansible', '#!/gone/python3.13\n'), ('fine', '#!/bin/sh\n'), ('tab', '#!/bin/sh\t-e\n'),
                               ('elf', '\x7fELF\x00\x00#!/gone\n'), ('env', '#!/usr/bin/env python3\n')]:
                (scripts / name).write_text(body)
                (scripts / name).chmod(0o755)
            run = lambda: subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                          'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; };\n' + source[start:end]],
                                         env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith('local-bin-interpreters WARN'), result.stdout)
            self.assertIn('{ansible}', result.stdout)
            (scripts / 'ansible').unlink()
            self.assertEqual(run().stdout.strip(), 'local-bin-interpreters PASS')

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
                                  '  "-c authdata") echo "time.example,NTS,1,15,256,135,0,0,8,100";;\nesac\n')
            executable.chmod(0o755)
            for fails, expected in [('0', 'PASS'), ('1', 'FAIL')]:
                result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                         'record() { printf "%s %s\\n" "$1" "$2"; }; CSB_HOST=false;\n' + source[start:end]],
                                        env=dict(os.environ, PATH=directory + ':' + os.environ['PATH'], CHRONY_QUERY_FAIL=fails),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'chrony-nts-active ' + expected)
            denied = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                                     'record() { printf "%s %s\\n" "$1" "$2"; }; CSB_HOST=false; _chrony_auth_denied=true;\n'
                                     + source[start:end]],
                                    env=dict(os.environ, PATH=directory + ':' + os.environ['PATH'], CHRONY_QUERY_FAIL='0'),
                                    capture_output=True, text=True)
            self.assertEqual(denied.returncode, 0, denied.stderr)
            self.assertEqual(denied.stdout.strip(), 'chrony-nts-active WARN')

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
                            '_csb_non_fedora=false;\n' + loop],
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
            hostname = home / 'hostname'  # a real *.csb host would skip the chrony checks
            hostname.write_text('#!/bin/sh\necho fixture.example\n')
            hostname.chmod(0o755)
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
                    for name in ['env-d-containers', 'env-d-ssh-agent'] if platform.system() == 'Linux' else []:
                        self.assertEqual(checks[name], 'SKIP', data)  # environment.d checks run only on Linux
                    if profile == 'personal':
                        self.assertEqual(checks['gitconfig-includeif'], 'SKIP', data)
                    # macOS: the system role that sets home permissions is Linux-only, so any mode passes
                    strict = platform.system() == 'Linux'
                    self.assertEqual(checks['home-dir-perms'], 'PASS' if mode in [0o700, 0o550] or not strict else 'FAIL', data)
                    home.chmod(0o700)
                    self.assertEqual(data['failures'], sum(status == 'FAIL' for status in checks.values()))


    def test_gitconfig_check_fails_when_signing_key_file_is_missing(self):
        """dotfiles can enable commit signing before ssh_client deploys the key; verify must not PASS then."""
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('_gc="$HOME/.config/git/config"')
        end = source.index('if [[ -f "$HOME/.gitconfig" ]]', start)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / '.config/git/config'
            config.parent.mkdir(parents=True)
            config.write_text('# Ansible managed\n[user]\n\tsigningkey = ~/.ssh/id_ed25519_sk_signing.pub\n')

            def run():
                return subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s\\n" "$1" "$2"; };\n' + source[start:end]],
                    env=dict(os.environ, HOME=str(home)), capture_output=True, text=True).stdout.strip()
            self.assertEqual(run(), 'dotfile-gitconfig FAIL')
            (home / '.ssh').mkdir()
            (home / '.ssh/id_ed25519_sk_signing.pub').write_text('key\n')
            self.assertEqual(run(), 'dotfile-gitconfig PASS')
            config.write_text('# Ansible managed\n[user]\n\tname = x\n')  # no signing configured: unaffected
            (home / '.ssh/id_ed25519_sk_signing.pub').unlink()
            self.assertEqual(run(), 'dotfile-gitconfig PASS')

    def test_cross_session_inbound_check_fails_for_any_managed_dir_without_accept(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        parse = source[source.index('_claude_extra_dirs=$(awk'):source.index('unset _cfg\n')]
        start = source.index('_csi_bad=()')
        end = source.index('unset _cd _csi_bad _csi_seen', start)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            config = Path(directory) / 'config.yml'
            config.write_text('')

            def run():
                return subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; };\n' + parse + source[start:end]],
                    env=dict(os.environ, HOME=str(home), _cfg=str(config)), capture_output=True, text=True).stdout.strip()
            self.assertTrue(run().startswith('claude-cross-session-inbound FAIL no Claude config dir'))
            for name, value in [('.claude', 'accept'), ('.claude-per2', 'hold')]:
                (home / name).mkdir()
                (home / name / 'settings.json').write_text(json.dumps({'crossSessionInbound': value}))
            (home / '.claude-work').mkdir()  # no settings.json yet
            result = run()
            self.assertTrue(result.startswith('claude-cross-session-inbound FAIL'), result)
            self.assertIn('.claude-work .claude-per2', result)
            self.assertNotIn('.claude ', result.split(' of: ')[1])
            for name in ['.claude-work', '.claude-per2']:
                (home / name / 'settings.json').write_text(json.dumps({'crossSessionInbound': 'accept'}))
            self.assertEqual(run(), 'claude-cross-session-inbound PASS')
            # an unmanaged ~/.claude-* (not in claude_extra_config_dirs) is not checked
            (home / '.claude-tmp').mkdir()
            (home / '.claude-tmp' / 'settings.json').write_text(json.dumps({'crossSessionInbound': 'hold'}))
            self.assertEqual(run(), 'claude-cross-session-inbound PASS')
            (home / '.claude-second').mkdir()
            (home / '.claude-second' / 'settings.json').write_text(json.dumps({'crossSessionInbound': 'hold'}))
            self.assertEqual(run(), 'claude-cross-session-inbound PASS')
            for listing in (f'claude_extra_config_dirs: ["{home}/.claude-second"]  # a second login\n',
                            f"claude_extra_config_dirs:\n  - '{home}/.claude-second'\n  # comment\nprofile: work\n",
                            f'claude_extra_config_dirs: [{home}/.claude-third, {home}/.claude-second]\n'):
                with self.subTest(listing=listing):
                    config.write_text(listing)
                    result = run()
                    self.assertTrue(result.startswith('claude-cross-session-inbound FAIL'), result)
                    self.assertTrue(result.endswith('of: .claude-second — run: make apply CHUNK=agents'), result)
            (home / '.claude-second' / 'settings.json').write_text(json.dumps({'crossSessionInbound': 'accept'}))
            self.assertEqual(run(), 'claude-cross-session-inbound PASS')
            # only ~/.claude-<name> entries are read
            config.write_text(f'claude_extra_config_dirs: [{home}/.claude-tmp/../.claude-tmp, {directory}/.claude-tmp]\n')
            self.assertEqual(run(), 'claude-cross-session-inbound PASS')

    def test_agent_cli_logins_lists_each_logged_out_home_without_logging_in(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('_logged_out=()')
        end = source.index('unset _logged_out', start)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            tools = Path(directory) / 'tools'
            calls = Path(directory) / 'calls'
            home.mkdir()
            tools.mkdir()
            # Logged in: only ~/.claude, checked as cper runs it (CLAUDE_CONFIG_DIR set, token variables cleared),
            # plus the default (unset) login while $HOME/default-login exists (a separate Keychain item on macOS).
            (tools / 'claude').write_text(
                '#!/bin/sh\necho "claude $* ${CLAUDE_CONFIG_DIR-unset}" >> "$CALLS"\n'
                '[ "$1 $2" = "auth status" ] || exit 9\n[ -z "${ANTHROPIC_API_KEY+x}" ] || exit 0\n'
                '[ -z "${CLAUDE_CONFIG_DIR+x}" ] && { [ -e "$HOME/default-login" ]; exit; }\n'
                '[ "$CLAUDE_CONFIG_DIR" = "$HOME/.claude" ]\n')
            (home / 'default-login').touch()
            (tools / 'codex').write_text(
                '#!/bin/sh\necho "codex $* $CODEX_HOME" >> "$CALLS"\n[ "$1 $2" = "login status" ] || exit 9\n'
                '[ -n "${OPENAI_API_KEY+x}" ]\n')
            for tool in ('claude', 'codex'):
                (tools / tool).chmod(0o755)

            def run(prelude='', timeout_bin=''):
                result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; }; CONTAINER=; ONLY=; _codex_enabled=true; '
                    '_claude_home2_off=false; _timeout_bin=' + timeout_bin + '; ' + prelude + '\n' + source[start:end]],
                    env=dict(os.environ, HOME=str(home), CALLS=str(calls), CLAUDE_CONFIG_DIR='/elsewhere',
                             ANTHROPIC_API_KEY='x', OPENAI_API_KEY='x', PATH=str(tools) + ':' + os.environ['PATH']),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            self.assertEqual(run(), 'agent-cli-logins PASS')  # no managed homes yet
            (home / '.claude').mkdir()
            self.assertEqual(run(), 'agent-cli-logins PASS')
            (home / 'default-login').unlink()  # logged in only with CLAUDE_CONFIG_DIR set (the macOS split)
            self.assertEqual(run(), 'agent-cli-logins WARN not logged in, run: env -u CLAUDE_CONFIG_DIR claude auth login')
            (home / 'default-login').touch()
            for name in ('.claude-per2', '.codex-personal', '.claude-work'):  # .claude-work: Vertex, not checked
                (home / name).mkdir()
            expected = ('agent-cli-logins WARN not logged in, run: CLAUDE_CONFIG_DIR=~/.claude-per2 claude auth login; '
                        'CODEX_HOME=~/.codex-personal codex login')
            self.assertEqual(run(), expected)
            self.assertNotIn('\\~', run())
            self.assertEqual(run(timeout_bin=shutil.which('timeout') or ''), expected)
            self.assertNotIn('.claude-work', calls.read_text())
            self.assertTrue(all(' status ' in line for line in calls.read_text().splitlines()), calls.read_text())
            # config.yml homes, a disabled second instance, unselected or container runs
            (home / 'per').mkdir()
            self.assertEqual(run('_claude_home="~/per"; _claude_home2_off=true; _codex_enabled=false'),
                             'agent-cli-logins WARN not logged in, run: CLAUDE_CONFIG_DIR=~/per claude auth login')
            (Path(directory) / 'home2 x').mkdir()  # outside $HOME despite the shared prefix, with a space
            self.assertEqual(run('_claude_home="' + directory + '/home2 x"; _claude_home2_off=true; _codex_enabled=false'),
                             'agent-cli-logins WARN not logged in, run: CLAUDE_CONFIG_DIR=' + directory.replace('/', '/')
                             + '/home2\\ x claude auth login')
            # a hung status (timeout's 124) is not reported as a logout
            hang = Path(directory) / 'hang'
            hang.write_text('#!/bin/sh\nexit 124\n')
            hang.chmod(0o755)
            self.assertEqual(run('_claude_home2_off=true; _codex_enabled=false; _claude_home=~/nonexistent', str(hang)),
                             'agent-cli-logins PASS')
            self.assertEqual(run('_claude_home2_off=true; _codex_enabled=false', str(hang)),
                             'agent-cli-logins WARN login status timed out for: ~/.claude')
            # claude_extra_config_dirs: valid ~/.claude-<name> entries only, never the Vertex home
            (home / '.claude-extra').mkdir()
            extra = ('printf -v _claude_extra_dirs "%s\\n" "$HOME/.claude-extra" "$HOME/.claude-work" /tmp/x '
                     '"$HOME/.claude-Bad" "$HOME/.claude-per2"; _claude_home2="~/.claude-per2"; ')  # same home twice
            self.assertEqual(run(extra + '_claude_home2_off=true; _codex_enabled=false'),
                             'agent-cli-logins WARN not logged in, run: CLAUDE_CONFIG_DIR=~/.claude-extra claude auth login; '
                             'CLAUDE_CONFIG_DIR=~/.claude-per2 claude auth login')
            self.assertEqual(run(extra + '_codex_enabled=false'),
                             'agent-cli-logins WARN not logged in, run: CLAUDE_CONFIG_DIR=~/.claude-per2 claude auth login; '
                             'CLAUDE_CONFIG_DIR=~/.claude-extra claude auth login')
            calls.unlink()
            self.assertEqual(run('ONLY=,codex,'), '')
            self.assertFalse(calls.exists())
            self.assertEqual(run('CONTAINER=dev'), 'agent-cli-logins SKIP host-only')

    def test_macos_remote_access_needs_the_key_policy_and_mosh_firewall_rule(self):
        """Remote Login never counts as fine without the key-only policy; it waits for authorized_keys."""
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _missing="" _rl_wait=false')
        end = source.index('  unset _missing _rl_wait _rl_on _mosh _alf _sshd_policy', start)
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory).resolve()
            only_tools(d / 'bin', 'sed', 'cat', 'awk', 'sort', 'realpath', 'grep')
            (d / 'home/.ssh').mkdir(parents=True)
            (d / 'prefix/bin').mkdir(parents=True)
            mosh = d / 'prefix/bin/mosh-server'
            mosh.write_text('')
            for name, body in [('launchctl', '[ -e "$STATE/$(echo "$2" | sed s,system/,,)" ]'),
                               ('brew', 'echo "$PREFIX"'), ('alf', 'cat "$STATE/apps" 2>/dev/null')]:
                (d / 'bin' / name).write_text('#!/bin/sh\n' + body + '\n')
                (d / 'bin' / name).chmod(0o755)
            state = d / 'state'
            (state / 'sshd_config.d').mkdir(parents=True)
            policy = state / 'sshd_config.d/000-laptop-setup.conf'
            (state / 'sshd_config').write_text('Include /etc/ssh/sshd_config.d/*\n')  # not this directory

            def run(remote_login, screen_sharing=False, molecule=''):
                result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; };'
                    f' _macos_remote_login={remote_login} _macos_screen_sharing={screen_sharing};'
                    f' _alf={d}/bin/alf _sshd_policy={policy} _sshd_config={state}/sshd_config;\n' + source[start:end]],
                    env=dict(os.environ, PATH=f'{d}/bin', HOME=str(d / 'home'),
                             STATE=str(state), PREFIX=str(d / 'prefix'), MOLECULE_PROJECT_DIRECTORY=molecule),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            self.assertTrue(run('false').startswith('macos-remote-access PASS remote access not managed'))
            (state / 'apps').write_text(f'1 : {mosh} \n')
            self.assertTrue(run('true').startswith('macos-remote-access WARN sshd stays off'))
            (state / 'apps').write_text('')
            self.assertIn('mosh-server-firewall', run('true'))
            (state / 'apps').write_text(f'1 : {mosh} \n')
            (d / 'home/.ssh/authorized_keys').write_text('sk-ssh-ed25519@openssh.com AAAA k\n')
            self.assertEqual(run('true'), 'macos-remote-access FAIL missing: sshd — run: make apply CHUNK=desktop (needs sudo)')
            (state / 'com.openssh.sshd').write_text('')  # Remote Login on from System Settings, without the policy
            self.assertTrue(run('false').startswith('macos-remote-access FAIL missing: sshd-key-policy'))
            # GitHub's macOS runners have Remote Login on: a WARN under Molecule, but only when that is all
            self.assertTrue(run('false', molecule='/src').startswith('macos-remote-access WARN Remote Login on'))
            self.assertTrue(run('false', 'true', molecule='/src').startswith('macos-remote-access FAIL'))
            self.assertIn('sshd', run('true'))  # and not our job on ssh_port
            (state / 'com.openssh.sshd').unlink()
            (state / 'com.laptop-setup.sshd').write_text('')
            self.assertIn('sshd-key-policy', run('true'))
            policy.write_text('# PasswordAuthentication no\n')
            self.assertIn('sshd-key-policy(PasswordAuthentication-no)', run('true'))
            policy.write_text('PasswordAuthentication no\nKbdInteractiveAuthentication no\n')
            self.assertIn('sshd-key-policy(not-included-by-sshd_config)', run('true'))
            (state / 'sshd_config').write_text(f'Include {state}/sshd_config.d/*\nAuthorizedKeysFile .ssh/authorized_keys\n')
            self.assertEqual(run('true'), 'macos-remote-access PASS')
            (state / 'sshd_config').write_text('Include sshd_config.d/*\nPasswordAuthentication yes\n')  # relative; after ours
            self.assertEqual(run('true'), 'macos-remote-access PASS')
            (state / 'sshd_config').write_text(f'kbdinteractiveauthentication=yes\nInclude {state}/sshd_config.d/*\n')
            self.assertIn('sshd-password-auth(sshd_config)', run('true'))
            (state / 'sshd_config').write_text(f'Include {state}/sshd_config.d/*\n')
            (state / 'sshd_config.d/100-macos.conf').write_text('PasswordAuthentication yes\n')  # sorts after ours
            (state / 'sshd_config.d/.hidden.conf').write_text('PasswordAuthentication yes\n')  # sshd's * skips it
            self.assertEqual(run('true'), 'macos-remote-access PASS')
            (state / 'sshd_config.d/000-early.conf').write_text('UsePAM yes\n')  # C order: 000-e before 000-l
            result = run('true')
            self.assertIn('sshd-key-policy-first(000-early.conf sorts before it)', result)
            self.assertNotIn('sshd-password-auth', result)
            (state / 'sshd_config.d/000-early.conf').write_text('  ChallengeResponseAuthentication yes\n')
            self.assertIn('sshd-password-auth(000-early.conf)', run('true'))
            (state / 'sshd_config.d/000-early.conf').unlink()
            self.assertEqual(run('true'), 'macos-remote-access PASS')
            self.assertIn('Screen-Sharing', run('true', 'true'))

    def test_macos_dev_tools_needs_every_tool(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _brew=$( (brew --prefix)')
        end = source.index('  unset _brew _missing _t', start)
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / 'bin').mkdir()
            (d / 'bin/brew').write_text('#!/bin/sh\necho "$PREFIX"\n')
            (d / 'bin/brew').chmod(0o755)
            prefix = d / 'prefix'

            def tool(path):
                path = prefix / path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!/bin/sh\n')
                path.chmod(0o755)
                return path

            def run():
                result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; }; _macos_python=python@3.14;\n'
                    + source[start:end]],
                    env=dict(os.environ, PATH=f'{d}/bin', PREFIX=str(prefix)),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            self.assertEqual(run(), 'macos-dev-tools FAIL missing: python(python@3.14) autoreconf automake glibtoolize '
                                    'pkg-config make(GNU) — run: make apply CHUNK=packages NOSUDO=1')
            for path in ['opt/python@3.14/libexec/bin/python', 'bin/autoreconf', 'bin/automake', 'bin/glibtoolize',
                         'bin/pkg-config']:
                tool(path)
            make = tool('opt/make/libexec/gnubin/make')
            self.assertEqual(run(), 'macos-dev-tools PASS')
            make.chmod(0o644)  # present but not executable
            self.assertEqual(run(), 'macos-dev-tools FAIL missing: make(GNU) — run: make apply CHUNK=packages NOSUDO=1')

    def test_docker_shim_needs_a_runnable_target(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  if ! $_podman_machine; then record "docker-shim"')
        end = source.index('\nfi\n', start)
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            only_tools(d / 'bin', 'grep', 'sed')  # no host docker: /usr/bin/docker would take the PASS branch
            (d / 'home/.local/bin').mkdir(parents=True)
            shim = d / 'home/.local/bin/docker'
            podman = d / 'podman'

            def run(machine='true', docker_shim='true', molecule=''):
                result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; };'
                    f' _podman_machine={machine} _docker_shim={docker_shim};\n' + source[start:end]],
                    env=dict(os.environ, PATH=f'{d}/bin', HOME=str(d / 'home'),
                             MOLECULE_PROJECT_DIRECTORY=molecule),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            self.assertEqual(run(machine='false'), 'docker-shim PASS containers_macos_machine_enabled is off')
            self.assertEqual(run(docker_shim='false'), 'docker-shim PASS containers_macos_docker_shim is off')
            self.assertTrue(run().startswith('docker-shim FAIL no docker command'))
            self.assertTrue(run(molecule='/src').startswith('docker-shim WARN no docker command'))
            # the shim as the containers role writes it
            task = next(t for t in yaml.safe_load((ROOT / 'roles/containers/tasks/macos-docker.yml').read_text())
                        if t.get('name') == 'Deploy the docker-to-podman shim (macOS)')
            shim.write_text(task['ansible.builtin.copy']['content'].replace('{{ containers_podman_bin }}', str(podman)))
            shim.chmod(0o755)
            self.assertEqual(run(), f'docker-shim FAIL the docker shim runs {podman}, which is not executable — '
                                    'run: make apply CHUNK=packages, then make apply CHUNK=containers')
            podman.write_text('#!/bin/sh\n')
            podman.chmod(0o755)
            self.assertEqual(run(), 'docker-shim PASS')
            shim.unlink()
            (d / 'bin/docker').write_text('#!/bin/sh\n')  # a real docker on PATH
            (d / 'bin/docker').chmod(0o755)
            self.assertEqual(run(), 'docker-shim PASS')
    @unittest.skipUnless(shutil.which('zsh'), 'zsh required')
    def test_macos_launchd_path_matches_what_zshenv_builds(self):
        """GUI apps get launchd's PATH: it must equal the PATH a bare zsh -c builds from ~/.zshenv."""
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index("  # GUI apps and launchd jobs get launchd's PATH")
        end = source.index('  unset _want _have', start)
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory).resolve()
            home, stubs = d / 'home', d / 'bin'
            home.mkdir()
            (home / '.zshenv').write_text('path=(/opt/brew/opt/coreutils/libexec/gnubin /opt/brew/bin $path); export PATH\n')
            only_tools(stubs, 'env', 'grep')  # env -i then finds the host zsh on the bare system PATH
            (stubs / 'launchctl').write_text('#!/bin/sh\n[ "$1 $2" = "getenv PATH" ] && [ -n "$LPATH" ] && echo "$LPATH"\n')
            (stubs / 'launchctl').chmod(0o755)

            def run(launchd_path, gnu='true'):
                result = subprocess.run([BASH, '-euo', 'pipefail', '-c',
                    f'record() {{ printf "%s %s\\n" "$1" "$2"; }}; _launchd_gnu={gnu};\n' + source[start:end]],
                    env={'PATH': str(stubs), 'HOME': str(home), 'LPATH': launchd_path}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            if not shutil.which('zsh', path='/usr/bin:/bin:/usr/sbin:/sbin'):
                self.skipTest('no zsh on the system PATH')
            full = '/opt/brew/opt/coreutils/libexec/gnubin:/opt/brew/bin:/usr/bin:/bin:/usr/sbin:/sbin'
            self.assertEqual(run(full), 'macos-launchd-path PASS')
            self.assertEqual(run('/usr/bin:/bin:/usr/sbin:/sbin'), 'macos-launchd-path FAIL')  # launchd's default
            self.assertEqual(run(''), 'macos-launchd-path FAIL')
            # dotfiles_macos_launchd_gnu_tools: false: GUI apps get the PATH without the GNU tools first
            self.assertEqual(run('/opt/brew/bin:/usr/bin:/bin:/usr/sbin:/sbin', gnu='false'), 'macos-launchd-path PASS')
            self.assertEqual(run(full, gnu='false'), 'macos-launchd-path FAIL')

    def test_macos_zshenv_path_and_launchd_agent_socket(self):
        """Bare-PATH zsh (ssh remote commands) must get Homebrew and GNU tools first; launchd must name the agent."""
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _zpath=$(env -i')
        end = source.index('  unset _brew _zpath _missing _sock', start)
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory).resolve()
            home, brew, stubs = d / 'home', d / 'brew', d / 'bin'
            for path in (home, brew / 'bin', brew / 'opt/coreutils/libexec/gnubin'):
                path.mkdir(parents=True)
            only_tools(stubs, 'env')  # env -i then finds the host zsh on the bare system PATH
            (stubs / 'launchctl').write_text('#!/bin/sh\n[ "$1 $2" = "getenv SSH_AUTH_SOCK" ] && [ -n "$SOCK" ] && echo "$SOCK"\n')
            (stubs / 'launchctl').chmod(0o755)

            def run(zshenv, sock, gnu='true', molecule=''):
                (home / '.zshenv').write_text(zshenv)
                result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c',
                    'record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; };'
                    f' _brew={brew} _macos_gnu_tools={gnu};\n' + source[start:end]],
                    env=dict(os.environ, PATH=str(stubs), HOME=str(home), SOCK=sock,
                             MOLECULE_PROJECT_DIRECTORY=molecule), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.splitlines()
            good = f'path=({home}/.local/bin {brew}/opt/coreutils/libexec/gnubin {brew}/bin $path)\n'
            sock = f'{home}/.ssh/ssh-agent.socket'
            self.assertEqual(run(good, sock), ['macos-zshenv-path PASS ', 'macos-launchd-ssh-auth-sock PASS '])
            late = f'path=({home}/.local/bin $path {brew}/opt/coreutils/libexec/gnubin {brew}/bin)\n'
            path, _ = run(late, sock)
            self.assertTrue(path.startswith('macos-zshenv-path FAIL'), path)
            self.assertIn(f' {brew}/bin', path)
            self.assertIn('coreutils-gnubin', path)
            self.assertNotIn('coreutils-gnubin', run(late, sock, gnu='false')[0])  # GNU tools turned off
            self.assertIn('~/.local/bin', run('', sock)[0])  # no ~/.zshenv
            # Everything appended after the system PATH, so /usr/bin is first and nothing precedes it
            self.assertTrue(run(f'path=($path {home}/.local/bin {brew}/bin)\n', sock, gnu='false')[0]
                            .startswith('macos-zshenv-path FAIL'))
            self.assertTrue(run(good, '')[1].startswith('macos-launchd-ssh-auth-sock FAIL launchd SSH_AUTH_SOCK is unset'))
            apple = '/private/tmp/com.apple.launchd.x/Listeners'
            self.assertTrue(run(good, apple)[1].startswith('macos-launchd-ssh-auth-sock FAIL'))
            self.assertTrue(run(good, apple, molecule='/src')[1].startswith('macos-launchd-ssh-auth-sock WARN'))

    def test_aerospace_config_check_needs_the_managed_file(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _aerospace_conf="$HOME/.config/aerospace/aerospace.toml"')
        end = source.index('  unset _aerospace_conf', start)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '.config/aerospace/aerospace.toml'
            tools = only_tools(Path(directory) / 'bin', 'grep')
            for text, expected in [(None, 'FAIL'), ('# Seeded once by Ansible; edit freely\n', 'FAIL'),
                                   ('# Ansible managed\nconfig-version = 2\n', 'PASS')]:
                with self.subTest(text=text):
                    if text is not None:
                        config.parent.mkdir(parents=True, exist_ok=True)
                        config.write_text(text)
                    result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; };\n'
                                             + source[start:end]], env=dict(os.environ, HOME=directory, PATH=str(tools)),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'aerospace-config ' + expected)

    def test_aerospace_running_check_only_when_it_is_the_wm(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  if [[ "$_desktop_env" != aerospace ]]')
        end = source.index('\nfi\n', start)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'pgrep').write_text('#!/bin/sh\n[ "$1 $2" = "-x AeroSpace" ] && exit "$RUNNING"\nexit 2\n')
            (Path(directory) / 'pgrep').chmod(0o755)
            for wm, running, molecule, expected in [('aerospace', 0, '', 'PASS'), ('aerospace', 1, '', 'FAIL'),
                                                    ('aerospace', 1, '/src', 'WARN'), ('CHANGE_ME', 1, '', 'PASS')]:
                with self.subTest(wm=wm, running=running, molecule=molecule):
                    result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; };'
                                             f' _desktop_env={wm};\n' + source[start:end]],
                                            env=dict(os.environ, PATH=directory,
                                                     RUNNING=str(running), MOLECULE_PROJECT_DIRECTORY=molecule),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'aerospace-running ' + expected)

    def test_macos_git_difftool_check(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('  _git_tools="$(git config --file "$_gc" --get merge.tool')
        end = source.index('  unset _git_tools', start)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config'
            tools = only_tools(Path(directory) / 'bin', 'git')
            (tools / 'vim').write_text('#!/bin/sh\n')
            (tools / 'vim').chmod(0o755)
            for text, expected in [('[merge]\n\ttool = vimdiff\n[diff]\n\ttool = vimdiff\n', 'PASS'),
                                   ('[merge]\n\ttool = meld\n[diff]\n\ttool = meld\n', 'FAIL'),
                                   ('[merge]\n\tconflictStyle = zdiff3\n', 'FAIL')]:
                with self.subTest(text=text):
                    config.write_text(text)
                    result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; };'
                                             f' _gc={config};\n' + source[start:end]],
                                            env=dict(os.environ, GIT_CONFIG_NOSYSTEM='1', PATH=str(tools)),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'macos-git-difftool ' + expected)
            (tools / 'vim').unlink()
            config.write_text('[merge]\n\ttool = vimdiff\n[diff]\n\ttool = vimdiff\n')
            result = subprocess.run([BASH, '-eu', '-o', 'pipefail', '-c', 'record() { printf "%s %s\\n" "$1" "$2"; };'
                                     f' _gc={config};\n' + source[start:end]],
                                    env=dict(os.environ, GIT_CONFIG_NOSYSTEM='1', PATH=str(tools)), capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), 'macos-git-difftool FAIL')  # no vim

    def test_github_ssh_auth_tells_unregistered_key_from_pin_needed(self):
        """A verify-required FIDO2 key always exits 255 under BatchMode: -v output says whether GitHub knows the key."""
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('_ssh_ret=0\n_timeout_bin=')
        end = source.index('unset _ssh_ret _ssh_err', start)
        with tempfile.TemporaryDirectory() as directory:
            ssh = Path(directory) / 'ssh'
            for stderr, code, expect in [
                ('debug1: Offering public key: k ED25519-SK\\ndebug1: Server accepts key: k ED25519-SK\\n', 255, 'PASS'),
                ('debug1: Offering public key: k ED25519-SK\\ngit@github.com: Permission denied (publickey).\\n', 255, 'WARN'),
                ('ssh: connect to host github.com port 22: Network is unreachable\\n', 255, 'WARN'),
                ('', 1, 'PASS'),
            ]:
                with self.subTest(stderr=stderr, code=code):
                    ssh.write_text('#!/bin/sh\necho stdout-noise\nprintf "%s" "' + stderr + '" >&2\nexit ' + str(code) + '\n')
                    ssh.chmod(0o755)
                    result = subprocess.run(['bash', '-u', '-c',
                        'record() { printf "%s %s %s\\n" "$1" "$2" "$3"; }; ONLY=;\n' + source[start:end]],
                        env=dict(os.environ, PATH=directory + ':' + os.environ['PATH']),
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.startswith('github-ssh-auth ' + expect), result.stdout)
                    if 'Permission denied' in stderr:
                        self.assertIn('make github-ssh-keys', result.stdout)

    def test_github_ssh_keys_adds_only_missing_keys_and_refreshes_scope_once(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.ssh').mkdir()
            (home / '.ssh/id_ed25519_sk.pub').write_text('sk-ssh-ed25519@openssh.com AAAAprimary yubikey-1\n')
            (home / '.ssh/id_ed25519_sk_backup.pub').write_text('sk-ssh-ed25519@openssh.com AAAAbackup yubikey-2\n')
            (home / '.ssh/id_ed25519_sk_signing.pub').write_text('sk-ssh-ed25519@openssh.com AAAAsigning signing\n')
            log = home / 'gh.log'
            gh = home / 'gh'
            # the key lists fail until refreshed (no scope); then user/keys has only the primary key and
            # user/ssh_signing_keys has the signing key only once the file 'signed' exists
            gh.write_text('#!/bin/sh\necho "$*" >> %s\ncase "$1 $2" in\n'
                          '"auth status") exit 0 ;;\n"auth refresh") touch %s/scoped; exit 0 ;;\n'
                          '"api --hostname") [ -f %s/scoped ] || exit 1\n'
                          '  case "$5" in user/keys) echo "ssh-ed25519 AAAAprimary" ;;\n'
                          '  user/ssh_signing_keys) [ -f %s/signed ] && echo "sk-ssh-ed25519@openssh.com AAAAsigning" ;;\n'
                          '  esac; exit 0 ;;\n'
                          'esac\nexit 0\n' % (log, home, home, home))
            gh.chmod(0o755)
            env = dict(os.environ, HOME=str(home), PATH=str(home) + ':' + os.environ['PATH'])
            def run():
                return subprocess.run([str(ROOT / 'scripts/github-ssh-keys.sh')], env=env,
                                      capture_output=True, text=True, stdin=subprocess.DEVNULL)
            result = run()  # no terminal: cannot run the interactive scope refresh
            self.assertEqual(result.returncode, 1)
            self.assertIn('admin:ssh_signing_key', result.stderr)
            (home / 'scoped').touch()
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            adds = [c for c in calls if c.startswith('ssh-key add')]
            self.assertEqual(adds, [
                'ssh-key add %s/.ssh/id_ed25519_sk_backup.pub --title yubikey-2 --type authentication' % home,
                'ssh-key add %s/.ssh/id_ed25519_sk_signing.pub --title signing --type signing' % home])
            self.assertFalse(any(c.startswith('auth refresh') for c in calls))
            # once GitHub lists the signing key, a re-run adds nothing new
            (home / 'signed').touch()
            log.write_text('')
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([c for c in log.read_text().splitlines() if c.startswith('ssh-key add')],
                             ['ssh-key add %s/.ssh/id_ed25519_sk_backup.pub --title yubikey-2 --type authentication' % home])

    def test_tailscale_operator_check(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index("# The packages role makes the login user Tailscale's operator")
        end = source.index('unset _ts_want\n', start)
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'tailscale'
            env = dict(os.environ, PATH=f'{directory}:{os.environ["PATH"]}', USER='alice')
            env.pop('SUDO_USER', None)

            def run(prefs, want='alice', prefs_rc=0):
                stub.write_text(f'#!/bin/sh\nprintf %s {shlex.quote(prefs)}\nexit {prefs_rc}\n')
                stub.chmod(0o755)
                script = ('record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; }\nIS_LINUX=true CONTAINER= SYSTEM_ONLY=false\n'
                          f'_ts_want={shlex.quote(want)}\n' + source[start:end])
                return subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', script], env=env,
                                      capture_output=True, text=True).stdout.strip()

            self.assertEqual(run('{\n  "OperatorUser": "alice",\n  "NetfilterMode": 0\n}'), 'tailscale-operator PASS')
            self.assertIn("tailscale-operator WARN Tailscale operator is '', not alice", run('{\n  "NetfilterMode": 0\n}'))
            self.assertIn("operator is 'bob', not alice", run('{"OperatorUser": "bob"}'))
            self.assertEqual(run('{}', want=''), "tailscale-operator PASS not managed (packages_tailscale_operator: '')")
            self.assertEqual(run('', prefs_rc=1), '')  # tailscaled down: the tailscale check reports it

    @unittest.skipUnless(Path('/usr/bin/gnome-shell').exists() and os.geteuid() != 0, 'GNOME host, not root')
    def test_scroll_direction_check_reads_touchpad_and_mouse(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('# Traditional scrolling (natural scrolling off)')
        end = source.index('\nfi\n', source.index('unset _scroll', start)) + 4
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / 'gsettings'
            env = dict(os.environ, PATH=f'{directory}:{os.environ["PATH"]}')

            def run(touchpad, mouse):
                stub.write_text(f'#!/bin/sh\ncase "$2" in *touchpad) echo {touchpad} ;; *mouse) echo {mouse} ;; esac\n')
                stub.chmod(0o755)
                script = ('record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; }\nIS_LINUX=true CONTAINER= SYSTEM_ONLY=false\n'
                          + source[start:end])
                return subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', script], env=env,
                                      capture_output=True, text=True).stdout.strip()

            self.assertEqual(run('false', 'false'), 'scroll-direction PASS')
            self.assertIn('scroll-direction WARN GNOME natural scrolling (touchpad, mouse): true false', run('true', 'false'))
            self.assertIn('false true', run('false', 'true'))

    @unittest.skipUnless(shutil.which('zsh'), 'zsh required')
    def test_remote_control_check_names_only_launchers_without_the_flag(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('# Remote Control: plain claude starts with it')
        end = source.index('unset _csi_dirs', start)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.zshrc').write_text('\n'.join([
                'good() { claude --remote-control --dangerously-skip-permissions "$@"; }',
                'bad() { claude --dangerously-skip-permissions "$@"; }',
                'bad2() { command claude "$@"; }',
                'claude() { command claude "$@"; }',  # the instance guard passes everything through
                'note() { echo "run make claude first"; }', '']))
            script = ('record() { printf "%s %s %s\\n" "$1" "$2" "${3:-}"; }\n_csi_dirs=()\n' + source[start:end])
            out = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', script], env=dict(os.environ, HOME=directory),
                                 capture_output=True, text=True).stdout.strip()
            self.assertTrue(out.startswith('claude-remote-control WARN'), out)
            self.assertIn('shell function bad starts', out)
            self.assertIn('shell function bad2 starts', out)
            for fine in ('good', 'claude', 'note'):
                self.assertNotIn(f'function {fine} ', out)
            (home / '.zshrc').write_text('good() { claude --remote-control "$@"; }\n')
            out = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', script], env=dict(os.environ, HOME=directory),
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, 'claude-remote-control PASS')

    def test_scripts_run_on_macos_bash_3(self):
        """macOS /bin/bash is 3.2: no ${var,,}/${var^^} case conversion, declare -A or mapfile."""
        import re
        for name in ('smoke-test.sh', 'preflight.sh', 'vault-init.sh', 'ssh-keys.sh', 'github-ssh-keys.sh'):
            text = (ROOT / 'scripts' / name).read_text()
            for pattern in (r'\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^)\}', r'\bdeclare -A\b', r'\b(mapfile|readarray)\b'):
                self.assertIsNone(re.search(pattern, text), f'{name}: bash 4 construct {pattern}')

if __name__ == '__main__':
    unittest.main()
