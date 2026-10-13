#!/usr/bin/env python3
"""Exercise account isolation, preservation, backup and migration metadata."""
import json
import os
from pathlib import Path
import shlex
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


def run_play(directory, tasks=None, variables=None, roles=None, extra_args=()):
    play = {'name': 'Migration regression', 'hosts': 'localhost', 'gather_facts': False,
            'vars': variables or {}}
    if tasks:
        play['tasks'] = tasks
    if roles:
        play['roles'] = roles
    path = directory / 'play.yml'
    path.write_text(yaml.safe_dump([play]))
    env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'),
               MOLECULE_PROJECT_DIRECTORY=str(ROOT),
               ANSIBLE_ROLES_PATH=str(ROOT / 'roles'),
               ANSIBLE_COLLECTIONS_PATH=str(ROOT / 'collections'))
    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(path), *extra_args],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout


class MigrationTests(unittest.TestCase):
    def test_codex_model_overrides_are_validated_before_account_writes(self):
        for variable in ['codex_work_model', 'codex_personal_model']:
            for invalid in [None, False, 123, '', '   ', 'custom-\x00-model', 'custom-\x1f-model', 'custom-\x7f-model']:
                with self.subTest(variable=variable, invalid=invalid), tempfile.TemporaryDirectory() as name:
                    directory = Path(name)
                    with self.assertRaisesRegex(AssertionError, 'Validate distinct Codex account homes'):
                        run_play(directory, variables={'ansible_facts': {'user_dir': name},
                            'packages_install_binaries': False, variable: invalid}, roles=['codex'])
                    self.assertFalse((directory / '.codex').exists())
                    self.assertFalse((directory / '.codex-personal').exists())
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            run_play(directory, variables={'ansible_facts': {'user_dir': name},
                'packages_install_binaries': False, 'codex_work_model': 'custom-"work"-🚀',
                'codex_personal_model': 'custom-personal-model'}, roles=['codex'])
            self.assertEqual(tomllib.loads((directory / '.codex/config.toml').read_text())['model'],
                             'custom-"work"-🚀')
            self.assertEqual(tomllib.loads((directory / '.codex-personal/config.toml').read_text())['model'],
                             'custom-personal-model')

    def test_claude_personal2_paths_reject_tmpfiles_injection_before_writes(self):
        production = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        selected = [t for t in production if t.get('name') in [
            'Set active Claude instance directories',
            'SECURITY assert active Claude instance paths cannot inject tmpfiles directives',
            'Create Claude instance config directories']]
        self.assertEqual(len(selected), 3)
        for suffix in ['', '\n', '\nz /fixture/unrelated 0000 - - -\n#']:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                variables = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
                variables.update({'ansible_facts': {'user_dir': name}, 'profile': 'personal',
                                  'claude_personal2_config_dir': name + '/.claude-per2' + suffix})
                if suffix:
                    with self.assertRaisesRegex(AssertionError, 'cannot inject tmpfiles'):
                        run_play(directory, tasks=selected, variables=variables)
                    self.assertFalse((directory / '.claude').exists())
                else:
                    run_play(directory, tasks=selected, variables=variables)
                    self.assertTrue((directory / '.claude-per2').is_dir())

    @unittest.skipIf(os.geteuid() == 0, 'unreadable-directory fixture needs an unprivileged user')
    def test_backup_discovery_errors_never_report_success(self):
        fixtures = [
            ('.codex/skills/private', 0, False),
            ('.claude/projects/private', 0, False),
            ('.ssh', 0, False),
            ('.ssh', 0o300, False),
            ('.gnupg', 0, False),
            ('.gnupg/private-keys-v1.d', 0o300, False),
            ('.gnupg/public-keys.d', 0o300, False),
            ('.config/gcloud', 0, False),
            ('.agents', 0, False),
            ('.codex-personal', 0o300, True),
            ('.codex/config.toml', 0, False),
            ('.dotfiles/config', 0, False),
        ]
        for relative, mode, history in fixtures:
            with self.subTest(relative=relative, mode=mode), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                fixture = directory / 'home'
                fixture.mkdir()
                scripts = directory / 'project/scripts'
                scripts.mkdir(parents=True)
                for filename in ['backup.sh', 'verify-vault-stub.py']:
                    shutil.copy(ROOT / 'scripts' / filename, scripts / filename)
                vault = scripts.parent / 'group_vars/all/vault.yml'
                vault.parent.mkdir(parents=True)
                vault.write_text('vault_placeholder: "encrypt-me"\nvault_key: ""\n')
                tools = directory / 'bin'
                tools.mkdir()
                getent = tools / 'getent'
                getent.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote(
                    'fixture:x:1000:1000::' + str(fixture) + ':/bin/zsh') + '\n')
                getent.chmod(0o700)
                blocked = fixture / relative
                if relative.endswith('.toml'):
                    blocked.parent.mkdir(parents=True)
                    blocked.write_text('fixture config\n')
                else:
                    blocked.mkdir(parents=True)
                    (blocked / 'SKILL.md').write_text('fixture skill\n')
                blocked.chmod(mode)
                if relative == '.dotfiles/config':
                    (fixture / '.zshrc').symlink_to(blocked / 'SKILL.md')
                scratch = directory / 'scratch'
                scratch.mkdir()
                try:
                    for dry_run in [False, True]:
                        result = subprocess.run(['bash', str(scripts / 'backup.sh')]
                            + (['--dry-run'] if dry_run else [])
                            + (['--ai-history'] if history else []),
                            env=dict(os.environ, HOME=str(fixture), TMPDIR=str(scratch),
                                     PATH=str(tools) + ':' + os.environ['PATH']),
                            capture_output=True, text=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('ERROR: backup incomplete', result.stderr)
                        self.assertNotIn('Backed up', result.stdout)
                        self.assertNotIn('would back up', result.stdout)
                        self.assertEqual(list(scratch.iterdir()), [])
                finally:
                    blocked.chmod(0o700)

    def test_macos_profiles_include_codex_npm_runtime(self):
        defaults = yaml.safe_load((ROOT / 'roles/packages/defaults/main.yml').read_text())
        brew = yaml.safe_load((ROOT / 'roles/packages/tasks/install_brew.yml').read_text())
        formulas = next(t['block'][0]['community.general.homebrew']['name'] for t in brew
                        if t.get('name') == 'Install Homebrew packages (macOS)')
        for profile in ['personal', 'work']:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as name:
                defaults['profile'] = profile
                result = run_play(Path(name), tasks=[{'name': 'Resolve macOS runtime prerequisites',
                    'ansible.builtin.assert': {'that': ["'" + tool + "' in (" + formulas.strip()[2:-2] + ")"
                                                       for tool in ['node', 'coreutils', 'bash', 'python3']]}}],
                    variables=defaults)
                self.assertIn('failed=0', result)

    def test_codex_fresh_check_mode_never_installs_or_writes_accounts(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            result = run_play(directory, variables={'ansible_facts': {'user_dir': name}},
                              roles=['codex'], extra_args=['--check'])
            self.assertIn('failed=0', result)
            self.assertFalse((directory / '.codex').exists())
            self.assertFalse((directory / '.local').exists())

    def test_codex_rejects_resolved_path_overlap_before_writes(self):
        for case in ['slash', 'dot', 'symlink', 'nested-install', 'nested-account', 'home', 'outside']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                variables = {'ansible_facts': {'user_dir': name}, 'packages_install_binaries': False}
                personal = name + '/.codex'
                if case == 'slash':
                    variables['codex_personal_config_dir'] = personal + '/'
                elif case == 'dot':
                    variables['codex_personal_config_dir'] = name + '/./.codex'
                elif case == 'symlink':
                    (directory / '.codex-personal').symlink_to(directory / '.codex')
                elif case == 'nested-install':
                    variables['codex_install_dir'] = personal + '/cli'
                elif case == 'nested-account':
                    variables['codex_work_config_dir'] = name + '/.local/share/codex-cli/node_modules/work'
                elif case == 'home':
                    variables['codex_work_config_dir'] = name + '/.'
                else:
                    (directory / '.codex-personal').symlink_to(directory.parent)
                with self.assertRaisesRegex(AssertionError, 'non-overlapping'):
                    run_play(directory, variables=variables, roles=['codex'])
                self.assertFalse((directory / '.local').exists())
                self.assertFalse((directory / '.codex').exists())

    def test_codex_offline_preserves_configs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            variables = {'ansible_facts': {'user_dir': name}, 'packages_install_binaries': False}
            run_play(directory, variables=variables, roles=['codex'])
            config = directory / '.codex/config.toml'
            self.assertEqual(tomllib.loads(config.read_text())['model'], 'gpt-6.1-sol')
            instructions = directory / '.codex/AGENTS.md'
            self.assertIn('--signoff', instructions.read_text())
            self.assertEqual(instructions.read_text(), (directory / '.codex-personal/AGENTS.md').read_text())
            config.write_text('model = "user-model"\n')
            instructions.write_text('User-owned working agreements\n')
            result = run_play(directory, variables=variables, roles=['codex'])
            self.assertIn('changed=0', result)
            self.assertEqual(config.read_text(), 'model = "user-model"\n')
            self.assertEqual(instructions.read_text(), 'User-owned working agreements\n')
            self.assertFalse((directory / '.local/share/codex-cli').exists())
            self.assertEqual((directory / '.codex').stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / '.codex-personal/config.toml').stat().st_mode & 0o777, 0o600)
            self.assertEqual(instructions.stat().st_mode & 0o777, 0o600)

    def test_claude_baseline_scopes_plugins_and_preserves_cli_preferences(self):
        production = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        selected = [task for task in production if task.get('name') in [
            'Seed Claude user preferences without replacing CLI edits',
            'Deploy instance CLAUDE.md (managed by Ansible, use CLAUDE.local.md for local edits)']]
        for task in selected:
            if 'ansible.builtin.template' in task:
                task['ansible.builtin.template']['src'] = str(ROOT / 'roles/claude/templates/CLAUDE.md.j2')
        for system in ['Linux', 'Darwin']:
            with self.subTest(system=system), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                variables = yaml.safe_load((ROOT / 'roles/claude/defaults/main.yml').read_text())
                variables.update({'ansible_facts': {'user_dir': name, 'system': system},
                                  'dotfiles_github_user': 'fixture-owner',
                                  'role_path': str(ROOT / 'roles/claude'), 'ansible_managed': 'test',
                                  '_claude_instances': [name + '/.claude', name + '/.claude-work', name + '/.claude-per2']})
                for instance in variables['_claude_instances']:
                    Path(instance).mkdir(mode=0o700)
                run_play(directory, tasks=selected, variables=variables)
                for instance in ['.claude', '.claude-work', '.claude-per2']:
                    settings_file = directory / instance / 'settings.json'
                    settings = json.loads(settings_file.read_text())
                    self.assertEqual(settings['tui'], 'fullscreen')
                    self.assertTrue(settings['enabledPlugins']['claude-skills@claude-skills'])
                    self.assertEqual('release-management@submariner-release' in settings['enabledPlugins'],
                                     instance == '.claude-work')
                    self.assertEqual(settings['extraKnownMarketplaces']['claude-skills']['source']['repo'],
                                     'fixture-owner/claude-skills')
                    self.assertNotIn(name, json.dumps(settings))
                    agreements = (directory / instance / 'CLAUDE.md').read_text()
                    self.assertIn('--signoff', agreements)
                    self.assertNotIn('~/laptop-setup', agreements)
                    self.assertNotIn('You cannot push', agreements)
                    settings_file.write_text('{"model": "user-model", "enabledPlugins": {}}\n')
                result = run_play(directory, tasks=selected, variables=variables)
                self.assertIn('changed=0', result)
                self.assertEqual(json.loads((directory / '.claude-work/settings.json').read_text()),
                                 {'model': 'user-model', 'enabledPlugins': {}})

    @unittest.skipUnless(os.environ.get('CODEX_INSTALL_TEST') == '1', 'opt-in npm download test')
    def test_codex_real_locked_install_and_repeat(self):
        # Native archives are large; avoid /tmp tmpfs space/quota pressure on laptop hosts.
        with tempfile.TemporaryDirectory(prefix='laptop-setup-codex-test-', dir=Path.home()) as name:
            directory = Path(name)
            variables = {'ansible_facts': {'user_dir': name}}
            run_play(directory, variables=variables, roles=['codex'])
            executable = directory / '.local/bin/codex'
            self.assertEqual(subprocess.check_output([str(executable), '--version'], text=True).strip(),
                             'codex-cli 0.162.1')
            checks = yaml.safe_load((ROOT / 'molecule/shared/verify-codex.yml').read_text())
            run_play(directory, tasks=checks, variables={'ansible_user_dir': name, 'system_is_container': False})
            self.assertIn('changed=0', run_play(directory, variables=variables, roles=['codex']))

    def test_claude_native_installer_receives_and_verifies_version_target(self):
        production = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
        install = next(t['block'] for t in production if t.get('name') == 'Install Claude Code via native binary')
        selected = [t for t in install if t.get('name') in [
            'Run Claude Code install script', 'Verify pinned Claude Code version after native installation']]
        for wrong_version in [False, True]:
            with self.subTest(wrong_version=wrong_version), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                installer = directory / 'install.sh'
                binary = directory / 'claude'
                argument_log = directory / 'target.txt'
                # Like the public installer, this fixture reads its target from $1, not CLAUDE_VERSION.
                reported = '9.9.9' if wrong_version else '${1:-latest}'
                installer.write_text('printf "%s" "${1:-latest}" > ' + shlex.quote(str(argument_log)) + '\n'
                    'printf "#!/bin/sh\\nprintf \'%s (Claude Code)\\\\n\'\\n" "' + reported + '" > '
                    + shlex.quote(str(binary)) + '\nchmod 700 ' + shlex.quote(str(binary)) + '\n')
                variables = {'_claude_temp_file': {'path': str(installer)},
                             'claude_pinned_version': '1.2.3', 'claude_bin_path': str(binary)}
                if wrong_version:
                    with self.assertRaisesRegex(AssertionError, 'Verify pinned Claude Code version'):
                        run_play(directory, tasks=selected, variables=variables)
                else:
                    run_play(directory, tasks=selected, variables=variables)
                self.assertEqual(argument_log.read_text(), '1.2.3')

    def test_lock_covers_all_archives_and_exact_platform_pins(self):
        lock = json.loads((ROOT / 'roles/codex/files/package-lock.json').read_text())
        packages = lock['packages']
        version = packages['']['dependencies']['@openai/codex']
        for name, package in packages.items():
            if not name:
                continue
            self.assertTrue(package['resolved'].startswith('https://registry.npmjs.org/@openai/codex/'))
            self.assertTrue(package['integrity'].startswith('sha512-'))
            self.assertTrue(package['version'].startswith(version))
        self.assertEqual(len(packages), 8)  # root, launcher, six platform archives

    @unittest.skipUnless(shutil.which('zsh'), 'zsh required for launcher regression')
    def test_rendered_shell_accounts_arguments_and_environment(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            defaults = yaml.safe_load((ROOT / 'roles/dotfiles/defaults/main.yml').read_text())
            defaults.update({'ansible_managed': 'test', 'ansible_facts': {'user_dir': name, 'system': 'Linux'},
                             'profile': 'work', 'packages_gotoolchain': 'local',
                             'claude_queue_runtime_config_dir': name + "/runtime path's config",
                             'claude_work_config_dir': name + "/work account's config",
                             'notes_repo_path': name + '/notes', 'claude_personal_config_dir': name + '/.claude-personal'})
            config = directory / 'zshrc'
            run_play(directory, tasks=[{'name': 'Render production shell', 'ansible.builtin.template': {
                'src': str(ROOT / 'roles/dotfiles/templates/zshrc.j2'), 'dest': str(config), 'mode': '0600'}}],
                variables=defaults)
            subprocess.run(['zsh', '-n', str(config)], check=True)
            if shutil.which('go'):
                go_env = directory / 'goenv'
                go_env.write_text('GONOSUMDB=*\nGOPRIVATE=stale.invalid\n')
                go_exports = '\n'.join(line for line in config.read_text().splitlines()
                                        if line.startswith(('export GONOSUMDB=', 'export GOPRIVATE=')))
                effective = subprocess.run(['zsh', '-f', '-c', go_exports + '\ncommand go env GONOSUMDB GOPRIVATE'],
                    cwd=directory, env=dict(os.environ, GOENV=str(go_env), GOTOOLCHAIN='local'),
                    capture_output=True, text=True, check=True)
                self.assertEqual(effective.stdout.splitlines(), ['none', 'none'])
            # Load the actual launcher block without starting plugins, tool hooks or compinit.
            content = config.read_text()
            launchers = content[content.index('_claude_check_base_url()'):content.index('# --- Device access')]
            binary_dir = directory / 'bin'
            binary_dir.mkdir()
            recorder = '#!/usr/bin/env python3\nimport os,sys,json\nprint(json.dumps({"tool":os.path.basename(sys.argv[0]),"args":sys.argv[1:],"env":{k:v for k,v in os.environ.items() if k.startswith(("CLAUDE_","ANTHROPIC_","GOOGLE_","CODEX_","OPENAI_"))}}))\n'
            for tool in ['claude', 'codex', 'agy']:
                path = binary_dir / tool
                path.write_text(recorder)
                path.chmod(0o700)
            work_env = Path(defaults['claude_queue_runtime_config_dir']) / 'work-env'
            work_env.parent.mkdir(parents=True)
            production = yaml.safe_load((ROOT / 'roles/claude/tasks/main.yml').read_text())
            run_play(directory, tasks=[next(t for t in production if t.get('name') == 'Deploy Claude work-env file')],
                     variables=defaults)
            probe = directory / 'probe.zsh'
            probe.write_text(launchers + '''
cper 'argument with spaces'
cper2 second
cwork third
oper fourth
owork fifth
gper sixth
print -r -- "PARENT:$CLAUDE_CODE_USE_VERTEX:$CODEX_HOME:$ANTHROPIC_API_KEY"
''')
            env = dict(os.environ, HOME=name, PATH=str(binary_dir) + ':' + os.environ['PATH'],
                       CLAUDE_CODE_USE_VERTEX='1', CODEX_HOME='original', ANTHROPIC_API_KEY='fixture-key',
                       ANTHROPIC_AUTH_TOKEN='fixture-token', GOOGLE_APPLICATION_CREDENTIALS='/fixture/adc',
                       OPENAI_API_KEY='fixture-openai')
            result = subprocess.run(['zsh', '-f', str(probe)], env=env, capture_output=True, text=True, check=True)
            rows = result.stdout.splitlines()
            records = [json.loads(line) for line in rows[:-1]]
            self.assertEqual(records[0]['args'][-1], 'argument with spaces')
            for record in records[:3]:
                self.assertEqual(record['args'][:3], ['--dangerously-skip-permissions', '--plugin-dir', '.'])
            for record in records[3:5]:
                self.assertEqual(record['args'][:2], ['--ask-for-approval=never', '--sandbox=danger-full-access'])
            self.assertEqual(records[0]['env']['CLAUDE_CONFIG_DIR'], name + '/.claude')
            self.assertEqual(records[1]['env']['CLAUDE_CONFIG_DIR'], name + '/.claude-per2')
            for record in records[:2]:
                self.assertEqual(record['env']['CLAUDE_CODE_USE_VERTEX'], '0')
                self.assertNotIn('ANTHROPIC_API_KEY', record['env'])
                self.assertNotIn('GOOGLE_APPLICATION_CREDENTIALS', record['env'])
            self.assertEqual(records[2]['env']['CLAUDE_CONFIG_DIR'], defaults['claude_work_config_dir'])
            self.assertEqual(records[2]['env']['CLAUDE_CODE_USE_VERTEX'], '1')
            self.assertEqual(records[3]['env']['CODEX_HOME'], name + '/.codex-personal')
            self.assertNotIn('OPENAI_API_KEY', records[3]['env'])
            self.assertEqual(records[4]['env']['CODEX_HOME'], name + '/.codex')
            self.assertEqual(records[5]['tool'], 'agy')
            self.assertEqual(rows[-1], 'PARENT:1:original:fixture-key')
            # Exercise the extra shell parsing level used by tmux with the same custom paths.
            tmux = binary_dir / 'tmux'
            tmux.write_text('#!/usr/bin/env python3\nimport os,sys,subprocess\n'
                            'raise SystemExit(subprocess.call([os.environ["FIXTURE_ZSH"],"-f","-c",sys.argv[-1]]))\n')
            tmux.chmod(0o700)
            (binary_dir / 'zsh').write_text('#!/bin/sh\nexit 0\n')
            (binary_dir / 'zsh').chmod(0o700)
            probe.write_text(launchers + '\ncw\ncwt fixture-worktree\n')
            tmux_result = subprocess.run([shutil.which('zsh'), '-f', str(probe)],
                env=dict(env, FIXTURE_ZSH=shutil.which('zsh'), CLAUDE_CODE_USE_VERTEX='0'),
                capture_output=True, text=True, check=True)
            tmux_records = [json.loads(line) for line in tmux_result.stdout.splitlines()]
            self.assertEqual(len(tmux_records), 2)
            for record in tmux_records:
                self.assertEqual(record['env']['CLAUDE_CONFIG_DIR'], defaults['claude_work_config_dir'])
                self.assertEqual(record['env']['CLAUDE_CODE_USE_VERTEX'], '1')
                self.assertNotIn('ANTHROPIC_API_KEY', record['env'])
            self.assertEqual(tmux_records[1]['args'], ['--worktree', 'fixture-worktree'])
            defaults['ansible_facts']['system'] = 'Darwin'
            run_play(directory, tasks=[{'name': 'Render work macOS shell', 'ansible.builtin.template': {
                'src': str(ROOT / 'roles/dotfiles/templates/zshrc.j2'), 'dest': str(config), 'mode': '0600'}}],
                variables=defaults)
            subprocess.run(['zsh', '-n', str(config)], check=True)
            content = config.read_text()
            self.assertIn('/opt/homebrew/bin:$PATH', content)
            path_exports = '\n'.join(line for line in content.splitlines() if line.startswith('export PATH='))
            effective_path = subprocess.check_output([shutil.which('zsh'), '-f', '-c',
                path_exports + '\nprint -r -- "$PATH"'], env=dict(os.environ, HOME=name), text=True).strip().split(':')
            self.assertEqual(effective_path[0], name + '/.local/bin')
            self.assertLess(effective_path.index(name + '/go/bin'), effective_path.index('/opt/homebrew/bin'))
            launchers = content[content.index('_claude_check_base_url()'):content.index('# --- Device access')]
            probe.write_text(launchers + '\ncwork mac-work\noper mac-personal\n')
            mac_result = subprocess.run([shutil.which('zsh'), '-f', str(probe)], env=env,
                                        capture_output=True, text=True, check=True)
            mac_records = [json.loads(line) for line in mac_result.stdout.splitlines()]
            self.assertEqual(mac_records[0]['env']['CLAUDE_CONFIG_DIR'], defaults['claude_work_config_dir'])
            self.assertEqual(mac_records[0]['args'][-1], 'mac-work')
            self.assertEqual(mac_records[1]['env']['CODEX_HOME'], name + '/.codex-personal')
            defaults.update({'profile': 'personal', 'claude_personal2_instance_enabled': False,
                             'ansible_facts': {'user_dir': name, 'system': 'Darwin'}})
            run_play(directory, tasks=[{'name': 'Render personal macOS shell', 'ansible.builtin.template': {
                'src': str(ROOT / 'roles/dotfiles/templates/zshrc.j2'), 'dest': str(config), 'mode': '0600'}}],
                variables=defaults)
            subprocess.run(['zsh', '-n', str(config)], check=True)
            personal = config.read_text()
            for function in ['cwork()', 'owork()', 'cper2()']:
                self.assertNotIn(function, personal)
            self.assertIn('oper()', personal)

    def test_backup_legacy_links_private_state_and_external_link_rejection(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            fixture = directory / 'fixture'
            fixture.mkdir()
            scripts = directory / 'project/scripts'
            scripts.mkdir(parents=True)
            shutil.copy(ROOT / 'scripts/backup.sh', scripts / 'backup.sh')
            shutil.copy(ROOT / 'scripts/verify-vault-stub.py', scripts / 'verify-vault-stub.py')
            (scripts.parent / 'config.yml').write_text("dotfiles_github_user: !unsafe 'fixture-owner'\n")
            vault = scripts.parent / 'group_vars/all/vault.yml'
            vault.parent.mkdir(parents=True)
            vault.write_text('vault_placeholder: "encrypt-me"\nvault_key: ""\n')
            dotfiles = fixture / '.dotfiles'
            dotfiles.mkdir()
            (dotfiles / '.zshrc').write_text('local alias\n')
            (fixture / '.zshrc').symlink_to(dotfiles / '.zshrc')
            ssh = fixture / '.ssh'
            ssh.mkdir()
            (dotfiles / 'ssh_config').write_text('Host fixture\n')
            (ssh / 'config').symlink_to(dotfiles / 'ssh_config')
            (ssh / 'id_ed25519_sk_yk0.priv').write_text('fixture private handle\n')
            (ssh / 'id_ed25519_sk_yk0.pub').write_text('fixture public handle\n')
            (ssh / 'alternate-key').write_text('fixture alternate key\n')
            outside = directory / 'outside'
            outside.write_text('outside\n')
            external_home = directory / 'external-home'
            external_home.mkdir()
            (external_home / 'auth.json').write_text('external fixture auth\n')
            (fixture / '.codex-personal').symlink_to(external_home)
            gpg = fixture / '.gnupg/public-keys.d'
            gpg.mkdir(parents=True)
            (gpg / 'pubring.db').symlink_to(outside)
            agents = fixture / 'Library/LaunchAgents'
            agents.mkdir(parents=True)
            (agents / 'com.fixture-owner.claude-queue.plist').write_text('fixture plist\n')
            (fixture / '.vimrc').symlink_to(outside)
            (fixture / '.codex').mkdir()
            (fixture / '.codex/config.toml').write_text('model = "fixture"\n')
            memory = fixture / '.claude-per2/projects/fixture/memory'
            memory.mkdir(parents=True)
            (memory / 'MEMORY.md').write_text('fixture memory\n')
            (memory.parent / 'transcript.jsonl').write_text('private transcript\n')
            (fixture / '.codex/skills').mkdir()
            (fixture / '.codex/skills/outside').symlink_to(outside)
            binary_dir = directory / 'bin'
            binary_dir.mkdir()
            getent = binary_dir / 'getent'
            getent.write_text('#!/bin/sh\nprintf "%s\\n" ' + shlex.quote('fixture:x:1000:1000::' + str(fixture) + ':/bin/zsh') + '\n')
            getent.chmod(0o700)
            date = binary_dir / 'date'
            date.write_text('#!/bin/sh\necho fixture-timestamp\n')
            date.chmod(0o700)
            env = dict(os.environ, HOME=str(fixture), PATH=str(binary_dir) + ':' + os.environ['PATH'])
            result = subprocess.run(['bash', str(scripts / 'backup.sh')], env=env, capture_output=True, text=True, check=True)
            backup = next(fixture.glob('laptop-setup-backup-*'))
            self.assertEqual((backup / '.zshrc').read_text(), 'local alias\n')
            self.assertFalse((backup / '.zshrc').is_symlink())
            self.assertEqual((backup / '.ssh/config').read_text(), 'Host fixture\n')
            for filename in ['id_ed25519_sk_yk0.priv', 'id_ed25519_sk_yk0.pub', 'alternate-key']:
                self.assertTrue((backup / '.ssh' / filename).is_file())
                self.assertEqual((backup / '.ssh' / filename).stat().st_mode & 0o777, 0o600)
            self.assertFalse((backup / '.vimrc').exists())
            self.assertTrue((backup / '.claude-per2/projects/fixture/memory/MEMORY.md').is_file())
            self.assertFalse((backup / '.claude-per2/projects/fixture/transcript.jsonl').exists())
            self.assertFalse((backup / '.codex/skills/outside').exists())
            self.assertFalse((backup / '.codex-personal/auth.json').exists())
            self.assertFalse((backup / '.gnupg/public-keys.d/pubring.db').exists())
            self.assertTrue((backup / 'Library/LaunchAgents/com.fixture-owner.claude-queue.plist').exists())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
            self.assertEqual((backup / '.codex/config.toml').stat().st_mode & 0o777, 0o600)
            self.assertIn('skipping external', result.stderr)
            duplicate = subprocess.run(['bash', str(scripts / 'backup.sh')], env=env,
                                       capture_output=True, text=True)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertIn('destination already exists', duplicate.stderr)
            # A pre-existing destination symlink must not receive credential writes.
            shutil.rmtree(backup)
            backup.symlink_to(external_home)
            redirected = subprocess.run(['bash', str(scripts / 'backup.sh')], env=env,
                                        capture_output=True, text=True)
            self.assertNotEqual(redirected.returncode, 0)
            self.assertFalse((external_home / '.codex/config.toml').exists())
            vault.write_text('vault_placeholder: "encrypt-me"\nvault_key: fixture-secret\n')
            rejected = subprocess.run(['bash', str(scripts / 'backup.sh'), '--dry-run'],
                                      env=env, capture_output=True, text=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn('fixture-secret', rejected.stdout + rejected.stderr)

    def test_metadata_dirty_worktree_refs_and_remote_redaction(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            repo = directory / 'repo'
            repo.mkdir()
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(repo), *args], env=env, text=True)
            git('init', '-q')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('config', 'core.hooksPath', '/dev/null')
            (repo / 'tracked').write_text('old\n')
            git('add', 'tracked')
            git('commit', '-qm', 'fixture')
            git('remote', 'add', 'origin', 'https://user:fixture-secret@example.invalid/repo.git?token=fixture-token')
            git('remote', 'add', 'helper', 'https::https://user:helper-secret@example.invalid/repo.git?token=helper-token#private')
            (repo / 'tracked').write_text('changed\n')
            (repo / 'untracked').write_text('untracked content\n')
            git('worktree', 'add', '-qb', 'local-only', str(directory / 'worktree'))
            unborn = directory / 'unborn'
            subprocess.run(['git', 'init', '-q', str(unborn)], env=env, check=True)
            (unborn / 'draft').write_text('local work without any commits\n')
            config_repo = directory / '.claude'
            subprocess.run(['git', 'init', '-q', str(config_repo)], env=env, check=True)
            nested_cache = config_repo / 'cache/plugin'
            subprocess.run(['git', 'init', '-q', str(nested_cache)], env=env, check=True)
            output = directory / 'report.json'
            subprocess.run(['python3', str(ROOT / 'scripts/migration-inventory.py'), str(directory),
                            '--output', str(output)], check=True, capture_output=True)
            report = json.loads(output.read_text())
            self.assertNotIn('helper-secret', output.read_text())
            self.assertNotIn('helper-token', output.read_text())
            self.assertFalse(report['errors'])
            self.assertEqual(len(report['repositories']), 4)
            main = next(r for r in report['repositories'] if r['path'].endswith('/repo'))
            self.assertEqual(main['modified'], 1)
            self.assertEqual(main['untracked'], 1)
            self.assertTrue(any(r['gitfile'] for r in report['repositories']))
            empty = next(r for r in report['repositories'] if r['path'].endswith('/unborn'))
            self.assertTrue(any(r['path'].endswith('/.claude') for r in report['repositories']))
            self.assertFalse(any(r['path'].endswith('/cache/plugin') for r in report['repositories']))
            self.assertIsNone(empty['head'])
            self.assertEqual(empty['untracked'], 1)
            self.assertTrue(any(b['branch'] == 'local-only' for b in main['branches']))
            self.assertEqual(main['remotes']['origin'], 'https://example.invalid/repo.git')
            self.assertNotIn('fixture-secret', output.read_text())
            self.assertNotIn('fixture-token', output.read_text())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            repeated = subprocess.run(['python3', str(ROOT / 'scripts/migration-inventory.py'), str(directory),
                                       '--output', str(output)], capture_output=True)
            self.assertNotEqual(repeated.returncode, 0)


if __name__ == '__main__':
    unittest.main()
